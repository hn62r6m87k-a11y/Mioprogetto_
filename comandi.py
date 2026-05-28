from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Chat
from telegram.error import TelegramError
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from typing import Optional
import logging
import asyncio

from utils import (
    admin_only,
    refresh_booking_message,
    find_admin_groups,
    sync_telegram_admins,
    get_active_booking_info,
    run_in_executor,
    async_retry,
    validate_booking_number,
    get_metrics,
    GRUPPI_AUTORIZZATI,
)
from bottoni import _safe_send_message, _safe_edit_message_text
from text import text_rules, text_assistenza
from firebase_utils import PrenotationManager, LeaderboardManager, AdminManager
from telegram.helpers import escape_markdown

logger = logging.getLogger(__name__)

# Timeout di una sessione di prenotazione utente, in secondi
BOOKING_TIMEOUT_SECONDS = 300

# Mappa comandi -> funzioni logiche per la selezione del gruppo
COMMAND_MAP = {}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

async def _resolve_target_user(target_str: str, context: ContextTypes.DEFAULT_TYPE) -> Optional[Chat]:
    """
    Risolve un utente tramite ID numerico o username Telegram.
    Restituisce l'oggetto Chat se trovato e valido, None altrimenti.
    """
    try:
        if target_str.lstrip('-').isdigit():
            chat = await asyncio.wait_for(context.bot.get_chat(int(target_str)), timeout=5)
        else:
            username = target_str if target_str.startswith('@') else f"@{target_str}"
            chat = await asyncio.wait_for(context.bot.get_chat(username), timeout=5)

        if getattr(chat, 'type', None) == 'private':
            return chat
        else:
            logger.warning(f"_resolve_target_user: '{target_str}' risolto ma non è un utente privato (type={chat.type}).")
            return None

    except asyncio.TimeoutError:
        logger.error(f"_resolve_target_user: timeout risolvendo '{target_str}'")
        return None
    except TelegramError as e:
        logger.error(f"_resolve_target_user: errore Telegram '{target_str}': {e}")
        return None
    except Exception as e:
        logger.error(f"_resolve_target_user: errore inatteso '{target_str}': {e}")
        return None

# ---------------------------------------------------------------------------
# Funzioni di base e gioco
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.message.from_user
    args = context.args
    logger.info(f"Args ricevuti da {user.id} in /start: {args}")

    if args and len(args) == 1 and args[0].startswith('rimuovi_'):
        try:
            parts = args[0].split('_', 2)
            if len(parts) != 3:
                raise ValueError("Formato non valido")
            _, set_str, group_str = parts
            set_number = int(set_str)
            group_id = group_str
            user_id = str(user.id)

            _, active_set = await get_active_booking_info(group_id)
            if active_set != set_number:
                await update.message.reply_text(
                    "⚠️ Impossibile annullare la prenotazione: le registrazioni per questo set sono già terminate o non sono attive."
                )
                return

            pm = PrenotationManager(group_id)
            removed = await async_retry(pm.remove_prenotation, set_number, user_id)
            if removed:
                await update.message.reply_text(f"✅ La tua prenotazione per il set {set_number} è stata rimossa con successo.")
                await refresh_booking_message(context, group_id)
            else:
                await update.message.reply_text(f"⚠️ Non risulti prenotato per il set {set_number}.")
        except (ValueError, IndexError):
            await update.message.reply_text("❌ Link non valido per la rimozione della prenotazione.")
        return

    if args:
        if args[0] == 'regole':
            await update.message.reply_text(text_rules, parse_mode=ParseMode.MARKDOWN_V2)
            return
        if args[0] == 'assistenza':
            await update.message.reply_text(text_assistenza)
            return

    if args and len(args) == 1 and args[0].startswith('prenotati_'):
        parts = args[0].split('_')
        if len(parts) == 3:
            _, set_str, group_str = parts
            args = ['prenotati', set_str, group_str]

    if len(args) == 3 and args[0] == 'prenotati':
        try:
            set_number = int(args[1])
            group_id = args[2]
        except ValueError:
            return

        context.user_data['booking'] = {
            'step': 1,
            'set_number': set_number,
            'group_id': group_id,
            'numbers': [],
            'created_at': asyncio.get_running_loop().time(),
        }
        await update.message.reply_text(
            f"*🔝 Prenotazione set {set_number}\\:*\n\n"
            "_🔽 Inserisci il numero di carte *bianche* mancanti\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    await update.message.reply_text(
        "Ciao! Sono il bot per la gestione del GiroSet. Interagisci con me nei gruppi autorizzati."
    )


