import asyncio
import logging
import uuid
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from firebase_utils import PrenotationManager, LeaderboardManager, DonationManager, AdminManager
from settings import handle_settings_callback
from text import text_rules, text_assistenza
from utils import (
    get_card_values,
    get_card_points,
    get_num_sets,
    is_booking_timer_active,
    get_booking_timer_duration,
    get_booking_message_components,
    set_active_booking_info,
    clear_active_booking_info,
    run_in_executor,
    async_retry,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

async def _check_admin(group_id: str, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Verifica se l'utente è amministratore (Firebase o Telegram)."""
    is_admin = False
    am = AdminManager(group_id)
    if await async_retry(am.is_admin, str(user_id)):
        is_admin = True
    else:
        try:
            member = await context.bot.get_chat_member(chat_id=group_id, user_id=user_id)
            if member.status in ('administrator', 'creator'):
                is_admin = True
        except Exception:
            pass
    return is_admin


# ---------------------------------------------------------------------------
# Dispatcher principale
# ---------------------------------------------------------------------------

async def _safe_edit_message_text(query: CallbackQuery, text: str, reply_markup=None, parse_mode: str = None):
    """Wrapper safe per edit_message_text con error handling. Ritorna message o None."""
    try:
        message = await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        return message
    except BadRequest as e:
        message = str(e).lower()
        if 'too old' in message or 'message not modified' in message:
            logger.warning(f"Impossibile modificare il messaggio (query scaduta o non modificato): {e}")
            return None
        logger.error(f"BadRequest in edit_message_text: {e}")
        return None
    except TelegramError as e:
        logger.error(f"Errore Telegram in edit_message_text: {e}")
        return None
    except Exception as e:
        logger.error(f"Errore inatteso in edit_message_text: {e}")
        return None


async def _safe_send_message(bot, chat_id: int, text: str, reply_markup=None, parse_mode: str = None, message_thread_id: int = None):
    """Wrapper safe per send_message con error handling. Ritorna message o None."""
    try:
        message = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            message_thread_id=message_thread_id
        )
        return message
    except TelegramError as e:
        logger.error(f"Errore Telegram in send_message a chat {chat_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Errore inatteso in send_message a chat {chat_id}: {e}")
        return None


async def _safe_delete_message(bot, chat_id: int, message_id: int) -> bool:
    """Wrapper safe per delete_message con error handling."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except BadRequest as e:
        if 'not found' in str(e).lower():
            logger.warning(f"Messaggio {message_id} non trovato in chat {chat_id} (già cancellato?)")
            return True  # Non è un errore se il messaggio non c'è
        logger.error(f"BadRequest in delete_message: {e}")
        return False
    except TelegramError as e:
        logger.error(f"Errore Telegram in delete_message: {e}")
        return False
    except Exception as e:
        logger.error(f"Errore inatteso in delete_message: {e}")
        return False


async def _safe_edit_message_reply_markup(bot, chat_id: int, message_id: int, reply_markup=None) -> bool:
    """Wrapper safe per edit_message_reply_markup con error handling."""
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)
        return True
    except BadRequest as e:
        if 'not found' in str(e).lower() or 'message not modified' in str(e).lower():
            logger.warning(f"Impossibile modificare i bottoni del messaggio {message_id}: {e}")
            return True
        logger.error(f"BadRequest in edit_message_reply_markup: {e}")
        return False
    except TelegramError as e:
        logger.error(f"Errore Telegram in edit_message_reply_markup: {e}")
        return False
    except Exception as e:
        logger.error(f"Errore inatteso in edit_message_reply_markup: {e}")
        return False


async def _safe_answer(query: CallbackQuery, text: str = None, show_alert: bool = False, cache_time: int = None) -> None:
    """Wrapper safe per query.answer con error handling per query scadute."""
    try:
        await query.answer(text=text, show_alert=show_alert, cache_time=cache_time)
    except BadRequest as e:
        message = str(e).lower()
        if 'too old' in message or 'query id is invalid' in message or 'response timeout expired' in message:
            logger.warning(f"Callback query ignorata perché scaduta o invalida: {e}")
            return
        logger.error(f"BadRequest in answer_callback_query: {e}")
    except TelegramError as e:
        logger.error(f"Errore Telegram durante answer_callback_query: {e}")
    except Exception as e:
        logger.error(f"Errore inatteso in answer_callback_query: {e}")


async def button_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data

    if data.startswith('settings_'):
        await _safe_answer(query)
        await handle_settings_callback(update, context)
        return

    await _safe_answer(query)

    if data == 'GiroSet':
        await handle_giroset(query, context)
    elif data == 'Avvia Giroset':
        await handle_avvio(query, context)
    elif data.startswith("set_"):
        if data == 'set_precedente':
            await handle_set_precedente(query, context)
        elif data == 'set_successivo':
            await handle_set_successivo(query, context)
        else:
            await handle_set(query, context)
    elif data == 'home':
        await handle_giroset(query, context)
    elif data == 'back':
        await handle_avvio(query, context)
    elif data == 'Termina prenotazioni':
        await handle_termina_prenotazioni(query, context)
    elif data == 'Termina Giroset':
        await handle_termina_giroset(update, context)
    elif data == 'Posta':
        await handle_posta(update, context)
    elif data == 'dona':
        await handle_dona(update, context)
    elif data.startswith("number_"):
        await handle_dona_carta(update, context)
    elif data.startswith("vai_"):
        await handle_vai(update, context)
    elif data.startswith("no_"):
        await handle_no(update, context)


# ---------------------------------------------------------------------------
# Gestori bottoni
# ---------------------------------------------------------------------------

async def handle_giroset(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    group_id = str(query.message.chat.id)
    user_id = query.from_user.id

    if not await _check_admin(group_id, user_id, context):
        await _safe_answer(query, "⚠️ Solo gli amministratori possono accedere al menu principale.", show_alert=True)
        return

    keyboard = [
        [InlineKeyboardButton("🆒 Avvia", callback_data='Avvia Giroset')],
        [
            InlineKeyboardButton("ℹ️ Regole", url=f'https://t.me/{context.bot.username}?start=regole'),
            InlineKeyboardButton("🖋 Assistenza", url=f'https://t.me/{context.bot.username}?start=assistenza'),
        ],
    ]
    await _safe_edit_message_text(
        query,
        "*🆙 Eccoci pronti ad iniziare, ultimo step, premi avvia se vuoi aprire le danze\\.*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def handle_avvio(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    group_id = str(query.message.chat.id)
    user_id = query.from_user.id

    if not await _check_admin(group_id, user_id, context):
        await _safe_answer(query, "⚠️ Solo gli amministratori possono avviare il Giroset.", show_alert=True)
        return

    context.chat_data['giroset_session_id'] = uuid.uuid4().hex
    num_sets = await get_num_sets(group_id)

    buttons = [InlineKeyboardButton(str(i), callback_data=f'set_{i}') for i in range(1, num_sets + 1)]
    keyboard = [buttons[i:i + 7] for i in range(0, len(buttons), 7)]
    keyboard.append([
        InlineKeyboardButton("🏠 Home", callback_data='home'),
        InlineKeyboardButton("🔚 Termina", callback_data='Termina Giroset'),
    ])
    keyboard.append([
        InlineKeyboardButton("ℹ️ Regole", url=f'https://t.me/{context.bot.username}?start=regole'),
        InlineKeyboardButton("🖋 Assistenza", url=f'https://t.me/{context.bot.username}?start=assistenza'),
    ])

    await _safe_edit_message_text(
        query,
        "_🔽 Guidati con la tastiera e decidi da dove cominciare, il resto verrà da sè\\._",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _create_end_booking_message(
    context: ContextTypes.DEFAULT_TYPE,
    group_id: str,
    current_set: int,
    topic_id: Optional[int] = None,
) -> tuple[InlineKeyboardMarkup, str]:
    pm = PrenotationManager(group_id)
    sorted_list = await async_retry(pm.get_sorted_prenotations, current_set)

    lines = []
    if sorted_list:
        for i, (uid, info) in enumerate(sorted_list, 1):
            try:
                chat = await context.bot.get_chat(uid)
                username = chat.username or chat.first_name
                esc_username = escape_markdown(username, version=2)
                counts = f"{info['numbers'][0]} \\+ {info['numbers'][1]}"
                lines.append(f"{i}\\. @{esc_username} {counts}")
            except Exception as e:
                logger.error(f"Impossibile ottenere info per l'utente {uid}: {e}")
                lines.append(f"{i}\\. Utente Sconosciuto \\({uid}\\)")

        booking_list_text = f"🛃 *Lista prenotati per il set {current_set}*\n\n" + "\n".join(lines)
        list_keyboard_buttons = [
            [InlineKeyboardButton("🔜 Posta", callback_data='Posta')],
            [InlineKeyboardButton("❌ Termina Giro Set", callback_data='Termina Giroset')],
        ]
        control_keyboard_buttons = list_keyboard_buttons
    else:
        booking_list_text = f"0️⃣ _Nessuna prenotazione completata per il set {current_set}\\._"
        list_keyboard_buttons = [
            [
                InlineKeyboardButton("⬅️ Set Precedente", callback_data='set_precedente'),
                InlineKeyboardButton("➡️ Set Successivo", callback_data='set_successivo'),
            ],
            [InlineKeyboardButton("❌ Termina Giro Set", callback_data='Termina Giroset')],
        ]
        control_keyboard_buttons = list_keyboard_buttons

    sent = await _safe_send_message(
        context.bot,
        chat_id=int(group_id),
        text=booking_list_text,
        reply_markup=InlineKeyboardMarkup(list_keyboard_buttons),
        parse_mode=ParseMode.MARKDOWN_V2,
        message_thread_id=topic_id,
    )

    if sent:
        app = context.application
        chat_data = app.chat_data.setdefault(int(group_id), {})
        chat_data['booking_list_message_id'] = sent.message_id
    else:
        logger.warning(f"Impossibile inviare lista prenotazioni per set {current_set}")


    control_text = "🛑 *Prenotazioni terminate\\!*\n\n_Ora si può procedere con la donazione delle carte\\._"
    return InlineKeyboardMarkup(control_keyboard_buttons), control_text


async def _cancel_booking_timer(context: ContextTypes.DEFAULT_TYPE, group_id: str, set_number: int) -> None:
    if context.job_queue is None:
        logger.warning("[TIMER] job_queue non disponibile, impossibile cancellare il timer.")
        return
    job_name = f"booking_{group_id}_{set_number}"
    current_jobs = context.job_queue.get_jobs_by_name(job_name)
    if current_jobs:
        for job in current_jobs:
            job.schedule_removal()
        logger.info(f"Timer prenotazioni cancellato per gruppo {group_id} set {set_number}.")


async def _show_set_booking_menu(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, set_number: int
):
    group_id = str(query.message.chat.id)
    num_sets = await get_num_sets(group_id)
    if set_number < 1 or set_number > num_sets:
        await _safe_edit_message_text(query, "Set non valido.")
        return

    context.chat_data.setdefault('sessions', {}).setdefault(set_number, {})
    context.chat_data['active_set'] = set_number

    pm = PrenotationManager(group_id)
    num_bookings = len(await async_retry(pm.get_prenotations, set_number))

    text, reply_markup = await get_booking_message_components(
        group_id=group_id,
        set_number=set_number,
        num_bookings=num_bookings,
        bot_username=context.bot.username,
    )

    message = await _safe_edit_message_text(
        query,
        text=text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    if message is None:
        logger.warning(f"Impossibile modificare messaggio per set {set_number} in gruppo {group_id}")
        return

    await set_active_booking_info(group_id, message.message_id, set_number)

    if await is_booking_timer_active(group_id):
        await _cancel_booking_timer(context, group_id, set_number)
        duration = await get_booking_timer_duration(group_id)
        if duration <= 0:
            logger.warning(f"Durata timer non valida per gruppo {group_id}: {duration}. Imposto 30s.")
            duration = 30

        if context.job_queue is None:
            logger.error("[TIMER] job_queue è None! Installa python-telegram-bot[job-queue]")
        else:
            context.job_queue.run_once(
                timer_expired_callback,
                duration,
                chat_id=int(group_id),
                data={
                    'message_id': message.message_id,
                    'set_number': set_number,
                    'topic_id': query.message.message_thread_id,
                },
                name=f"booking_{group_id}_{set_number}",
            )
            logger.info(f"Timer prenotazioni avviato: gruppo {group_id}, set {set_number}, durata {duration}s")


async def handle_set(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    group_id = str(query.message.chat.id)
    user_id = query.from_user.id

    if not await _check_admin(group_id, user_id, context):
        await query.answer("⚠️ Solo gli amministratori possono selezionare i set.", show_alert=True)
        return

    try:
        _, set_number_str = query.data.split("_", 1)
        set_number = int(set_number_str)
    except (ValueError, IndexError):
        logger.error(f"Errore parsing set_number da {query.data}")
        await query.answer("❌ Errore interno", show_alert=True)
        return
    
    num_sets = await get_num_sets(group_id)
    if set_number < 1 or set_number > num_sets:
        await query.answer("Set non valido.", show_alert=True)
        return
    
    await _show_set_booking_menu(query, context, set_number)


async def handle_set_precedente(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    group_id = str(query.message.chat.id)
    user_id = query.from_user.id

    if not await _check_admin(group_id, user_id, context):
        await query.answer("⚠️ Solo gli amministratori possono navigare tra i set.", show_alert=True)
        return

    current_set = context.chat_data.get('active_set')
    if not current_set:
        await query.answer("⚠️ Sessione del set non trovata. Riprova dal menu principale.", show_alert=True)
        return
    if current_set > 1:
        await _show_set_booking_menu(query, context, current_set - 1)
    else:
        await query.answer("Sei già al primo set!", show_alert=True)


async def handle_set_successivo(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    group_id = str(query.message.chat.id)
    user_id = query.from_user.id

    if not await _check_admin(group_id, user_id, context):
        await query.answer("⚠️ Solo gli amministratori possono navigare tra i set.", show_alert=True)
        return

    current_set = context.chat_data.get('active_set')
    if not current_set:
        await query.answer("⚠️ Sessione del set non trovata. Riprova dal menu principale.", show_alert=True)
        return
    num_sets = await get_num_sets(group_id)
    if current_set < num_sets:
        await _show_set_booking_menu(query, context, current_set + 1)
    else:
        await query.answer("Sei già all'ultimo set!", show_alert=True)


async def handle_termina_prenotazioni(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    group_id = str(query.message.chat.id)
    user_id = query.from_user.id

    if not await _check_admin(group_id, user_id, context):
        await query.answer("⚠️ Solo gli amministratori possono terminare le prenotazioni.", show_alert=True)
        return

    current_set = context.chat_data.get('active_set')
    if current_set is None:
        return

    await _cancel_booking_timer(context, group_id, current_set)
    await _create_end_booking_message(context, group_id, current_set, query.message.message_thread_id)
    await _safe_delete_message(context.bot, query.message.chat.id, query.message.message_id)
    await clear_active_booking_info(group_id)


async def timer_expired_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    if not job or not isinstance(job.data, dict):
        logger.error("[TIMER ERROR] job data mancante o non valido.")
        return

    message_id = job.data.get('message_id')
    set_number = job.data.get('set_number')
    topic_id = job.data.get('topic_id')
    group_id = str(job.chat_id)

    if message_id is None or set_number is None:
        logger.error(f"[TIMER ERROR] dati timer incompleti: {job.data}")
        return

    logger.info(f"[TIMER] Scaduto per gruppo {group_id}, set {set_number}")

    # Usa context.chat_data che è il dict mutable per questo chat
    context.chat_data['active_set'] = set_number
    context.chat_data.setdefault('sessions', {}).setdefault(set_number, {})

    try:
        await _create_end_booking_message(context, group_id, set_number, topic_id)
        await _safe_delete_message(context.bot, chat_id=int(group_id), message_id=message_id)
    except Exception as e:
        logger.error(f"[TIMER ERROR] Errore in timer_expired_callback per set {set_number}: {e}")
        return

    await clear_active_booking_info(group_id)


async def handle_posta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    group_id = str(query.message.chat.id)
    user_id = query.from_user.id

    if not await _check_admin(group_id, user_id, context):
        await query.answer("⚠️ Solo gli amministratori possono procedere con la posta.", show_alert=True)
        return

    chat_id = query.message.chat.id
    topic_id = getattr(query.message, "message_thread_id", None)
    set_number = context.chat_data.get('active_set', 1)
    sessions = context.chat_data.setdefault('sessions', {})
    session = sessions.setdefault(set_number, {})

    is_first_turn = session.get('current_index', 0) == 0

    if is_first_turn:
        list_msg_id = context.chat_data.pop('booking_list_message_id', None)
        if list_msg_id:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=list_msg_id,
                    reply_markup=None,
                )
            except Exception as e:
                logger.warning(f"Impossibile rimuovere i bottoni dalla lista prenotati: {e}")
    else:
        try:
            await query.message.delete()
        except Exception as e:
            logger.warning(f"Impossibile cancellare il messaggio 'Posta': {e}")

    pm = PrenotationManager(str(chat_id))

    if 'sorted_prenotazioni' not in session:
        session['sorted_prenotazioni'] = await async_retry(pm.get_sorted_prenotations, set_number)
        session['current_index'] = 0

    sorted_prenotazioni = session['sorted_prenotazioni']
    idx = session.get('current_index', 0)

    if idx < len(sorted_prenotazioni):
        user_id_str, info = sorted_prenotazioni[idx]
        session['recipient_id'] = int(user_id_str)

        try:
            chat_user = await context.bot.get_chat(int(user_id_str))
            user_name = f"@{chat_user.username}" if chat_user.username else chat_user.first_name
        except TelegramError as e:
            logger.error(f"Errore ottenendo info utente {user_id_str}: {e}")
            user_name = f"Utente {user_id_str}"
        except Exception as e:
            logger.error(f"Errore inatteso in handle_posta get_chat: {e}")
            user_name = f"Utente {user_id_str}"
        esc_username = escape_markdown(user_name, version=2)

        kb = [[InlineKeyboardButton("🔢 Avvia Donazioni", callback_data='dona')]]
        await _safe_send_message(
            context.bot,
            chat_id=chat_id,
            text=f"_👤 {esc_username} posta per il set {set_number}_",
            reply_markup=InlineKeyboardMarkup(kb),
            message_thread_id=topic_id,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        session['current_index'] = idx + 1
    else:
        set_kb = [
            [
                InlineKeyboardButton("⬅️ Set Precedente", callback_data='set_precedente'),
                InlineKeyboardButton("➡️ Set Successivo", callback_data='set_successivo'),
            ],
            [InlineKeyboardButton("❌ Termina", callback_data='Termina Giroset')],
        ]
        await _safe_send_message(
            context.bot,
            chat_id=chat_id,
            text="_0️⃣ Tutti gli utenti prenotati hanno postato\\._",
            reply_markup=InlineKeyboardMarkup(set_kb),
            message_thread_id=topic_id,
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def handle_dona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    group_id = str(query.message.chat.id)
    user_id = query.from_user.id

    if not await _check_admin(group_id, user_id, context):
        await query.answer("⚠️ Solo gli amministratori possono avviare le donazioni.", show_alert=True)
        return

    chat_id = query.message.chat.id
    topic_id = getattr(query.message, "message_thread_id", None)

    if context.chat_data.get('giroset_chiuso', False):
        return

    number_keyboard = [
        [InlineKeyboardButton(str(i), callback_data=f'number_{i}') for i in range(1, 4)],
        [InlineKeyboardButton(str(i), callback_data=f'number_{i}') for i in range(4, 7)],
        [InlineKeyboardButton(str(i), callback_data=f'number_{i}') for i in range(7, 10)],
        [InlineKeyboardButton("🔜 Prossimo", callback_data='Posta')],
    ]
    await _safe_send_message(
        context.bot,
        chat_id=chat_id,
        text="_⏬ Seleziona la carta che vuoi mettere a disposizione\\:_",
        reply_markup=InlineKeyboardMarkup(number_keyboard),
        message_thread_id=topic_id,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def handle_dona_carta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        parts = query.data.split('_')
        if len(parts) < 2:
            raise ValueError("Formato callback incorretto")
        number_pressed_str = parts[1]
        number_pressed = int(number_pressed_str)
    except (ValueError, IndexError) as e:
        logger.error(f"Errore parsing carta da {query.data}: {e}")
        await query.answer("❌ Errore interno", show_alert=True)
        return

    chat_id = query.message.chat.id
    topic_id = getattr(query.message, "message_thread_id", None)
    donating_user = query.from_user

    set_number = context.chat_data.get('active_set')
    sessions = context.chat_data.get('sessions', {})
    session = sessions.get(set_number, {})
    expected_recipient_id = session.get('recipient_id')

    if expected_recipient_id and donating_user.id == int(expected_recipient_id):
        await query.answer("⚠️ Sei il ricevente di questo turno, non puoi donare a te stesso!", show_alert=True)
        return

    esc_username = escape_markdown(donating_user.username or donating_user.first_name, version=2)
    response_message = f"🔝 @{esc_username} mette a disposizione la carta *{number_pressed}*\\."

    donazione_keyboard = [
        [
            InlineKeyboardButton("➕ Vai e Grazie", callback_data=f'vai_{donating_user.id}_{number_pressed}_{expected_recipient_id}'),
            InlineKeyboardButton("➖ No grazie", callback_data=f'no_{donating_user.id}_{number_pressed}'),
        ]
    ]
    await _safe_send_message(
        context.bot,
        chat_id=chat_id,
        text=response_message,
        reply_markup=InlineKeyboardMarkup(donazione_keyboard),
        message_thread_id=topic_id,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def handle_vai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    group_id = str(query.message.chat.id)
    action_user = query.from_user

    # Controllo Moderatore
    if not await _check_admin(group_id, action_user.id, context):
        await query.answer("⚠️ Solo i moderatori possono accettare la donazione con 'Vai e Grazie'.", show_alert=True)
        return

    # Parsing dei dati dal callback
    try:
        parts = query.data.split('_')
        if len(parts) < 3 or parts[0] != 'vai':
            raise ValueError("Formato callback non valido")

        donor_id = int(parts[1])
        number_pressed = int(parts[2])
        if number_pressed < 1 or number_pressed > 9:
            raise ValueError("Numero carta non valido")

        if len(parts) >= 4 and parts[3]:
            expected_recipient_id = int(parts[3])
        else:
            set_number_fb = context.chat_data.get('active_set')
            sessions_fb = context.chat_data.get('sessions', {})
            expected_recipient_id = sessions_fb.get(set_number_fb, {}).get('recipient_id')
    except (ValueError, IndexError) as ex:
        logger.error(f"Errore parsing dati bottone 'vai': {query.data} -> {ex}")
        await query.answer("⚠️ Errore interno: dati bottone non validi.", show_alert=True)
        return

    set_number = context.chat_data.get('active_set')

    if not set_number:
        await query.answer("Sessione non attiva.", show_alert=True)
        return

    num_sets = await get_num_sets(group_id)
    if set_number < 1 or set_number > num_sets:
        await query.answer("Set non valido.", show_alert=True)
        return

    if not expected_recipient_id:
        await query.answer("⚠️ Errore: ricevente non trovato nella sessione.", show_alert=True)
        return

    try:
        donor_chat = await asyncio.wait_for(context.bot.get_chat(donor_id), timeout=5)
        donor_username = f"@{donor_chat.username}" if donor_chat.username else donor_chat.first_name
    except asyncio.TimeoutError:
        logger.error(f"Timeout ottenendo donor_chat {donor_id}")
        donor_username = f"Utente {donor_id}"
    except TelegramError as e:
        logger.error(f"Errore ottenendo donor_chat {donor_id}: {e}")
        donor_username = f"Utente {donor_id}"
    except Exception as e:
        logger.error(f"Errore inatteso ottenendo donor_chat {donor_id}: {e}")
        donor_username = f"Utente {donor_id}"

    try:
        recipient_chat = await asyncio.wait_for(context.bot.get_chat(expected_recipient_id), timeout=5)
        recipient_username = f"@{recipient_chat.username}" if recipient_chat.username else recipient_chat.first_name
    except asyncio.TimeoutError:
        logger.error(f"Timeout ottenendo recipient_chat {expected_recipient_id}")
        recipient_username = f"Utente {expected_recipient_id}"
    except TelegramError as e:
        logger.error(f"Errore ottenendo recipient_chat {expected_recipient_id}: {e}")
        recipient_username = f"Utente {expected_recipient_id}"
    except Exception as e:
        logger.error(f"Errore inatteso ottenendo recipient_chat {expected_recipient_id}: {e}")
        recipient_username = f"Utente {expected_recipient_id}"

    rec_esc = escape_markdown(recipient_username, version=2)
    donor_esc = escape_markdown(donor_username, version=2)

    if not isinstance(set_number, int):
        logger.error(f"Errore critico in handle_vai: 'active_set' non trovato. Gruppo: {group_id}")
        await _safe_edit_message_text(
            query,
            text="_⚠️ Errore: la sessione del set non è stata trovata, impossibile assegnare punti\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    session_id = context.chat_data.get('giroset_session_id')
    dm = DonationManager(group_id, session_id=session_id)

    already_received = await async_retry(dm.has_received_card, set_number, str(expected_recipient_id), number_pressed)
    if already_received:
        await _safe_answer(query, f"⚠️ {recipient_username} ha già ricevuto questa carta.", show_alert=True)
        await _safe_edit_message_text(
            query,
            text=f"_⚠️ Donazione rifiutata: {rec_esc} ha già ricevuto la carta {number_pressed} in questo set\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    new_text = f"_🆒 {donor_esc} ha donato la carta {number_pressed} del set {set_number} a {rec_esc}\\._"
    await _safe_edit_message_text(query, text=new_text, reply_markup=None, parse_mode=ParseMode.MARKDOWN_V2)

    await async_retry(dm.record_donation, set_number, str(expected_recipient_id), number_pressed, str(donor_id))

    try:
        card_values = await get_card_values(group_id)
        card_points = await get_card_points(group_id)
        valore_carta = card_values[set_number - 1][number_pressed - 1]
        points = card_points.get(str(valore_carta), 0)

        if points > 0:
            lm = LeaderboardManager(group_id)
            try:
                await async_retry(lm.increment_score, str(donor_id), points)
            except ValueError as e:
                # increment_score solleva ValueError("punteggio_azzerato:N") se il punteggio
                # sarebbe diventato negativo: impossibile con delta positivo, gestito per robustezza.
                logger.warning(f"[handle_vai] increment_score inatteso per donor={donor_id}: {e}")

            giroset_scores = context.chat_data.setdefault('giroset_scores', {})
            giroset_scores[str(donor_id)] = giroset_scores.get(str(donor_id), 0) + points
    except IndexError:
        logger.error(f"Errore Indice in handle_vai: set={set_number}, carta={number_pressed}.")
        await _safe_edit_message_text(query, text="Errore interno: carta non valida.")
        return
    except Exception as e:
        logger.error(f"Errore inatteso durante il calcolo punti: {e}")


async def handle_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    group_id = str(query.message.chat.id)
    action_user = query.from_user

    if not await _check_admin(group_id, action_user.id, context):
        await query.answer("⚠️ Solo i moderatori possono rifiutare la donazione.", show_alert=True)
        return

    await _safe_delete_message(context.bot, query.message.chat.id, query.message.message_id)


# ---------------------------------------------------------------------------
# Helper: nome display con escape MarkdownV2
# ---------------------------------------------------------------------------

async def _get_display_name(
    context: ContextTypes.DEFAULT_TYPE, user_id: str, fallback: str = None
) -> str:
    try:
        chat_user = await context.bot.get_chat(int(user_id))
        name = f"@{chat_user.username}" if chat_user.username else chat_user.first_name
    except Exception:
        name = fallback or f"Utente {user_id}"
    return escape_markdown(name, version=2)


# ---------------------------------------------------------------------------
# Recap finale GiroSet
# ---------------------------------------------------------------------------

async def _send_recap_messages(
    context: ContextTypes.DEFAULT_TYPE, group_id: str, chat_id: int, topic_id
):
    session_id = context.chat_data.get('giroset_session_id')
    dm = DonationManager(group_id, session_id=session_id)
    card_values = await get_card_values(group_id)
    num_sets = len(card_values)

    donations_map = {}
    for set_num in range(1, num_sets + 1):
        set_ref = await async_retry(dm.get_set_donations, set_num)
        if not set_ref:
            continue
        if isinstance(set_ref, list):
            set_ref = {str(i): v for i, v in enumerate(set_ref) if v is not None}
        per_recipient = {}
        for recipient_id, cards in set_ref.items():
            if not cards:
                continue
            if isinstance(cards, list):
                cards = {str(i): v for i, v in enumerate(cards) if v is not None}
            per_recipient[recipient_id] = {int(card_num): donor for card_num, donor in cards.items()}
        if per_recipient:
            donations_map[set_num] = per_recipient

    if not donations_map:
        return

    # Recap gruppo per ogni set
    for set_num in sorted(donations_map.keys()):
        group_lines = [f"*📋 Recap donazioni Set {set_num}*"]
        for recipient_id, cards in donations_map[set_num].items():
            recipient_name = await _get_display_name(context, recipient_id)
            for card_num in sorted(cards.keys()):
                donor_val = cards[card_num]
                if isinstance(donor_val, str) and donor_val.isdigit():
                    donor_name = await _get_display_name(context, donor_val)
                else:
                    donor_name = escape_markdown("Donatore sconosciuto", version=2)
                group_lines.append(f"{donor_name} dona carta {card_num} a {recipient_name}")

        await _safe_send_message(
            context.bot,
            chat_id=chat_id,
            text="\n".join(group_lines),
            parse_mode=ParseMode.MARKDOWN_V2,
            message_thread_id=topic_id,
        )

    # Recap privato
    user_receives: dict[str, list] = {}
    user_donates: dict[str, list] = {}

    for set_num, recipients in donations_map.items():
        for recipient_id, cards in recipients.items():
            for card_num, donor_val in cards.items():
                user_receives.setdefault(recipient_id, []).append((set_num, card_num, donor_val))
                if isinstance(donor_val, str) and donor_val.isdigit():
                    user_donates.setdefault(donor_val, []).append((set_num, card_num, recipient_id))

    all_involved = set(user_receives.keys()) | set(user_donates.keys())

    for user_id in all_involved:
        lines = []

        receives = user_receives.get(user_id, [])
        if receives:
            lines.append("📥 *Ricevi*")
            for set_num, card_num, donor_val in sorted(receives, key=lambda x: (x[0], x[1])):
                if isinstance(donor_val, str) and donor_val.isdigit():
                    donor_name = await _get_display_name(context, donor_val)
                else:
                    donor_name = escape_markdown("Donatore sconosciuto", version=2)
                lines.append(f"  Carta {card_num} set {set_num} da {donor_name}")

        donates = user_donates.get(user_id, [])
        if donates:
            if lines:
                lines.append("")
            lines.append("📤 *Doni*")
            for set_num, card_num, recipient_id in sorted(donates, key=lambda x: (x[0], x[1])):
                recipient_name = await _get_display_name(context, recipient_id)
                lines.append(f"  Carta {card_num} set {set_num} a {recipient_name}")

        if not lines:
            continue

        text = "📬 *Riepilogo GiroSet*\n\n" + "\n".join(lines)
        message = await _safe_send_message(
            context.bot,
            chat_id=int(user_id),
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        if message is None:
            logger.warning(f"Impossibile inviare recap privato a {user_id}")


async def handle_termina_giroset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from comandi import visualizza_classifica

    query = update.callback_query
    group_id = str(query.message.chat.id)
    user_id = query.from_user.id

    if not await _check_admin(group_id, user_id, context):
        await query.answer("⚠️ Solo gli amministratori possono terminare il Giroset.", show_alert=True)
        return

    chat_id = query.message.chat.id
    topic_id = getattr(query.message, "message_thread_id", None)

    await _safe_delete_message(context.bot, query.message.chat.id, query.message.message_id)
    context.chat_data['giroset_chiuso'] = True

    giroset_scores = context.chat_data.get('giroset_scores', {})
    text_session = "*🏆 Classifica Sessione Corrente 🏆*\n\n"
    if not giroset_scores:
        text_session += "_Nessun punto assegnato in questa sessione\\._"
    else:
        sorted_scores = sorted(giroset_scores.items(), key=lambda item: item[1], reverse=True)
        lines = []
        for i, (score_user_id, score) in enumerate(sorted_scores, 1):
            try:
                chat_user = await context.bot.get_chat(score_user_id)
                username = escape_markdown(chat_user.username or chat_user.first_name, version=2)
                lines.append(f"{i}\\. @{username}: *{score}* punti")
            except Exception:
                lines.append(f"{i}\\. Utente Sconosciuto \\({score_user_id}\\): *{score}* punti")
        text_session += "\n".join(lines)

    await _safe_send_message(
        context.bot,
        chat_id=chat_id,
        text=text_session,
        message_thread_id=topic_id,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    await visualizza_classifica(update, context)
    await _send_recap_messages(context, group_id, chat_id, topic_id)

    for key in ['giroset_scores', 'sessions', 'active_set']:
        context.chat_data.pop(key, None)

    await clear_active_booking_info(group_id)

    pm = PrenotationManager(group_id)
    await async_retry(pm.clear_all_prenotations_for_group)

    logger.info(f"GiroSet terminato e dati di sessione puliti per il gruppo {group_id}.")
