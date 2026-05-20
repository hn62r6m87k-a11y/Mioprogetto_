import asyncio
import logging
import os
import sys

from aiohttp import web
from dotenv import load_dotenv
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bottoni import button_dispatch
from comandi import (
    add_admin_command,
    add_points,
    classigira_command,
    gira,
    handle_booking_numbers,
    handle_group_selection,
    remove_admin_command,
    remove_points,
    reset_leaderboard_command,
    start,
    visualizza_classifica,
)
from settings import impostazioni_command

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

TOKEN = os.getenv("TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # es. https://giroset-bot.onrender.com
PORT = int(os.getenv("PORT", "8443"))

if not TOKEN:
    logger.critical("TOKEN non configurato nelle variabili d'ambiente.")
    sys.exit(1)

if not WEBHOOK_URL:
    logger.critical("WEBHOOK_URL non configurato nelle variabili d'ambiente.")
    sys.exit(1)

# Istanza globale dell'application (necessaria per handle_webhook)
application = None


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Gestore globale degli errori non catturati dagli handler.
    Logga l'eccezione e notifica l'utente in modo graceful.
    """
    logger.error("Eccezione non gestita:", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Si è verificato un errore temporaneo. Riprova tra qualche secondo."
            )
        except TelegramError:
            pass


# ---------------------------------------------------------------------------
# Webhook handler (aiohttp)
# ---------------------------------------------------------------------------

async def health_check(request: web.Request) -> web.Response:
    return web.Response(text="✅ GiroSet Bot è online!", status=200)


async def handle_webhook(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception as e:
        logger.error(f"Errore nel parse del JSON webhook: {e}")
        return web.Response(status=400, text="Invalid JSON")

    update = Update.de_json(data, application.bot)
    asyncio.create_task(application.process_update(update))
    return web.Response(text="OK")


async def start_webserver() -> None:
    webapp = web.Application()
    webapp.router.add_get("/", health_check)
    webapp.router.add_get("/health", health_check)
    webapp.router.add_post("/webhook", handle_webhook)

    runner = web.AppRunner(webapp)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Webserver avviato su 0.0.0.0:{PORT}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run() -> None:
    global application

    logger.info("Configurazione del bot...")

    application = ApplicationBuilder().token(TOKEN).build()

    # --- Gestore globale errori ---
    application.add_error_handler(error_handler)

    # --- Comandi ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("gira", gira))
    application.add_handler(CommandHandler("classigira", classigira_command))
    application.add_handler(CommandHandler("impostagiro", impostazioni_command))

    # Comandi di gestione
    application.add_handler(CommandHandler("addpoints", add_points))
    application.add_handler(CommandHandler("removepoints", remove_points))
    application.add_handler(CommandHandler("addadmin", add_admin_command))
    application.add_handler(CommandHandler("removeadmin", remove_admin_command))
    application.add_handler(CommandHandler("resetclassigira", reset_leaderboard_command))

    # --- Callback ---
    application.add_handler(CallbackQueryHandler(handle_group_selection, pattern=r"^select_group_"))
    application.add_handler(CallbackQueryHandler(button_dispatch))

    # --- Messaggi privati (prenotazioni) ---
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle_booking_numbers,
    ))

    # Inizializza l'application (necessario prima di usare application.bot)
    await application.initialize()

    # Imposta il webhook su Telegram
    webhook_endpoint = f"{WEBHOOK_URL.rstrip('/')}/webhook"
    await application.bot.set_webhook(
        url=webhook_endpoint,
        drop_pending_updates=True,
    )
    logger.info(f"Webhook impostato su: {webhook_endpoint}")

    # Avvia il webserver aiohttp e l'application
    await start_webserver()
    await application.start()

    logger.info("Bot avviato in modalità webhook. In attesa di aggiornamenti...")

    # Mantieni il processo in esecuzione a tempo indeterminato
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown richiesto.")
    finally:
        await application.bot.delete_webhook()
        await application.stop()
        await application.shutdown()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Shutdown richiesto.")
    except Exception as e:
        logger.critical(f"Errore fatale: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()