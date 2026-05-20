import asyncio
import logging
import os
import threading
import time
from functools import wraps, partial

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes
from firebase_utils import SettingsManager, PrenotationManager, AdminManager
from firebase_admin import db

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Metriche semplici
# ---------------------------------------------------------------------------

_metrics = {
    'firebase_calls': 0,
    'firebase_errors': 0,
    'bookings_completed': 0,
}

def increment_metric(key: str):
    """Incrementa una metrica."""
    _metrics[key] = _metrics.get(key, 0) + 1

def get_metrics() -> dict:
    """Restituisce le metriche correnti."""
    return _metrics.copy()

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

DEFAULT_TIMER_DURATION_SECONDS = 30

# Nessuna lista di gruppi autorizzati: il bot funziona in qualsiasi gruppo
# e mantiene dati separati per ciascuno tramite il group_id su Firebase.
GRUPPI_AUTORIZZATI: set[int] = set()  # Tenuto per compatibilità import, non usato

DEFAULT_CARD_VALUES = [
    [1, 1, 1, 1, 1, 1, 1, 1, 1], [1, 1, 1, 1, 1, 1, 1, 1, 2],
    [1, 1, 1, 1, 1, 1, 1, 2, 2], [1, 1, 1, 1, 1, 1, 2, 2, 2],
    [1, 1, 1, 1, 1, 1, 2, 2, 2], [1, 1, 1, 1, 1, 2, 2, 2, 2],
    [1, 1, 1, 2, 2, 2, 2, 2, 2], [1, 1, 2, 2, 2, 2, 2, 2, 3],
    [1, 2, 2, 2, 2, 2, 2, 3, 3], [1, 2, 2, 2, 2, 2, 3, 3, 3],
    [2, 2, 2, 2, 3, 3, 3, 3, 3], [2, 2, 2, 3, 3, 3, 3, 3, 4],
    [2, 2, 3, 3, 3, 3, 3, 4, 4], [2, 2, 3, 3, 3, 3, 3, 4, 4],
    [2, 3, 3, 3, 3, 3, 3, 4, 4], [3, 3, 3, 3, 3, 4, 4, 4, 5],
    [3, 3, 3, 3, 3, 4, 4, 4, 5], [3, 3, 3, 3, 3, 4, 4, 4, 5],
    [4, 4, 4, 4, 4, 4, 4, 4, 6], [4, 4, 4, 4, 4, 4, 4, 4, 6],
    [4, 4, 4, 4, 4, 4, 4, 4, 6], [5, 5, 5, 5, 5, 5, 5, 5, 6],
    [5, 5, 5, 5, 5, 5, 5, 5, 6], [5, 5, 5, 5, 5, 5, 5, 5, 6],
]

DEFAULT_CARD_POINTS = {"1": 2, "2": 4, "3": 6, "4": 9, "5": 13, "6": 20}

# ---------------------------------------------------------------------------
# Cache in-memory con TTL per le impostazioni
# ---------------------------------------------------------------------------

_CACHE_TTL = 60  # secondi

# Struttura: { group_id: { 'data': {...}, 'ts': float } }
_settings_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


def _cache_get(group_id: str):
    with _cache_lock:
        entry = _settings_cache.get(group_id)
        if entry and (time.monotonic() - entry['ts']) < _CACHE_TTL:
            return entry['data']
    return None


def _cache_set(group_id: str, data: dict):
    with _cache_lock:
        _settings_cache[group_id] = {'data': data, 'ts': time.monotonic()}


def invalidate_settings_cache(group_id: str):
    """
    Invalida la cache per un gruppo.
    Da chiamare ogni volta che si scrive un'impostazione con set_setting().
    """
    with _cache_lock:
        _settings_cache.pop(group_id, None)


def _get_all_settings_sync(group_id: str) -> dict:
    """
    Recupera TUTTE le impostazioni di un gruppo con cache TTL.
    Usa chiamate sincrone a Firebase quando viene invocata da codice non-async.
    """
    cached = _cache_get(group_id)
    if cached is not None:
        return cached
    sm = SettingsManager(group_id)
    data = sm.get_all()
    _cache_set(group_id, data)
    return data

async def get_all_settings(group_id: str) -> dict:
    """Versione asincrona di _get_all_settings, sicura per l'event loop."""
    cached = _cache_get(group_id)
    if cached is not None:
        return cached
    sm = SettingsManager(group_id)
    data = await async_retry(sm.get_all)
    _cache_set(group_id, data)
    return data

# ---------------------------------------------------------------------------
# Helper asincrono: esegui chiamate Firebase bloccanti nel thread pool
# ---------------------------------------------------------------------------

