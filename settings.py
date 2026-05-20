import logging
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from telegram.constants import ParseMode
from telegram.error import TelegramError, BadRequest
from telegram.ext import ContextTypes

from firebase_utils import SettingsManager
from utils import (
    DEFAULT_CARD_VALUES,
    DEFAULT_CARD_POINTS,
    admin_only,
    get_num_sets,
    get_card_values,
    get_card_points,
    get_booking_timer_duration,
    format_duration_seconds,
    invalidate_settings_cache,
    run_in_executor,
    async_retry,
)

logger = logging.getLogger(__name__)

async def _safe_settings_edit_message_text(
    query: CallbackQuery,
    text: str,
    reply_markup=None,
    parse_mode: str = None,
):
    try:
        return await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except BadRequest as e:
        message = str(e).lower()
        if 'too old' in message or 'message not modified' in message:
            logger.warning(f"Impossibile modificare il messaggio settings: {e}")
            return None
        logger.error(f"BadRequest settings edit_message_text: {e}")
        return None
    except TelegramError as e:
        logger.error(f"Errore Telegram settings edit_message_text: {e}")
        return None
    except Exception as e:
        logger.error(f"Errore inatteso settings edit_message_text: {e}")
        return None

async def _safe_settings_delete_message(query: CallbackQuery) -> bool:
    try:
        await query.message.delete()
        return True
    except BadRequest as e:
        if 'not found' in str(e).lower():
            logger.warning(f"Messaggio di settings non trovato: {e}")
            return True
        logger.error(f"BadRequest settings delete_message: {e}")
        return False
    except TelegramError as e:
        logger.error(f"Errore Telegram settings delete_message: {e}")
        return False
    except Exception as e:
        logger.error(f"Errore inatteso settings delete_message: {e}")
        return False

# Quanti set mostrare per pagina nel menu album
ALBUM_PAGE_SIZE = 10


# ---------------------------------------------------------------------------
# Verifica permessi per i callback di settings
# ---------------------------------------------------------------------------

