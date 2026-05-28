import os
import time
import logging
import threading
import firebase_admin
from firebase_admin import credentials, db

logger = logging.getLogger(__name__)

_firebase_init_lock = threading.Lock()


def initialize_firebase():
    with _firebase_init_lock:
        if firebase_admin._apps:
            return

        database_url = os.getenv("FIREBASE_DATABASE_URL")
        if not database_url:
            raise RuntimeError("Manca FIREBASE_DATABASE_URL nelle env vars.")

        cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if cred_path and os.path.isfile(cred_path):
            cred = credentials.Certificate(cred_path)
        else:
            cred = credentials.ApplicationDefault()

        firebase_admin.initialize_app(cred, {
            'databaseURL': database_url,
            'httpTimeout': 10,
        })


def _retry(func, *args, retries: int = 3, delay: float = 1.0, **kwargs):
    """
    Esegue func(*args, **kwargs) fino a `retries` volte in caso di eccezione.
    Attende `delay` secondi (raddoppiato ad ogni tentativo) prima di riprovare.
    """
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_exc = e
            logger.warning(f"[Firebase] Tentativo {attempt}/{retries} fallito per {func.__name__}: {e}")
            if attempt < retries:
                time.sleep(delay * attempt)
    logger.error(f"[Firebase] Tutti i tentativi falliti per {func.__name__}: {last_exc}")
    raise last_exc


class AdminManager:
    """Gestisce la lista degli admin del bot per un gruppo specifico."""

    def __init__(self, group_id: str):
        initialize_firebase()
        self.group_id = group_id
        self.ref = db.reference(f'admins/{self.group_id}')

    def add_admin(self, user_id: str) -> None:
        """Aggiunge un utente alla lista degli admin."""
        _retry(self.ref.child(user_id).set, True)

    def remove_admin(self, user_id: str) -> None:
        """Rimuove un utente dalla lista degli admin."""
        _retry(self.ref.child(user_id).delete)

    def is_admin(self, user_id: str) -> bool:
        """Verifica se un utente è nella lista degli admin."""
        try:
            return _retry(self.ref.child(user_id).get) is not None
        except Exception:
            return False


class LeaderboardManager:

    def __init__(self, group_id: str):
        initialize_firebase()
        self.group_id = group_id
        self.ref = db.reference(f'classifiche/{self.group_id}')

    def get_leaderboard(self) -> dict:
        """Restituisce la classifica completa come dict {user_id: score}"""
        try:
            return _retry(self.ref.get) or {}
        except Exception:
            return {}

    def update_score(self, user_id: str, score: int) -> None:
        """Imposta il punteggio di un utente a un valore assoluto."""
        _retry(self.ref.child(user_id).set, score)

    def increment_score(self, user_id: str, delta: int) -> tuple:
        """
        Incrementa (o decrementa) il punteggio di un utente di `delta` in modo atomico,
        usando una transazione Firebase per evitare race condition.

        Comportamento:
        - Se l'utente NON esiste, viene creato con punteggio 0 e delta viene applicato.
        - Se l'utente ESISTE, delta viene sommato al punteggio corrente.
        - Il punteggio non può mai scendere sotto 0: se la sottrazione porterebbe
          il risultato negativo, viene azzerato a 0 e viene sollevata una ValueError
          con prefisso 'punteggio_azzerato:' in modo che il chiamante possa
          informare l'utente in modo chiaro.

        Restituisce: (new_score: int, is_new_user: bool)

        SECURITY: Valida delta prima di applicare (max ±10000).
        """
        if not isinstance(delta, int):
            raise TypeError(f"delta deve essere int, non {type(delta)}")

        # VALIDATION: Evita bomba di punteggi o exploit negativi
        if abs(delta) > 10000:
            logger.warning(f"[LeaderboardManager] Tentativo di incremento anomalo user={user_id} delta={delta}")
            raise ValueError(f"delta troppo grande: {delta}")

        result = {'new_score': 0, 'is_new_user': False, 'clamped': False}

        def transaction_fn(current_value):
            is_new = current_value is None
            result['is_new_user'] = is_new
            base = 0 if is_new else int(current_value)
            new_score = base + delta
            if new_score < 0:
                result['clamped'] = True
                new_score = 0
            result['new_score'] = new_score
            return new_score

        try:
            _retry(self.ref.child(user_id).transaction, transaction_fn)
        except Exception as e:
            logger.error(f"[LeaderboardManager] Errore increment_score user={user_id} delta={delta}: {e}")
            raise

        if result['clamped']:
            logger.warning(
                f"[LeaderboardManager] Score negativo bloccato a 0: user={user_id} delta={delta}"
            )
            raise ValueError(f"punteggio_azzerato:{result['new_score']}")

        return result['new_score'], result['is_new_user']

    def reset_leaderboard(self) -> None:
        """Pulisce tutte le voci della classifica per questo gruppo."""
        _retry(self.ref.delete)


class SettingsManager:

    def __init__(self, group_id: str):
        initialize_firebase()
        self.group_id = group_id
        self.ref = db.reference(f'impostazioni/{self.group_id}')

    def get_setting(self, key: str, default=None):
        """Recupera il valore di una singola impostazione."""
        try:
            value = _retry(self.ref.child(key).get)
            return value if value is not None else default
        except Exception:
            return default

    def set_setting(self, key: str, value) -> None:
        """Imposta o aggiorna una singola impostazione."""
        _retry(self.ref.child(key).set, value)

    def get_all(self) -> dict:
        """Recupera tutte le impostazioni del gruppo."""
        try:
            return _retry(self.ref.get) or {}
        except Exception:
            return {}

    def reset_settings(self) -> None:
        """Ripristina tutte le impostazioni di questo gruppo (cancellandole)."""
        _retry(self.ref.delete)


