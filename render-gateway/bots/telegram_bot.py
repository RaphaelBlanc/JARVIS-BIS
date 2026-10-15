"""
bots/telegram_bot.py — Bot Telegram en mode webhook.

Reçoit les messages Telegram via un webhook FastAPI,
les transmet au home-runner via `gateway.forward_to_runner()`,
et répond à l'utilisateur avec le résultat.
"""

import asyncio
import logging
import os
import threading
from typing import Optional

from fastapi import APIRouter, Request, Response
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from gateway import forward_to_runner

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_WEBHOOK_URL: str = os.getenv("TELEGRAM_WEBHOOK_URL", "")

_RAW_ALLOWED_IDS: str = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS: set[int] = (
    {int(uid.strip()) for uid in _RAW_ALLOWED_IDS.split(",") if uid.strip()}
    if _RAW_ALLOWED_IDS
    else set()
)

# Singleton Application (initialisé via get_application())
_application: Optional[Application] = None

# ── Helpers ───────────────────────────────────────────────────────────────────


def _is_allowed(user_id: int) -> bool:
    """Vérifie si un utilisateur est autorisé à utiliser le bot."""
    if not ALLOWED_USER_IDS:
        return True  # pas de restriction configurée
    return user_id in ALLOWED_USER_IDS


# ── Handlers de commandes ────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Répond à la commande /start avec un message de bienvenue."""
    user = update.effective_user
    if not _is_allowed(user.id):
        logger.warning("Accès refusé pour l'utilisateur Telegram %d (%s).", user.id, user.username)
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    await update.message.reply_text(
        f"👋 Bonjour {user.first_name} !\n\n"
        "Je suis votre assistant personnel. Envoyez-moi simplement un message "
        "et je vous répondrai en transmettant votre demande à votre PC maison.\n\n"
        "Tapez /help pour plus d'informations."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Répond à la commande /help avec les instructions d'utilisation."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    await update.message.reply_text(
        "ℹ️ *Aide — Assistant Personnel*\n\n"
        "• Envoyez n'importe quel message texte pour interroger votre assistant.\n"
        "• /start — Message de bienvenue\n"
        "• /help — Affiche ce message\n\n"
        "_Le traitement peut prendre jusqu'à 2 minutes selon la complexité de la tâche._",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Traite les messages texte entrants et transmet au home-runner."""
    user = update.effective_user
    if not _is_allowed(user.id):
        logger.warning("Message refusé de l'utilisateur Telegram %d.", user.id)
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    prompt = update.message.text.strip()
    if not prompt:
        return

    logger.info("Message Telegram reçu de %d: '%s'", user.id, prompt[:80])

    # Envoie un message temporaire "en cours de traitement"
    waiting_msg = await update.message.reply_text("⏳ Traitement en cours...")

    result = await forward_to_runner(prompt)

    if result.get("success"):
        response_text = result.get("output", "").strip() or "✅ Tâche terminée (pas de sortie)."
    else:
        error = result.get("error", "Erreur inconnue")
        response_text = f"❌ Erreur : {error}"
        logger.error("Erreur retournée par le runner: %s", error)

    # Édite le message "en cours" avec la vraie réponse
    await waiting_msg.edit_text(response_text)


from telegram.request import HTTPXRequest

# ── Application Telegram ──────────────────────────────────────────────────────


def get_application() -> Application:
    """Construit et retourne l'Application Telegram (singleton).

    Returns:
        L'instance `Application` de python-telegram-bot configurée avec
        les handlers de commandes et de messages.
    """
    global _application
    if _application is not None:
        return _application

    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN n'est pas défini dans les variables d'environnement.")

    req = HTTPXRequest(
        connection_pool_size=10,
        read_timeout=30.0,
        write_timeout=30.0,
        connect_timeout=30.0,
        pool_timeout=30.0,
    )
    app = Application.builder().token(TELEGRAM_TOKEN).request(req).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    _application = app
    logger.info("Application Telegram configurée avec pool de connexions (size=10).")
    return _application


async def setup_webhook(app: Application, webhook_url: str) -> None:
    """Enregistre le webhook Telegram auprès de l'API Telegram.

    Args:
        app: L'instance Application Telegram.
        webhook_url: L'URL publique complète du webhook
                     (ex: ``https://mon-assistant.onrender.com/telegram/webhook``).
    """
    await app.bot.set_webhook(url=webhook_url)
    logger.info("Webhook Telegram enregistré sur: %s", webhook_url)


# ── Polling en arrière-plan (mode dev / test local) ───────────────────────────

_stop_event = threading.Event()
_polling_thread: Optional[threading.Thread] = None


def start_telegram_polling() -> None:
    """Lance le polling Telegram dans un thread séparé (pour tests locaux sans webhook)."""
    global _polling_thread
    if not TELEGRAM_TOKEN:
        logger.warning("TELEGRAM_TOKEN non défini — bot Telegram non démarré.")
        return

    _stop_event.clear()

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app = get_application()

        async def _async_polling():
            await app.initialize()
            # Supprime le webhook existant pour permettre le polling
            await app.bot.delete_webhook(drop_pending_updates=True)
            await app.start()
            await app.updater.start_polling()
            logger.info("Bot Telegram démarré en mode POLLING (local) !")
            while not _stop_event.is_set():
                await asyncio.sleep(1)
            await app.updater.stop()
            await app.stop()
            await app.shutdown()

        try:
            loop.run_until_complete(_async_polling())
        except Exception as exc:
            logger.error("Erreur dans le polling Telegram: %s", exc, exc_info=True)
        finally:
            loop.close()

    _polling_thread = threading.Thread(target=_run, name="telegram-polling", daemon=True)
    _polling_thread.start()
    logger.info("Thread de polling Telegram lancé.")


def stop_telegram_polling() -> None:
    """Arrête le polling Telegram."""
    _stop_event.set()


# ── Router FastAPI ────────────────────────────────────────────────────────────

telegram_router = APIRouter()


@telegram_router.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> Response:
    """Point d'entrée du webhook Telegram.

    Reçoit les mises à jour Telegram en JSON et les traite via l'Application.
    """
    tg_app = get_application()
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)

    async with tg_app:
        await tg_app.process_update(update)

    return Response(content="OK", status_code=200)