async def _is_admin_callback(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await asyncio.wait_for(
            context.bot.get_chat_member(
                chat_id=query.message.chat.id,
                user_id=query.from_user.id,
            ),
            timeout=5,
        )
        if member.status in ('administrator', 'creator'):
            return True
    except asyncio.TimeoutError:
        logger.error("Timeout in _is_admin_callback")
        await query.answer("⚠️ Timeout durante la verifica dei permessi", show_alert=True)
        return False
    except TelegramError as e:
        logger.error(f"Errore Telegram in _is_admin_callback: {e}")
        await query.answer("⚠️ Errore durante la verifica dei permessi", show_alert=True)
        return False
    except Exception as e:
        logger.error(f"Errore inatteso in _is_admin_callback: {e}")
        await query.answer("⚠️ Errore interno", show_alert=True)
        return False

    await query.answer("❌ Accesso negato. Solo gli amministratori possono modificare le impostazioni.", show_alert=True)
    return False


# ---------------------------------------------------------------------------
# Comando /impostazioni
# ---------------------------------------------------------------------------

@admin_only
async def impostazioni_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🛠️ *Pannello Impostazioni*\n\n_Scegli quale impostazione modificare\\:_",
        reply_markup=get_main_settings_keyboard(),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def show_main_settings_menu(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    await _safe_settings_edit_message_text(
        query,
        "🛠️ *Pannello Impostazioni*\n\n_Scegli quale impostazione modificare\\:_",
        reply_markup=get_main_settings_keyboard(),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


def get_main_settings_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🏧 Punteggi", callback_data='settings_punteggi'),
            InlineKeyboardButton("🔢 Numero Set", callback_data='settings_num_set'),
        ],
        [
            InlineKeyboardButton("🌐 Personalizza Album", callback_data='settings_album_page_0'),
            InlineKeyboardButton("⏰ Timer Prenotazioni", callback_data='settings_timer'),
        ],
        [InlineKeyboardButton("❎ Chiudi", callback_data='settings_cancel')],
    ]
    return InlineKeyboardMarkup(keyboard)


# ---------------------------------------------------------------------------
# Dispatcher principale dei callback settings
# ---------------------------------------------------------------------------

async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    # SECURITY: Verifica admin SEMPRE prima di modificare qualsiasi setting
    if not await _is_admin_callback(query, context):
        return

    data = query.data
    context.chat_data['settings_message_id'] = query.message.message_id

    # Navigazione principale
    if data == 'settings_main':
        await show_main_settings_menu(query, context)
    elif data == 'settings_cancel':
        await _safe_settings_delete_message(query)

    # Punteggi
    elif data == 'settings_punteggi':
        await show_punteggi_menu(query, context)
    elif data.startswith('settings_punteggi_select_'):
        card_value_key = data.split('_')[-1]
        await show_punteggio_editor(query, context, card_value_key)
    elif data.startswith('settings_punteggi_update_'):
        _, _, _, action, card_value_key = data.split('_')
        await update_single_punteggio(query, context, card_value_key, action)

    # Numero Set
    elif data == 'settings_num_set':
        await show_num_set_menu(query, context)
    elif data.startswith('settings_num_set_update_'):
        action = data.split('_')[-1]
        await update_num_set(query, context, action)

    # Gestione Album — con paginazione
    elif data == 'settings_album':
        # Compatibilità: se arriva 'settings_album' senza pagina, vai a pagina 0
        await show_album_menu(query, context, page=0)
    elif data.startswith('settings_album_page_'):
        page = int(data.split('_')[-1])
        await show_album_menu(query, context, page=page)
    elif data.startswith('settings_album_set_'):
        set_index = int(data.split('_')[-1])
        await show_set_editor(query, context, set_index)
    elif data.startswith('settings_album_update_'):
        parts = data.split('_')
        set_index = int(parts[3])
        card_index = int(parts[4])
        action = parts[5]
        await update_card_value(query, context, set_index, card_index, action)

    # Stato Timer Prenotazioni
    elif data == 'settings_timer':
        await show_timer_status_menu(query, context)
    elif data == 'settings_timer_set_active':
        await set_timer_status(query, context, True)
    elif data == 'settings_timer_set_inactive':
        await set_timer_status(query, context, False)

    # Durata Timer
    elif data == 'settings_timer_duration':
        await show_timer_duration_menu(query, context)
    elif data.startswith('settings_timer_duration_update_'):
        action = data.split('_')[-1]
        await update_timer_duration(query, context, action)


# ---------------------------------------------------------------------------
# Gestione Punteggi
# ---------------------------------------------------------------------------

async def show_punteggi_menu(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    group_id = str(query.message.chat.id)
    card_points = await get_card_points(group_id)
    text = "*🏧 Modifica Punteggi*\n\n_Seleziona il valore della carta\\:_\n\n"
    buttons = []
    try:
        sorted_keys = sorted(card_points.keys(), key=lambda k: int(k) if isinstance(k, str) else k)
    except ValueError:
        logger.warning(f"Errore sorting card_points keys per gruppo {group_id}")
        sorted_keys = sorted(card_points.keys())
    
    for key in sorted_keys:
        try:
            text += f"*{key}* 🌟 → *{card_points.get(key, 0)}* punti\n"
            buttons.append(InlineKeyboardButton(f"{key} 🌟", callback_data=f'settings_punteggi_select_{key}'))
        except Exception as e:
            logger.warning(f"Errore formattando punteggio per chiave {key}: {e}")
            continue
    
    keyboard = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    keyboard.append([InlineKeyboardButton("⬅️ Indietro", callback_data='settings_main')])
    await _safe_settings_edit_message_text(
        query,
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def show_punteggio_editor(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, card_value_key: str):
    group_id = str(query.message.chat.id)
    # CONSISTENCY: Invalida cache prima di leggere per evitare stale data
    invalidate_settings_cache(group_id)
    card_points = await get_card_points(group_id)
    current_score = card_points.get(card_value_key, "N/A")
    text = (
        f"🏧 *Modifica Punteggio*\n\n"
        f"_Valore carta\\: *{card_value_key}* 🌟_\n"
        f"_Punteggio attuale\\: *{current_score}*_"
    )
    keyboard = [
        [
            InlineKeyboardButton("-1", callback_data=f'settings_punteggi_update_dec_{card_value_key}'),
            InlineKeyboardButton("+1", callback_data=f'settings_punteggi_update_inc_{card_value_key}'),
        ],
        [InlineKeyboardButton("⬅️ Indietro", callback_data='settings_punteggi')],
    ]
    await _safe_settings_edit_message_text(
        query,
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def update_single_punteggio(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, card_value_key: str, action: str
):
    group_id = str(query.message.chat.id)
    card_points = await get_card_points(group_id)
    sm = SettingsManager(group_id)
    current_points = card_points.get(card_value_key, 0)
    change = 1 if action == 'inc' else -1
    card_points[card_value_key] = max(0, current_points + change)
    await async_retry(sm.set_setting, 'card_points', card_points)
    invalidate_settings_cache(group_id)
    await show_punteggio_editor(query, context, card_value_key)


# ---------------------------------------------------------------------------
# Gestione Numero Set
# ---------------------------------------------------------------------------

async def show_num_set_menu(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    group_id = str(query.message.chat.id)
    current_num_sets = await get_num_sets(group_id)
    text = f"*🔢 Modifica Numero Set*\n\n_Numero attuale di set\\: *{current_num_sets}*_"
    keyboard = [
        [
            InlineKeyboardButton("-1", callback_data='settings_num_set_update_dec'),
            InlineKeyboardButton("+1", callback_data='settings_num_set_update_inc'),
        ],
        [InlineKeyboardButton("⬅️ Indietro", callback_data='settings_main')],
    ]
    await _safe_settings_edit_message_text(
        query,
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def update_num_set(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, action: str):
    group_id = str(query.message.chat.id)
    sm = SettingsManager(group_id)
    current_num = await get_num_sets(group_id)
    change = 1 if action == 'inc' else -1
    new_num = current_num + change
    # VALIDATION: Num set deve essere tra 1 e 50
    if 1 <= new_num <= 50:
        await async_retry(sm.set_setting, 'num_sets', new_num)
        invalidate_settings_cache(group_id)
    await show_num_set_menu(query, context)


# ---------------------------------------------------------------------------
# Gestione Album — con paginazione
# ---------------------------------------------------------------------------

async def show_album_menu(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """
    Mostra il menu di selezione set con paginazione.
    Ogni pagina mostra ALBUM_PAGE_SIZE set (default 10), 5 per riga.
    """
    group_id = str(query.message.chat.id)
    card_values = await get_card_values(group_id)
    total_sets = min(len(card_values), await get_num_sets(group_id))
    total_pages = max(1, (total_sets + ALBUM_PAGE_SIZE - 1) // ALBUM_PAGE_SIZE)

    # Sicurezza: mantieni page nei limiti
    page = max(0, min(page, total_pages - 1))

    start_idx = page * ALBUM_PAGE_SIZE
    end_idx = min(start_idx + ALBUM_PAGE_SIZE, total_sets)

    text = (
        f"🌐 *Personalizza Album*\n\n"
        f"_Seleziona il set che vuoi modificare\\:_\n"
        f"_Pagina {page + 1} di {total_pages} "
        f"\\(set {start_idx + 1}\\-{end_idx}\\)_"
    )

    # Bottoni dei set per questa pagina
    buttons = [
        InlineKeyboardButton(f"Set {i + 1}", callback_data=f'settings_album_set_{i}')
        for i in range(start_idx, end_idx)
    ]
    keyboard = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]

    # Riga navigazione pagine
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Prec", callback_data=f'settings_album_page_{page - 1}'))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Succ ▶️", callback_data=f'settings_album_page_{page + 1}'))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("⬅️ Indietro", callback_data='settings_main')])

    await _safe_settings_edit_message_text(
        query,
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def show_set_editor(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, set_index: int):
    group_id = str(query.message.chat.id)
    card_values = await get_card_values(group_id)
    total_sets = min(len(card_values), await get_num_sets(group_id))
    if not (0 <= set_index < total_sets):
        await _safe_settings_edit_message_text(query, "❌ Errore: Set non trovato.")
        return
    set_data = card_values[set_index]

    # Calcola su quale pagina si trova questo set per il bottone "Indietro"
    back_page = set_index // ALBUM_PAGE_SIZE

    text = f"🌐 *Personalizza Set {set_index + 1}*\n\n_Modifica le stelle per ogni carta\\:_"
    keyboard = []
    for idx, val in enumerate(set_data):
        keyboard.append([
            InlineKeyboardButton("➖", callback_data=f'settings_album_update_{set_index}_{idx}_dec'),
            InlineKeyboardButton(f"{idx + 1} ({val} ⭐)", callback_data="noop"),
            InlineKeyboardButton("➕", callback_data=f'settings_album_update_{set_index}_{idx}_inc'),
        ])
    keyboard.append([InlineKeyboardButton("⬅️ Indietro", callback_data=f'settings_album_page_{back_page}')])
    await _safe_settings_edit_message_text(
        query,
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def update_card_value(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, set_index: int, card_index: int, action: str
):
    group_id = str(query.message.chat.id)
    sm = SettingsManager(group_id)
    card_values = await get_card_values(group_id)
    total_sets = min(len(card_values), await get_num_sets(group_id))
    if not (0 <= set_index < total_sets and 0 <= card_index < len(card_values[set_index])):
        await _safe_settings_edit_message_text(query, "❌ Errore: Carta o set non validi.")
        return
    change = 1 if action == 'inc' else -1
    new_val = card_values[set_index][card_index] + change
    # VALIDATION: Valori carte devono essere tra 1 e 10
    if 1 <= new_val <= 10:
        card_values[set_index][card_index] = new_val
        await async_retry(sm.set_setting, 'card_values', card_values)
        invalidate_settings_cache(group_id)
    else:
        await query.answer("⚠️ Valore non valido (1-10)", show_alert=True)
        return
    await show_set_editor(query, context, set_index)


# ---------------------------------------------------------------------------
# Gestione Stato Timer
# ---------------------------------------------------------------------------

async def show_timer_status_menu(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    group_id = str(query.message.chat.id)
    sm = SettingsManager(group_id)
    is_active = await async_retry(sm.get_setting, 'booking_timer_active', False)

    active_label = "✅ Attivo" if is_active else "Attivo"
    inactive_label = "✅ Disattivo" if not is_active else "Disattivo"
    status_text = "Il timer è *ATTIVO*\\." if is_active else "Il timer è *NON ATTIVO*\\."

    text = (
        f"⏰ *Impostazioni Timer Prenotazioni*\n\n"
        f"{status_text}\n\n"
        "Scegli se attivare/disattivare il timer\\. Se attivo, puoi regolarne la durata\\."
    )

    keyboard = [
        [
            InlineKeyboardButton(active_label, callback_data='settings_timer_set_active'),
            InlineKeyboardButton(inactive_label, callback_data='settings_timer_set_inactive'),
        ]
    ]
    if is_active:
        keyboard.append([InlineKeyboardButton("⏳ Regola Tempo", callback_data='settings_timer_duration')])
    keyboard.append([InlineKeyboardButton("⬅️ Indietro", callback_data='settings_main')])

    await _safe_settings_edit_message_text(
        query,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def set_timer_status(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, activate: bool):
    group_id = str(query.message.chat.id)
    sm = SettingsManager(group_id)
    await async_retry(sm.set_setting, 'booking_timer_active', activate)
    invalidate_settings_cache(group_id)
    message = "Timer attivato ✅" if activate else "Timer disattivato ❌"
    await query.answer(message)
    await show_timer_status_menu(query, context)


# ---------------------------------------------------------------------------
# Gestione Durata Timer
# ---------------------------------------------------------------------------

async def show_timer_duration_menu(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    group_id = str(query.message.chat.id)
    current_duration_seconds = await get_booking_timer_duration(group_id)
    current_duration_text = format_duration_seconds(current_duration_seconds)

    text = (
        f"*⏳ Modifica Durata Timer*\n\n"
        f"Durata attuale\\: *{current_duration_text}*\n\n"
        f"_La durata si applica solo quando il timer è attivo\\._"
    )

    keyboard = [
        [
            InlineKeyboardButton("-30 sec", callback_data='settings_timer_duration_update_dec'),
            InlineKeyboardButton("+30 sec", callback_data='settings_timer_duration_update_inc'),
        ],
        [InlineKeyboardButton("⬅️ Indietro", callback_data='settings_timer')],
    ]
    await _safe_settings_edit_message_text(
        query,
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def update_timer_duration(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, action: str):
    group_id = str(query.message.chat.id)
    sm = SettingsManager(group_id)
    current_duration = await get_booking_timer_duration(group_id)
    change = 30 if action == 'inc' else -30
    new_duration = current_duration + change
    # VALIDATION: Timer deve essere tra 30s e 3600s (1 ora)
    new_duration = max(30, min(3600, new_duration))
    await async_retry(sm.set_setting, 'booking_timer_duration', new_duration)
    invalidate_settings_cache(group_id)
    await show_timer_duration_menu(query, context)