async def handle_booking_numbers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    booking = context.user_data.get('booking')
    if not booking:
        return

    now = asyncio.get_running_loop().time()
    if now - booking.get('created_at', now) > BOOKING_TIMEOUT_SECONDS:
        context.user_data.pop('booking', None)
        await update.message.reply_text(
            "⌛️ La sessione di prenotazione è scaduta. Invia di nuovo il comando per ricominciare."
        )
        return

    num, error = validate_booking_number(update.message.text)
    if error:
        await update.message.reply_text(error)
        return

    booking['numbers'].append(num)

    if booking['step'] == 1:
        booking['step'] = 2
        await update.message.reply_text(
            "_🔽 Inserisci il numero di carte *oro* mancanti\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    set_number = booking['set_number']
    group_id = booking['group_id']
    numbers = booking['numbers']
    user_id = str(update.message.from_user.id)

    if len(numbers) != 2:
        context.user_data.pop('booking', None)
        await update.message.reply_text(
            "⚠️ Errore nella sequenza di inserimento. Invia di nuovo il comando di prenotazione."
        )
        return

    total_cards = sum(numbers)
    if total_cards < 1 or total_cards > 9:
        _, active_set = await get_active_booking_info(group_id)
        if active_set != set_number:
            await update.message.reply_text(
                "⚠️ La somma delle carte non è valida e le prenotazioni sono già chiuse. Prenotazione annullata."
            )
            context.user_data.pop('booking', None)
            return

        context.user_data['booking'] = {
            'step': 1,
            'set_number': set_number,
            'group_id': group_id,
            'numbers': [],
            'created_at': now,
        }
        await update.message.reply_text(
            f"⚠️ La somma delle carte deve essere tra 1 e 9 \\(hai inserito {numbers[0]} \\+ {numbers[1]} \\= {total_cards}\\)\\.\n\n"
            "_🔽 Reinserisci il numero di carte *bianche* mancanti\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    pm = PrenotationManager(group_id)
    await async_retry(pm.add_prenotation, set_number, user_id, numbers)

    await update.message.reply_text(
        f"*✅ Prenotazione per set {set_number} avvenuta con successo\\.*\n"
        f"Mancanti: {numbers[0]} bianche \\+ {numbers[1]} oro",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    context.user_data.pop('booking', None)
    from utils import increment_metric
    increment_metric('bookings_completed')
    await refresh_booking_message(context, group_id)


@admin_only
async def gira(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    topic_id = getattr(update.effective_message, "message_thread_id", None)

    # Creare un nuovo dizionario per giroset mantenendo dati non correlati
    giroset_data = {
        'giroset_topic_id': topic_id,
        'giroset_chiuso': False,
        'giroset_scores': {},
        'sessions': {},
        'active_set': None,
    }
    
    # Aggiornare solo le chiavi specifiche di giroset
    for key, value in giroset_data.items():
        context.chat_data[key] = value

    pm = PrenotationManager(str(chat_id))
    await async_retry(pm.clear_all_prenotations_for_group)

    logger.info(f"Sincronizzazione admin per il gruppo {chat_id}...")
    await sync_telegram_admins(str(chat_id), context)

    keyboard = [
        [InlineKeyboardButton("➕ Giro Set", callback_data='GiroSet')],
        [
            InlineKeyboardButton("ℹ️ Regole", url=f'https://t.me/{context.bot.username}?start=regole'),
            InlineKeyboardButton("🖋 Assistenza", url=f'https://t.me/{context.bot.username}?start=assistenza'),
        ],
    ]

    text = (
        '*_🆕 Benvenuto al GiroSet\\!_*\n\n'
        '*🆒 Qui potrai donare e ricevere carte gratuitamente\\. Le premesse sono buone, direi di cominciare\\.*\n\n'
        '_⚙️ Ah quasi dimenticavo, per qualunque dubbio premi sugli appositi bottoni o contatta un moderatore\\._'
    )

    await _safe_send_message(
        context.bot,
        chat_id=chat_id,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        message_thread_id=topic_id,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def visualizza_classifica(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    topic_id = context.chat_data.get(
        'giroset_topic_id',
        getattr(update.effective_message, "message_thread_id", None),
    )
    if not chat:
        logger.error("Impossibile determinare la chat in visualizza_classifica.")
        return

    group_id = str(chat.id)
    lm = LeaderboardManager(group_id)
    leaderboard = await async_retry(lm.get_leaderboard)

    if not leaderboard:
        await _safe_send_message(
            context.bot,
            chat_id=chat.id,
            text="La classifica è vuota.",
            message_thread_id=topic_id,
        )
        return

    sorted_leaderboard = sorted(leaderboard.items(), key=lambda item: item[1], reverse=True)

    group_title = chat.title if chat.title else "Gruppo"
    heading_name = escape_markdown(group_title, version=2)
    text = f"*🏆 Classifica Generale per {heading_name} 🏆*\n\n"
    visible_count = 0

    for user_id, score in sorted_leaderboard:
        display_name = await _get_username_for_leaderboard(user_id, context.bot)
        visible_count += 1
        text += f"{visible_count}\\. {display_name}\\: *{score}* punti\n"

    await _safe_send_message(
        context.bot,
        chat_id=chat.id,
        text=text,
        message_thread_id=topic_id,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _get_username_for_leaderboard(user_id: str, bot) -> str:
    try:
        chat_user = await asyncio.wait_for(bot.get_chat(user_id), timeout=3)
        if getattr(chat_user, 'username', None):
            return escape_markdown(f"@{chat_user.username}", version=2)

        display_name_parts = []
        if getattr(chat_user, 'first_name', None):
            display_name_parts.append(chat_user.first_name)
        if getattr(chat_user, 'last_name', None):
            display_name_parts.append(chat_user.last_name)
        if display_name_parts:
            return escape_markdown(" ".join(display_name_parts), version=2)
    except TelegramError as e:
        if isinstance(user_id, str) and user_id.startswith('@'):
            return escape_markdown(user_id, version=2)
        logger.warning(f"Errore ottenendo username utente {user_id}: {e}")
    except Exception as e:
        logger.warning(f"Errore ottenendo username utente {user_id}: {e}")

    return escape_markdown(str(user_id), version=2)


# ---------------------------------------------------------------------------
# Logica per comandi admin globali
# ---------------------------------------------------------------------------

async def _execute_admin_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, logic_func, command_name: str
):
    chat = update.effective_chat
    if chat.type != 'private':
        await update.message.reply_text("⚠️ Questo comando è disponibile solo in chat privata con il bot.")
        return

    user_id = update.effective_user.id
    admin_groups = await find_admin_groups(user_id, context)
    if not admin_groups:
        await update.message.reply_text("⚠️ Non sei amministratore in nessun gruppo autorizzato.")
        return

    # Filtra solo i gruppi che sono ancora in GRUPPI_AUTORIZZATI (doppia verifica sicurezza)
    valid_groups = [gid for gid in admin_groups if int(gid) in GRUPPI_AUTORIZZATI]
    if not valid_groups:
        await update.message.reply_text("⚠️ Non sei amministratore in nessun gruppo autorizzato.")
        return

    if len(valid_groups) == 1:
        context.args = [valid_groups[0]] + context.args
        await logic_func(update, context)
    else:
        keyboard = []
        for gid in valid_groups:
            # Recupera il nome del gruppo; usa fallback se get_chat fallisce
            try:
                group_chat = await context.bot.get_chat(gid)
                group_name = group_chat.title or f"Gruppo {gid}"
            except Exception:
                group_name = f"Gruppo {gid}"
            # keyboard.append SEMPRE eseguito, indipendentemente da get_chat
            keyboard.append([InlineKeyboardButton(group_name, callback_data=f"select_group_{gid}")])

        if not keyboard:
            await update.message.reply_text("⚠️ Nessun gruppo disponibile. Riprova tra qualche secondo.")
            return

        await update.message.reply_text(
            "Seleziona il gruppo in cui eseguire il comando:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        context.user_data['pending_action'] = {'command': command_name, 'args': context.args}


async def handle_group_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    raw_group_id = query.data[len("select_group_"):]

    # Sicurezza: verifica che il group_id estratto dal callback sia autorizzato
    try:
        if int(raw_group_id) not in GRUPPI_AUTORIZZATI:
            await _safe_edit_message_text(query, "❌ Gruppo non autorizzato.")
            return
    except ValueError:
        await _safe_edit_message_text(query, "❌ ID gruppo non valido.")
        return

    group_id = raw_group_id
    pending_action = context.user_data.pop('pending_action', None)
    if not pending_action:
        await _safe_edit_message_text(query, "❌ Azione scaduta o non trovata. Riprova a inviare il comando.")
        return

    command_name = pending_action['command']
    original_args = pending_action['args']
    context.args = [group_id] + original_args

    group_name = "gruppo selezionato"
    for row in query.message.reply_markup.inline_keyboard:
        for button in row:
            if button.callback_data == query.data:
                group_name = button.text
                break

    await _safe_edit_message_text(
        query,
        f"✅ Ottimo\\! Eseguo il comando nel gruppo *{escape_markdown(group_name, version=2)}*\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    callback_func = COMMAND_MAP.get(command_name)
    if callback_func:
        await callback_func(update, context)


async def _add_admin_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id, target_user_str = context.args[0], context.args[1]

    target_user = await _resolve_target_user(target_user_str, context)
    if not target_user:
        await update.message.reply_text(
            f"❌ Utente '{target_user_str}' non trovato. Usa l'ID numerico o @username."
        )
        return

    if target_user.username and target_user.username.lower().endswith('bot'):
        await update.message.reply_text("❌ Non puoi aggiungere un bot come admin.")
        return

    am = AdminManager(group_id)
    await async_retry(am.add_admin, str(target_user.id))
    esc_username = escape_markdown(target_user.username or target_user.first_name, version=2)
    await update.message.reply_text(
        f"✅ Utente {esc_username} aggiunto agli admin del bot per il gruppo `{group_id}`."
    )


async def _remove_admin_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id, target_user_str = context.args[0], context.args[1]

    target_user = await _resolve_target_user(target_user_str, context)
    if not target_user:
        await update.message.reply_text(
            f"❌ Utente '{target_user_str}' non trovato. Usa l'ID numerico o @username."
        )
        return

    am = AdminManager(group_id)
    await async_retry(am.remove_admin, str(target_user.id))
    esc_username = escape_markdown(target_user.username or target_user.first_name, version=2)
    await update.message.reply_text(
        f"✅ Utente {esc_username} rimosso dagli admin del bot per il gruppo `{group_id}`."
    )


async def _add_points_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _internal_modify_points(update, context, sign=1)


async def _remove_points_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _internal_modify_points(update, context, sign=-1)


async def _internal_modify_points(update: Update, context: ContextTypes.DEFAULT_TYPE, sign: int):
    """Funzione logica interna e unificata per la modifica dei punti."""
    reply_to_message = update.message if update.message else update.callback_query.message
    group_id, target_user_str, points_str = context.args[0], context.args[1], context.args[2]

    # Normalizza username in lowercase (gli username Telegram sono case-insensitive)
    if not target_user_str.lstrip("-").isdigit():
        target_user_str = target_user_str.lower()

    # Prima prova a risolvere l'utente via Telegram (ID o @username).
    target_user = await _resolve_target_user(target_user_str, context)

    # Se non è risolvibile come utente Telegram, accettiamo comunque l'input
    # come username (es. @username) o come ID numerico e lo usiamo come chiave
    # nella classifica. Questo permette di aggiungere punti anche se l'utente
    # non ha mai avviato il bot.
    if target_user:
        target_key = str(target_user.id)
        display_name = target_user.username or target_user.first_name or target_key
        # Evita bot espliciti
        if getattr(target_user, 'username', None) and target_user.username.lower().endswith('bot'):
            await reply_to_message.reply_text("❌ Non puoi modificare punti per un bot.")
            return
    else:
        # Fallback: accetta '@username' oppure id numerico stringa
        if target_user_str.lstrip('-').isdigit():
            target_key = target_user_str
            display_name = target_key
        else:
            # Normalizza l'username per garantire il prefisso @ in lowercase
            target_key = target_user_str if target_user_str.startswith('@') else f"@{target_user_str}"
            display_name = target_key

    # --- Deduplicazione classifica ---
    # Se target_key è un ID numerico, controlla se esiste già in classifica una chiave
    # @username che corrisponde a quell'utente (aggiunto manualmente in passato) e
    # consolidala: somma i punti sulla chiave numerica e rimuove quella @username.
    # Se invece target_key è un @username, cerca se esiste già una chiave numerica
    # nella classifica il cui username Telegram (da get_chat) coincide — e usa quella.
    lm = LeaderboardManager(group_id)
    try:
        existing_leaderboard = await async_retry(lm.get_leaderboard)
    except Exception:
        existing_leaderboard = {}

    if target_key.lstrip('-').isdigit():
        # target è ID numerico: cerca chiavi @username duplicate da fondere
        try:
            tg_user_check = await asyncio.wait_for(context.bot.get_chat(int(target_key)), timeout=5)
            tg_username_lower = (tg_user_check.username or '').lower()
        except Exception:
            tg_username_lower = ''

        if tg_username_lower:
            at_key = f"@{tg_username_lower}"
            if at_key in existing_leaderboard:
                orphan_score = existing_leaderboard[at_key]
                logger.info(
                    f"[Dedup] Fusione {at_key} ({orphan_score}pt) → {target_key} per gruppo {group_id}"
                )
                try:
                    # Somma i punti orfani sulla chiave numerica
                    await async_retry(lm.increment_score, target_key, orphan_score)
                    # Rimuove la chiave @username orfana
                    await async_retry(lm.ref.child(at_key).delete)
                except Exception as e:
                    logger.warning(f"[Dedup] Errore fusione {at_key} → {target_key}: {e}")
    else:
        # target è @username: cerca se esiste già una chiave numerica con lo stesso username
        username_bare = target_key.lstrip('@').lower()
        for existing_key in list(existing_leaderboard.keys()):
            if existing_key.lstrip('-').isdigit():
                try:
                    tg_user_check = await asyncio.wait_for(
                        context.bot.get_chat(int(existing_key)), timeout=5
                    )
                    if (tg_user_check.username or '').lower() == username_bare:
                        # Trovata corrispondenza: usa la chiave numerica esistente
                        logger.info(
                            f"[Dedup] @{username_bare} risolto alla chiave numerica {existing_key} "
                            f"per gruppo {group_id}"
                        )
                        target_key = existing_key
                        display_name = tg_user_check.username or tg_user_check.first_name or existing_key
                        break
                except Exception:
                    continue

    # Sicurezza: non permettere di usare l'ID del gruppo come chiave utente
    if str(target_key) == str(group_id):
        logger.error(f"Tentativo di modificare punti usando l'ID del gruppo come ID utente: {group_id}")
        await reply_to_message.reply_text("❌ Errore interno: l'ID utente non può coincidere con quello del gruppo.")
        return

    try:
        points = int(points_str)
        # VALIDATION: Punti devono essere tra 1 e 10000 (anti-bomba)
        if points <= 0 or points > 10000:
            raise ValueError
    except ValueError:
        await reply_to_message.reply_text("⚠️ I punti devono essere tra 1 e 10000.")
        return

    points_to_modify = points * sign

    logger.info(f"Modifica Punti: Gruppo={group_id}, UtenteKey={target_key}, Punti={points_to_modify}")

    esc_username = escape_markdown(display_name, version=2)
    action_text = "Aggiunti" if sign == 1 else "Rimossi"
    clamped_to_zero = False
    new_score = 0
    is_new_user = False

    try:
        new_score, is_new_user = await async_retry(lm.increment_score, str(target_key), points_to_modify)
    except ValueError as e:
        err_str = str(e)
        if err_str.startswith("punteggio_azzerato:"):
            # La sottrazione avrebbe portato sotto zero: punteggio azzerato a 0
            clamped_to_zero = True
            try:
                new_score = int(err_str.split(":", 1)[1])
            except (IndexError, ValueError):
                new_score = 0
        else:
            await reply_to_message.reply_text(f"⚠️ Operazione fallita: {err_str}")
            return
    except Exception as e:
        logger.warning(f"Tentativo di modifica punteggi fallito: {e}")
        await reply_to_message.reply_text(f"⚠️ Operazione fallita: {str(e)}")
        return

    # Costruzione messaggio di conferma
    new_user_note = " \\(nuovo utente, inizializzato a 0\\)" if is_new_user else ""
    esc_group = escape_markdown(str(group_id), version=2)

    if clamped_to_zero:
        await reply_to_message.reply_text(
            f"⚠️ *Operazione completata nel gruppo `{esc_group}`\\.*\n"
            f"*{action_text} {points}* punti a {esc_username}{new_user_note}\\.\n"
            f"Il punteggio sarebbe diventato negativo: azzerato a *0*\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await reply_to_message.reply_text(
            f"✅ *Operazione completata nel gruppo `{esc_group}`\\.*\n"
            f"*{action_text} {points}* punti a {esc_username}{new_user_note}\\.\n"
            f"Nuovo punteggio\\: *{new_score}*",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


# ---------------------------------------------------------------------------
# Comandi esposti al bot
# ---------------------------------------------------------------------------

async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1:
        await update.message.reply_text("⚠️ Uso: `/addadmin <user_id o username>`")
        return
    await _execute_admin_command(update, context, _add_admin_logic, '/addadmin')


async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1:
        await update.message.reply_text("⚠️ Uso: `/removeadmin <user_id o username>`")
        return
    await _execute_admin_command(update, context, _remove_admin_logic, '/removeadmin')


async def add_points(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 2:
        await update.message.reply_text("⚠️ Uso: `/addpoints <user_id o username> <punti>`")
        return
    await _execute_admin_command(update, context, _add_points_logic, '/addpoints')


async def remove_points(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 2:
        await update.message.reply_text("⚠️ Uso: `/removepoints <user_id o username> <punti>`")
        return
    await _execute_admin_command(update, context, _remove_points_logic, '/removepoints')


async def _visualizza_classifica_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id = context.args[0]
    lm = LeaderboardManager(group_id)
    leaderboard = await async_retry(lm.get_leaderboard)

    reply_to_message = update.message if update.message else update.callback_query.message

    if not leaderboard:
        await reply_to_message.reply_text(f"La classifica per il gruppo {group_id} è vuota.")
        return

    sorted_leaderboard = sorted(leaderboard.items(), key=lambda item: item[1], reverse=True)

    group_title = None
    try:
        group_chat = await context.bot.get_chat(group_id)
        group_title = group_chat.title
    except Exception:
        group_title = None

    heading_name = escape_markdown(group_title, version=2) if group_title else "Gruppo"
    text = f"*🏆 Classifica Generale per {heading_name} 🏆*\n\n"
    visible_count = 0

    for user_id, score in sorted_leaderboard:
        display_name = await _get_username_for_leaderboard(user_id, context.bot)
        visible_count += 1
        text += f"{visible_count}\\. {display_name}\\: *{score}* punti\n"

    await reply_to_message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def classigira_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 0:
        await update.message.reply_text("⚠️ Uso: `/classigira` (nessun argomento)")
        return
    await _execute_admin_command(update, context, _visualizza_classifica_logic, '/classigira')


async def _reset_leaderboard_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id = context.args[0]
    lm = LeaderboardManager(group_id)
    await async_retry(lm.reset_leaderboard)
    reply_to_message = update.message if update.message else update.callback_query.message
    await reply_to_message.reply_text(f"✅ Classifica azzerata per il gruppo {escape_markdown(str(group_id), version=2)}.")


async def reset_leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 0:
        await update.message.reply_text("⚠️ Uso: `/resetclassigira` (nessun argomento)")
        return
    await _execute_admin_command(update, context, _reset_leaderboard_logic, '/resetclassigira')


@admin_only
async def metrics_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mostra metriche di sistema per admin."""
    metrics = get_metrics()
    text = "*📊 Metriche Sistema*\n\n" + "\n".join(f"{k}: {v}" for k, v in metrics.items())
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


COMMAND_MAP.update({
    '/addadmin': _add_admin_logic,
    '/removeadmin': _remove_admin_logic,
    '/addpoints': _add_points_logic,
    '/removepoints': _remove_points_logic,
    '/resetclassigira': _reset_leaderboard_logic,
    '/classigira': _visualizza_classifica_logic,
})
