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

from gateway import (
    fetch_models,
    fetch_session,
    forward_to_runner,
    reset_session_remote,
    update_session_remote,
)

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
    """Affiche la liste complète des commandes Antigravity."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    help_text = (
        "🤖 *Commandes disponibles — Assistant Personnel*\n\n"
        "💬 *Discussion & Mémoire*\n"
        "• Écrivez simplement votre message pour échanger avec Antigravity.\n"
        "• Vos messages s'enchaînent avec mémoire du contexte !\n"
        "• Si l'IA pose une question, répondez directement dans le chat.\n"
        "• `/new` — Démarre une nouvelle conversation (oublie l'historique)\n"
        "• `/session` — Affiche l'état et les réglages de la session en cours\n\n"
        "🧠 *Modèles d'IA*\n"
        "• `/models` — Liste tous les modèles disponibles\n"
        "• `/setmodel <nom>` — Change le modèle actif (ex: `/setmodel claude-sonnet-4-6`)\n"
        "• `/effort <low|medium|high|xhigh|max>` — Règle l'effort de réflexion\n"
        "• `/mode <auto|plan|accept-edits>` — Change le mode d'exécution\n\n"
        "🛡️ *Permissions & Outils*\n"
        "• `/permissions <always-proceed|request-review>` — Politique d'exécution des outils\n"
        "• `/usage` — Affiche la consommation de quota\n"
        "• `/skills` — Liste les compétences et outils installés\n"
        "• `/help` — Affiche cette aide"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def cmd_models(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche la liste des modèles disponibles avec le modèle actif."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    data = await fetch_models()
    models_list = data.get("models", [])
    current = data.get("current", "")

    lines = ["🧠 *Modèles Antigravity disponibles :*\n"]
    for m in models_list:
        mid = m.get("id")
        name = m.get("name")
        desc = m.get("desc", "")
        prefix = "⭐ " if mid == current else "• "
        lines.append(f"{prefix}`{mid}`\n   _{name}_ — {desc}\n")

    lines.append("\n👉 Pour changer : `/setmodel <nom>`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_setmodel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Change le modèle d'IA actif pour la session."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "⚠️ Précisez le nom du modèle. Exemple :\n`/setmodel claude-sonnet-4-6`\n`/setmodel gemini-3.8-flash-high`\n\nTapez `/models` pour voir la liste.",
            parse_mode="Markdown",
        )
        return

    chosen_model = args[0].strip()
    res = await update_session_remote(model=chosen_model)
    if "error" in res:
        await update.message.reply_text(f"❌ Erreur : {res['error']}")
    else:
        await update.message.reply_text(
            f"✅ Modèle changé avec succès !\nActif : `{chosen_model}`",
            parse_mode="Markdown",
        )


async def cmd_effort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Règle le niveau de raisonnement."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    args = context.args
    valid = ["low", "medium", "high", "xhigh", "max"]
    if not args or args[0].lower() not in valid:
        await update.message.reply_text(
            f"⚠️ Spécifiez un niveau valide : `{', '.join(valid)}`\nExemple : `/effort high`",
            parse_mode="Markdown",
        )
        return

    effort_val = args[0].lower()
    await update_session_remote(effort=effort_val)
    await update.message.reply_text(f"✅ Niveau d'effort réglé sur : `{effort_val}`", parse_mode="Markdown")


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Règle le mode d'exécution d'Antigravity."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    args = context.args
    valid = ["accept-edits", "auto", "plan"]
    if not args or args[0].lower() not in valid:
        await update.message.reply_text(
            f"⚠️ Spécifiez un mode valide : `{', '.join(valid)}`\nExemple : `/mode plan`",
            parse_mode="Markdown",
        )
        return

    mode_val = args[0].lower()
    await update_session_remote(mode=mode_val)
    await update.message.reply_text(f"✅ Mode d'exécution réglé sur : `{mode_val}`", parse_mode="Markdown")


async def cmd_permissions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Règle la politique de permissions pour les commandes sensibles."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    args = context.args
    valid = ["always-proceed", "request-review"]
    if not args or args[0].lower() not in valid:
        await update.message.reply_text(
            "⚠️ Spécifiez une politique :\n"
            "• `always-proceed` — Exécute les commandes automatiquement\n"
            "• `request-review` — Demande confirmation avant actions sensibles\n\n"
            "Exemple : `/permissions always-proceed`",
            parse_mode="Markdown",
        )
        return

    perm_val = args[0].lower()
    await update_session_remote(permission_mode=perm_val)
    await update.message.reply_text(f"✅ Politique de permissions : `{perm_val}`", parse_mode="Markdown")


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Démarre une nouvelle conversation vierge."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    await reset_session_remote()
    await update.message.reply_text(
        "🧹 *Nouvelle conversation démarrée !*\nL'historique précédent a été réinitialisé.",
        parse_mode="Markdown",
    )


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche les paramètres de la session active."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    s = await fetch_session()
    text = (
        "⚙️ *Configuration de la session active :*\n\n"
        f"• *Modèle :* `{s.get('model', 'défaut')}`\n"
        f"• *Effort de réflexion :* `{s.get('effort') or 'par défaut'}`\n"
        f"• *Mode d'exécution :* `{s.get('mode') or 'auto'}`\n"
        f"• *Permissions :* `{s.get('permission_mode', 'always-proceed')}`\n"
        f"• *Conversation en cours :* `{'Oui' if s.get('has_active_session') else 'Non (vierge)'}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Interroge et affiche la consommation de quota d'Antigravity."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    waiting_msg = await update.message.reply_text("⏳ Récupération des quotas...")
    res = await forward_to_runner("/usage", continue_session=False)
    await waiting_msg.edit_text(res.get("output", "Aucune information de quota retournée."))


async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Liste les compétences et intégrations actives."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    waiting_msg = await update.message.reply_text("⏳ Récupération des compétences...")
    res = await forward_to_runner("/skills", continue_session=False)
    await waiting_msg.edit_text(res.get("output", "Aucune compétence trouvée."))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Traite les messages texte entrants avec mémoire de contexte conversationnel."""
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

    # Exécution avec continuité de session activée (multi-turn)
    result = await forward_to_runner(prompt, continue_session=True)

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
    """Construit et retourne l'Application Telegram (singleton)."""
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

    # Enregistrement de tous les handlers de commandes
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("models", cmd_models))
    app.add_handler(CommandHandler("model", cmd_session))
    app.add_handler(CommandHandler("setmodel", cmd_setmodel))
    app.add_handler(CommandHandler("effort", cmd_effort))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("permissions", cmd_permissions))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("clear", cmd_new))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("quota", cmd_usage))
    app.add_handler(CommandHandler("skills", cmd_skills))

    # Handler messages texte normaux
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    _application = app
    logger.info("Application Telegram configurée avec l'ensemble des commandes Antigravity.")
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