class PrenotationManager:

    def __init__(self, group_id: str):
        initialize_firebase()
        self.group_id = group_id
        self.base_ref = db.reference(f'prenotazioni/{self.group_id}')

    def add_prenotation(self, set_number: int, user_id: str, numbers: list) -> None:
        """Aggiunge o aggiorna la prenotazione di un utente per uno specifico set."""
        _retry(
            self.base_ref.child(str(set_number)).child(user_id).set,
            {'numbers': numbers, 'timestamp': time.time()}
        )

    def remove_prenotation(self, set_number: int, user_id: str) -> bool:
        """
        Rimuove la prenotazione di un utente per un set.
        Restituisce True se la prenotazione esisteva ed è stata rimossa, False altrimenti.
        """
        prenotation_ref = self.base_ref.child(str(set_number)).child(user_id)
        try:
            if _retry(prenotation_ref.get):
                _retry(prenotation_ref.delete)
                return True
        except Exception as e:
            logger.error(f"[PrenotationManager] Errore remove_prenotation set={set_number} user={user_id}: {e}")
        return False

    def get_prenotations(self, set_number: int) -> dict:
        """Recupera tutte le prenotazioni per un set specifico."""
        try:
            return _retry(self.base_ref.child(str(set_number)).get) or {}
        except Exception:
            return {}

    def get_sorted_prenotations(self, set_number: int) -> list:
        """
        Ordina i prenotati per:
        1. carte oro mancanti (crescente)
        2. carte bianche mancanti (crescente)
        3. timestamp di prenotazione (crescente, chi si è prenotato prima)
        """
        data = self.get_prenotations(set_number)

        def sort_key(item):
            uid, info = item
            try:
                if isinstance(info, dict):
                    numbers = info.get('numbers', [0, 0])
                    if not isinstance(numbers, list) or len(numbers) < 2:
                        numbers = [0, 0]
                else:
                    numbers = [0, 0]
                
                bianche = int(numbers[0]) if len(numbers) > 0 else 0
                oro = int(numbers[1]) if len(numbers) > 1 else 0
                timestamp = info.get('timestamp', 0) if isinstance(info, dict) else 0
            except (ValueError, TypeError, AttributeError):
                logger.warning(f"Errore parsing prenotazione {uid}: {info}")
                bianche, oro, timestamp = 0, 0, 0
            
            return (oro, bianche, timestamp)

        return sorted(data.items(), key=sort_key)

    def clear_prenotations(self, set_number: int) -> None:
        """Rimuove tutte le prenotazioni di un set."""
        try:
            _retry(self.base_ref.child(str(set_number)).delete)
        except Exception as e:
            logger.error(f"[PrenotationManager] Errore clear_prenotations set={set_number}: {e}")

    def clear_all_prenotations_for_group(self) -> None:
        """Rimuove TUTTE le prenotazioni per l'intero gruppo."""
        try:
            _retry(self.base_ref.delete)
        except Exception as e:
            logger.error(f"[PrenotationManager] Errore clear_all_prenotations_for_group: {e}")


class DonationManager:

    def __init__(self, group_id: str, session_id: str = None):
        initialize_firebase()
        self.group_id = group_id
        self.session_id = session_id or 'current'
        self.base_ref = db.reference(f'donations/{self.group_id}/{self.session_id}')

    def record_donation(self, set_number: int, recipient_id: str, card_number: int, donor_id: str = None) -> None:
        """Registra una donazione per un utente ricevente in un set specifico."""
        value = donor_id if donor_id is not None else True
        try:
            _retry(
                self.base_ref.child(str(set_number)).child(recipient_id).child(str(card_number)).set,
                value
            )
        except Exception as e:
            logger.error(f"[DonationManager] Errore record_donation set={set_number} recipient={recipient_id}: {e}")
            raise

    def has_received_card(self, set_number: int, recipient_id: str, card_number: int) -> bool:
        """Verifica se un utente ha già ricevuto una specifica carta in un set."""
        try:
            return _retry(
                self.base_ref.child(str(set_number)).child(recipient_id).child(str(card_number)).get
            ) is not None
        except Exception:
            return False

    def clear_donations(self, set_number: int) -> None:
        """Rimuove tutte le donazioni per un set specifico."""
        try:
            _retry(self.base_ref.child(str(set_number)).delete)
        except Exception as e:
            logger.error(f"[DonationManager] Errore clear_donations set={set_number}: {e}")

    def clear_all_donations_for_group(self) -> None:
        """Rimuove TUTTE le donazioni per l'intero gruppo."""
        try:
            _retry(self.base_ref.delete)
        except Exception as e:
            logger.error(f"[DonationManager] Errore clear_all_donations_for_group: {e}")
    
    def get_set_donations(self, set_number: int) -> dict:
        """Recupera tutte le donazioni per un set specifico."""
        try:
            return _retry(self.base_ref.child(str(set_number)).get) or {}
        except Exception as e:
            logger.error(f"[DonationManager] Errore get_set_donations set={set_number}: {e}")
            return {}