async def run_in_executor(func, *args, **kwargs):
    """
    Esegue una funzione sincrona bloccante nel thread pool di default,
    senza bloccare l'event loop di asyncio.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


async def async_retry(func, *args, retries: int = 3, delay: float = 1.0, timeout: float = 10.0, **kwargs):
    """
    Riprova una funzione bloccante nel thread pool con backoff esponenziale.
    Applica un timeout alle chiamate Firebase per evitare blocchi indefiniti.
    """
    increment_metric('firebase_calls')
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return await asyncio.wait_for(run_in_executor(func, *args, **kwargs), timeout=timeout)
        except asyncio.TimeoutError as e:
            increment_metric('firebase_errors')
            last_exc = e
            logger.warning(f"[AsyncRetry] Timeout dopo {timeout}s per {getattr(func, '__name__', repr(func))}")
        except Exception as e:
            increment_metric('firebase_errors')
            last_exc = e
            logger.warning(f"[AsyncRetry] Tentativo {attempt}/{retries} fallito per {getattr(func, '__name__', repr(func))}: {e}")
        if attempt < retries:
            await asyncio.sleep(delay * attempt)
    logger.error(f"[AsyncRetry] Tutti i tentativi falliti per {getattr(func, '__name__', repr(func))}: {last_exc}")
    raise last_exc

# ---------------------------------------------------------------------------
# Validazione input utente
# ---------------------------------------------------------------------------

MAX_BOOKING_NUMBER = 9
MAX_SINGLE_NUMBER = 9


def validate_booking_number(text: str) -> tuple[int | None, str | None]:
    """
    Valida un singolo numero intero inserito dall'utente durante la prenotazione.
    Ritorna (numero, None) se valido, (None, messaggio_errore) altrimenti.
    """
    text = text.strip()
    if len(text) > 10:
        return None, "Input troppo lungo."
    try:
        num = int(text)
    except ValueError:
        return None, "Inserisci un numero valido."
    if num < 0 or num > MAX_SINGLE_NUMBER:
        return None, f"Inserisci un numero compreso tra 0 e {MAX_SINGLE_NUMBER}."
    return num, None

# ---------------------------------------------------------------------------
# Decoratori
# ---------------------------------------------------------------------------

def authorized_group_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        chat = update.effective_chat
        if not chat or chat.type not in ('group', 'supergroup'):
            if update.message:
                await update.message.reply_text("Questo comando è disponibile solo nei gruppi.")
            else:
                logger.warning("Chat non trovato in authorized_group_only")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


def admin_only(func):
    """Decorator per comandi riservati agli amministratori di Telegram."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        chat = update.effective_chat
        user = update.effective_user
        # Valido solo nei gruppi
        if not chat or chat.type not in ('group', 'supergroup'):
            if update.message:
                await update.message.reply_text("❌ Questo comando è disponibile solo nei gruppi.")
            return
        try:
            member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user.id)
            if member.status not in ('administrator', 'creator'):
                if update.message:
                    await update.message.reply_text("❌ Accesso negato. Comando riservato agli amministratori del gruppo.")
                return
        except TelegramError as e:
            if "User is not a member" in str(e):
                if update.message:
                    await update.message.reply_text("❌ Non sei membro di questo gruppo.")
            else:
                if update.message:
                    await update.message.reply_text("⚠️ Errore temporaneo nella verifica. Riprova.")
            logger.error(f"Errore Telegram in admin_only: {e}")
            return
        except Exception as e:
            logger.error(f"Errore inatteso in admin_only: {e}")
            if update.message:
                await update.message.reply_text("⚠️ Errore interno.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


def custom_admin_only(func):
    """
    Decorator per comandi riservati agli admin del bot (dalla lista custom)
    o agli amministratori di Telegram.
    """
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        chat = update.effective_chat
        user = update.effective_user
        if not chat or chat.id == user.id:
            return
        try:
            member = await context.bot.get_chat_member(chat_id=chat.id, user_id=user.id)
            if member.status in ('administrator', 'creator'):
                return await func(update, context, *args, **kwargs)
        except TelegramError as e:
            logger.error(f"Errore verifica permessi admin Telegram: {e}")
            await update.message.reply_text("⚠️ Impossibile verificare i permessi di Telegram.")
            return
        except Exception as e:
            logger.error(f"Errore inatteso in custom_admin_only: {e}")
            return
        
        am = AdminManager(str(chat.id))
        try:
            if await async_retry(am.is_admin, str(user.id)):
                return await func(update, context, *args, **kwargs)
        except Exception as e:
            logger.error(f"Errore verifica admin custom: {e}")
        
        await update.message.reply_text("❌ Accesso negato. Permessi insufficienti.")
        return    
    return wrapper

# ---------------------------------------------------------------------------
# Funzioni di stato per il messaggio di booking (Firebase)
# ---------------------------------------------------------------------------

def _get_group_state_ref(group_id: str):
    return db.reference(f'group_states/{group_id}')


async def set_active_booking_info(group_id: str, message_id: int, set_number: int):
    """Salva le info del messaggio di prenotazione attivo."""
    ref = _get_group_state_ref(group_id)
    await async_retry(ref.set, {
        'message_id': message_id,
        'set_number': set_number,
    })


async def get_active_booking_info(group_id: str) -> tuple[int | None, int | None]:
    """Recupera le info del messaggio di prenotazione attivo."""
    try:
        ref = _get_group_state_ref(group_id)
        state = await async_retry(ref.get)
        if state and isinstance(state, dict):
            return state.get('message_id'), state.get('set_number')
    except Exception as e:
        logger.error(f"Errore get_active_booking_info gruppo {group_id}: {e}")
    return None, None


async def clear_active_booking_info(group_id: str):
    """Pulisce le info del messaggio di prenotazione attivo."""
    try:
        ref = _get_group_state_ref(group_id)
        await async_retry(ref.delete)
    except Exception as e:
        logger.error(f"Errore clear_active_booking_info gruppo {group_id}: {e}")
# ---------------------------------------------------------------------------
# Aggiornamento messaggio di booking
# ---------------------------------------------------------------------------

async def refresh_booking_message(context: ContextTypes.DEFAULT_TYPE, group_id: str):
    """
    Legge lo stato corrente (message_id, set_number) e aggiorna il messaggio
    con il conteggio aggiornato dei prenotati.
    """
    message_id, set_number = await get_active_booking_info(group_id)
    if not message_id or not set_number:
        logger.warning(f"Nessun messaggio di booking attivo trovato per il gruppo {group_id}.")
        return

    pm = PrenotationManager(group_id)
    num_bookings = len(await async_retry(pm.get_prenotations, set_number))

    text, reply_markup = await get_booking_message_components(
        group_id=group_id,
        set_number=set_number,
        num_bookings=num_bookings,
        bot_username=context.bot.username,
    )

    try:
        await context.bot.edit_message_text(
            chat_id=group_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as e:
        logger.error(f"Impossibile aggiornare il contatore di booking per il gruppo {group_id}: {e}")


async def get_booking_message_components(
    group_id: str,
    set_number: int,
    num_bookings: int,
    bot_username: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """Costruisce il testo e la tastiera per il messaggio di prenotazione."""
    timer_active = await is_booking_timer_active(group_id)

    url_prenotati = f'https://t.me/{bot_username}?start=prenotati_{set_number}_{group_id}'
    url_rimuovi = f'https://t.me/{bot_username}?start=rimuovi_{set_number}_{group_id}'

    keyboard_buttons = [
        [
            InlineKeyboardButton("➕ Prenotati", url=url_prenotati),
            InlineKeyboardButton("➖ Rimuovi Prenotazione", url=url_rimuovi),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data='back')],
        [
            InlineKeyboardButton("ℹ️ Regole", url=f'https://t.me/{bot_username}?start=regole'),
            InlineKeyboardButton("🖋 Assistenza", url=f'https://t.me/{bot_username}?start=assistenza'),
        ],
    ]

    text = f"*▶️ Set {set_number}*\n\n"

    if not timer_active:
        keyboard_buttons.insert(2, [InlineKeyboardButton("🔒 Termina Prenotazioni", callback_data='Termina prenotazioni')])
        text += "_🆗 Prenotazioni aperte, premete l'apposito bottone se vi mancano carte per questo set\\._"
    else:
        duration_seconds = await get_booking_timer_duration(group_id)
        duration_text = format_duration_seconds(duration_seconds)
        text += (
            f"_🆗 Prenotazioni aperte per *{duration_text}*\\! "
            "Allo scadere del tempo, verranno chiuse automaticamente\\._"
        )

    if num_bookings > 0:
        text += f"\n\n*✅ {num_bookings} utente/i prenotato/i\\.*"

    return text, InlineKeyboardMarkup(keyboard_buttons)

# ---------------------------------------------------------------------------
# Funzioni admin helper
# ---------------------------------------------------------------------------

async def find_admin_groups(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    """
    Trova tutti i gruppi in cui il bot è presente e l'utente è admin.
    I gruppi vengono tracciati automaticamente in bot_data['known_groups']
    ogni volta che il bot riceve un messaggio da un gruppo.
    Restituisce una lista di ID di gruppo (come stringhe).
    """
    admin_in_groups = []
    known_groups = list(context.bot_data.get('known_groups', set()))
    for group_id in known_groups:
        try:
            member = await context.bot.get_chat_member(chat_id=group_id, user_id=user_id)
            if member.status in ('administrator', 'creator'):
                admin_in_groups.append(str(group_id))
        except Exception:
            continue
    return admin_in_groups


async def sync_telegram_admins(group_id: str, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Sincronizza la lista degli admin di Firebase con quella di Telegram per un gruppo.
    Restituisce il numero di admin sincronizzati.
    """
    am = AdminManager(group_id)
    count = 0
    try:
        tg_admins = await context.bot.get_chat_administrators(group_id)
        if not isinstance(tg_admins, (list, tuple)):
            logger.error(f"get_chat_administrators ritornò {type(tg_admins)}, non iterabile")
            return 0
        
        for admin in tg_admins:
            try:
                if admin.user and not admin.user.is_bot:
                    await async_retry(am.add_admin, str(admin.user.id))
                    count += 1
            except Exception as e:
                logger.warning(f"Errore aggiungendo admin {getattr(admin.user, 'id', 'unknown')}: {e}")
                continue
        return count
    except TelegramError as e:
        logger.error(f"Errore Telegram sincronizzazione admin per {group_id}: {e}")
        return 0
    except Exception as e:
        logger.error(f"Errore inatteso in sync_telegram_admins: {e}")
        return 0

# ---------------------------------------------------------------------------
# Funzioni helper — lettura impostazioni con cache
# ---------------------------------------------------------------------------

def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ('true', '1', 'yes', 'y', 'on'):
            return True
        if normalized in ('false', '0', 'no', 'n', 'off'):
            return False
    return default


def format_duration_seconds(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    if minutes and secs:
        minute_unit = "minuto" if minutes == 1 else "minuti"
        second_unit = "secondo" if secs == 1 else "secondi"
        return f"{minutes} {minute_unit} {secs} {second_unit}"
    if minutes:
        minute_unit = "minuto" if minutes == 1 else "minuti"
        return f"{minutes} {minute_unit}"
    second_unit = "secondo" if secs == 1 else "secondi"
    return f"{secs} {second_unit}"


async def get_card_values(group_id: str) -> list:
    """Recupera i valori delle carte per il gruppo, con cache e validazione."""
    settings = await get_all_settings(group_id)
    card_values = settings.get('card_values', DEFAULT_CARD_VALUES)

    if (
        not isinstance(card_values, list)
        or any(
            not isinstance(row, list)
            or len(row) != 9
            or any(not isinstance(v, int) for v in row)
            for row in card_values
        )
    ):
        logger.warning(f"Formato non valido 'card_values' gruppo {group_id}. Reimposto al default.")
        sm = SettingsManager(group_id)
        await async_retry(sm.set_setting, 'card_values', DEFAULT_CARD_VALUES)
        invalidate_settings_cache(group_id)
        return DEFAULT_CARD_VALUES

    num_sets = _safe_int(settings.get('num_sets', len(card_values)), len(card_values))
    if num_sets > len(card_values):
        extra_sets = [DEFAULT_CARD_VALUES[0].copy() for _ in range(num_sets - len(card_values))]
        card_values = [*card_values, *extra_sets]
        sm = SettingsManager(group_id)
        await async_retry(sm.set_setting, 'card_values', card_values)
        invalidate_settings_cache(group_id)

    return card_values


async def get_card_points(group_id: str) -> dict:
    """Recupera i punteggi per il gruppo, con cache e validazione."""
    settings = await get_all_settings(group_id)
    card_points = settings.get('card_points', DEFAULT_CARD_POINTS)

    if not isinstance(card_points, dict):
        logger.warning(f"Formato non valido 'card_points' gruppo {group_id}. Reimposto al default.")
        sm = SettingsManager(group_id)
        await async_retry(sm.set_setting, 'card_points', DEFAULT_CARD_POINTS)
        invalidate_settings_cache(group_id)
        return DEFAULT_CARD_POINTS

    normalized = {}
    changed = False
    for key, value in card_points.items():
        try:
            normalized[str(key)] = int(value)
        except (TypeError, ValueError):
            normalized[str(key)] = DEFAULT_CARD_POINTS.get(str(key), 0)
            changed = True

    if changed:
        sm = SettingsManager(group_id)
        await async_retry(sm.set_setting, 'card_points', normalized)
        invalidate_settings_cache(group_id)

    return normalized


async def get_num_sets(group_id: str) -> int:
    settings = await get_all_settings(group_id)
    default_num = len(await get_card_values(group_id))
    return max(1, _safe_int(settings.get('num_sets', default_num), default_num))


async def is_booking_timer_active(group_id: str) -> bool:
    settings = await get_all_settings(group_id)
    return _safe_bool(settings.get('booking_timer_active', False), False)


async def get_booking_timer_duration(group_id: str) -> int:
    settings = await get_all_settings(group_id)
    duration = _safe_int(settings.get('booking_timer_duration'), DEFAULT_TIMER_DURATION_SECONDS)
    return max(30, duration)
