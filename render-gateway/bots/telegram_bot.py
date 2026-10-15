"""
bots/telegram_bot.py — Bot Telegram en mode webhook.

Reçoit les messages Telegram via un webhook FastAPI,
les transmet au home-runner via `gateway.forward_to_runner()`,
et répond à l'utilisateur avec le résultat.
"""

import asyncio
import html
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional


from fastapi import APIRouter, Request, Response
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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


def markdown_to_telegram_html(text: str) -> str:
    """Convertit du Markdown GitHub Flavored en HTML Telegram riche et élégant."""
    if not text:
        return ""

    # 1. Extraction et protection des blocs de code
    code_blocks = []
    def save_code_block(match):
        code_blocks.append(match.group(2))
        return f"@@CODE_BLOCK_{len(code_blocks)-1}@@"
    text = re.sub(r"```([a-zA-Z0-9_-]*)\n(.*?)```", save_code_block, text, flags=re.DOTALL)

    # 2. Extraction du code inline
    inline_codes = []
    def save_inline_code(match):
        inline_codes.append(match.group(1))
        return f"@@INLINE_CODE_{len(inline_codes)-1}@@"
    text = re.sub(r"`([^`\n]+)`", save_inline_code, text)

    # 3. Échappement HTML du texte hors code
    text = html.escape(text, quote=False)

    # 4. Citations / Blockquotes (> texte) avec support natif Telegram
    def replace_blockquotes(match):
        lines = [re.sub(r"^&gt;\s?", "", l) for l in match.group(0).split("\n")]
        content = "\n".join(lines).strip()
        # Supprime les badges GitHub style [!NOTE] etc.
        content = re.sub(r"\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*", "", content)
        return f"<blockquote>{content}</blockquote>"
    text = re.sub(r"(?:^&gt;[^\n]*\n?)+", replace_blockquotes, text, flags=re.MULTILINE)

    # 5. Titres avec mise en valeur
    text = re.sub(r"^###\s+(.+)$", r"<b><u>\1</u></b>", text, flags=re.MULTILINE)
    text = re.sub(r"^##\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^#\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # 6. Séparateurs horizontaux
    text = re.sub(r"^\s*[-*_]{3,}\s*$", "────────────────────────────", text, flags=re.MULTILINE)

    # 7. Gras et Italique
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<![a-zA-Z0-9])\*([^* \n][^*\n]*?)\*(?![a-zA-Z0-9])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![a-zA-Z0-9])_([^_ \n][^_\n]*?)_(?![a-zA-Z0-9])", r"<i>\1</i>", text)

    # 8. Barré
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # 9. Liens [texte](url)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\)]+)\)", r'<a href="\2">\1</a>', text)

    # 10. Listes à puces avec jolis points
    text = re.sub(r"^\s*[\*\-]\s+", "• ", text, flags=re.MULTILINE)

    # 11. Restauration des blocs de code échappés
    for i, code in enumerate(code_blocks):
        escaped = html.escape(code.strip())
        text = text.replace(f"@@CODE_BLOCK_{i}@@", f"<pre><code>{escaped}</code></pre>")

    for i, code in enumerate(inline_codes):
        escaped = html.escape(code)
        text = text.replace(f"@@INLINE_CODE_{i}@@", f"<code>{escaped}</code>")

    return text.strip()


def split_telegram_message(text: str, max_length: int = 4000) -> list[str]:
    """Découpe un message long en morceaux de moins de 4000 caractères sans couper au milieu d'un paragraphe."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    current_chunk = ""

    # Découpe par paragraphe
    paragraphs = text.split("\n\n")
    for p in paragraphs:
        if len(current_chunk) + len(p) + 2 <= max_length:
            current_chunk = f"{current_chunk}\n\n{p}" if current_chunk else p
        else:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = ""
            if len(p) > max_length:
                lines = p.split("\n")
                for l in lines:
                    if len(current_chunk) + len(l) + 1 <= max_length:
                        current_chunk = f"{current_chunk}\n{l}" if current_chunk else l
                    else:
                        if current_chunk:
                            chunks.append(current_chunk)
                        current_chunk = l
            else:
                current_chunk = p

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


async def send_or_edit_telegram_response(
    waiting_msg,
    raw_text: str,
    reply_to_message=None,
) -> None:
    """Met en forme en HTML Telegram et édite/envoie la réponse avec gestion propre des messages longs et erreurs."""
    html_text = markdown_to_telegram_html(raw_text)
    chunks = split_telegram_message(html_text)

    # 1. Tente d'éditer le message d'attente avec le premier morceau
    first_chunk = chunks[0] if chunks else "✅ Tâche terminée."
    try:
        await waiting_msg.edit_text(first_chunk, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as exc:
        logger.warning("Échec affichage HTML Telegram (%s), repli sur texte brut.", exc)
        raw_chunks = split_telegram_message(raw_text)
        await waiting_msg.edit_text(raw_chunks[0] if raw_chunks else raw_text)
        chunks = raw_chunks

    # 2. S'il y a d'autres morceaux (message > 4000 chars), on les envoie à la suite
    if len(chunks) > 1 and reply_to_message:
        for chunk in chunks[1:]:
            try:
                await reply_to_message.reply_text(chunk, parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                await reply_to_message.reply_text(chunk)



# ── Handlers de commandes ────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Répond à la commande /start avec un message de bienvenue."""
    user = update.effective_user
    if not _is_allowed(user.id):
        logger.warning("Accès refusé pour l'utilisateur Telegram %d (%s).", user.id, user.username)
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    _save_chat_id(update.effective_chat.id)
    await update.message.reply_text(
        f"👋 Bonjour {user.first_name} !\n\n"
        "Je suis votre assistant personnel. Envoyez-moi simplement un message "
        "et je vous répondrai en transmettant votre demande à votre PC maison.\n\n"
        "Tapez /help pour plus d'informations ou /briefing pour votre résumé du jour."
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


def fuzzy_resolve_model(query: str, models: list[dict]) -> Optional[str]:
    """Résout intelligemment un nom de modèle même avec une saisie approximative."""
    q = query.lower().strip()
    if not q:
        return None
    # 1. Correspondance exacte
    for m in models:
        if m.get("id", "").lower() == q:
            return m["id"]
    # 2. Sous-chaîne dans l'ID
    for m in models:
        if q in m.get("id", "").lower():
            return m["id"]
    # 3. Sous-chaîne dans le nom complet
    for m in models:
        if q in m.get("name", "").lower():
            return m["id"]
    # 4. Raccourcis courants
    aliases = {
        "claude": "claude-sonnet-4-6",
        "sonnet": "claude-sonnet-4-6",
        "opus": "claude-opus-4-6-thinking",
        "flash": "gemini-3.8-flash-high",
        "gemini": "gemini-3.8-flash-high",
        "gpt": "gpt-oss-120b-medium",
        "oss": "gpt-oss-120b-medium",
    }
    return aliases.get(q)


async def cmd_models(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche la liste des modèles avec des boutons cliquables interactifs."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    data = await fetch_models()
    models_list = data.get("models", [])
    current = data.get("current", "")

    keyboard = []
    for m in models_list:
        mid = m.get("id")
        name = m.get("name", mid)
        label = f"⭐ {name}" if mid == current else name
        keyboard.append([InlineKeyboardButton(label, callback_data=f"setmodel:{mid}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🧠 *Choisissez un modèle d'IA :*\n_(cliquez sur un bouton pour l'activer immédiatement)_",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def cmd_setmodel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Change le modèle d'IA actif (avec autocomplétion par boutons ou saisie souple)."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    args = context.args
    data = await fetch_models()
    models_list = data.get("models", [])

    if not args:
        # Aucun argument : affiche directement les boutons cliquables !
        return await cmd_models(update, context)

    input_text = " ".join(args)
    resolved = fuzzy_resolve_model(input_text, models_list)

    if not resolved:
        await update.message.reply_text(
            f"⚠️ Modèle `{input_text}` non reconnu.\nCliquez sur `/models` pour choisir directement parmi les boutons.",
            parse_mode="Markdown",
        )
        return

    res = await update_session_remote(model=resolved)
    if "error" in res:
        await update.message.reply_text(f"❌ Erreur : {res['error']}")
    else:
        await update.message.reply_text(
            f"✅ Modèle changé avec succès !\nActif : `{resolved}`",
            parse_mode="Markdown",
        )


async def cmd_effort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Règle le niveau de raisonnement avec boutons interactifs."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    args = context.args
    valid = ["low", "medium", "high", "xhigh", "max"]

    if not args or args[0].lower() not in valid:
        keyboard = [
            [
                InlineKeyboardButton("low", callback_data="effort:low"),
                InlineKeyboardButton("medium", callback_data="effort:medium"),
                InlineKeyboardButton("high", callback_data="effort:high"),
            ],
            [
                InlineKeyboardButton("xhigh", callback_data="effort:xhigh"),
                InlineKeyboardButton("max", callback_data="effort:max"),
            ],
        ]
        await update.message.reply_text(
            "⚡ *Choisissez le niveau de réflexion (reasoning effort) :*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    effort_val = args[0].lower()
    await update_session_remote(effort=effort_val)
    await update.message.reply_text(f"✅ Niveau d'effort réglé sur : `{effort_val}`", parse_mode="Markdown")


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Règle le mode d'exécution d'Antigravity avec boutons interactifs."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    args = context.args
    valid = ["accept-edits", "auto", "plan"]

    if not args or args[0].lower() not in valid:
        keyboard = [
            [
                InlineKeyboardButton("Auto (standard)", callback_data="mode:auto"),
                InlineKeyboardButton("Plan (réflexion)", callback_data="mode:plan"),
            ],
            [
                InlineKeyboardButton("Accept-Edits", callback_data="mode:accept-edits"),
            ],
        ]
        await update.message.reply_text(
            "🛠️ *Choisissez le mode d'exécution d'Antigravity :*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    mode_val = args[0].lower()
    await update_session_remote(mode=mode_val)
    await update.message.reply_text(f"✅ Mode d'exécution réglé sur : `{mode_val}`", parse_mode="Markdown")


async def cmd_permissions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Règle la politique de permissions avec boutons interactifs."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    args = context.args
    valid = ["always-proceed", "request-review"]

    if not args or args[0].lower() not in valid:
        keyboard = [
            [
                InlineKeyboardButton("✅ Auto (Always Proceed)", callback_data="perm:always-proceed"),
            ],
            [
                InlineKeyboardButton("🛡️ Demander confirmation (Request Review)", callback_data="perm:request-review"),
            ],
        ]
        await update.message.reply_text(
            "🛡️ *Politique de permissions des outils sensibles :*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    perm_val = args[0].lower()
    await update_session_remote(permission_mode=perm_val)
    await update.message.reply_text(f"✅ Politique de permissions : `{perm_val}`", parse_mode="Markdown")


async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gère les clics sur les boutons interactifs Telegram."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("setmodel:"):
        model_id = data.split(":", 1)[1]
        await update_session_remote(model=model_id)
        await query.edit_message_text(f"✅ Modèle activé : `{model_id}`", parse_mode="Markdown")

    elif data.startswith("effort:"):
        eff = data.split(":", 1)[1]
        await update_session_remote(effort=eff)
        await query.edit_message_text(f"✅ Niveau d'effort réglé sur : `{eff}`", parse_mode="Markdown")

    elif data.startswith("mode:"):
        m = data.split(":", 1)[1]
        await update_session_remote(mode=m)
        await query.edit_message_text(f"✅ Mode d'exécution réglé sur : `{m}`", parse_mode="Markdown")

    elif data.startswith("perm:"):
        p = data.split(":", 1)[1]
        await update_session_remote(permission_mode=p)
        await query.edit_message_text(f"✅ Politique de permissions : `{p}`", parse_mode="Markdown")


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
    await send_or_edit_telegram_response(waiting_msg, res.get("output", "Aucune information de quota retournée."), update.message)


async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Liste les compétences et intégrations actives."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    waiting_msg = await update.message.reply_text("⏳ Récupération des compétences...")
    res = await forward_to_runner("/skills", continue_session=False)
    await send_or_edit_telegram_response(waiting_msg, res.get("output", "Aucune compétence trouvée."), update.message)


SAVED_CHATS_FILE = Path(__file__).resolve().parent.parent / "saved_chats.json"

BRIEFING_PROMPT = (
    "Effectue mon briefing matinal complet et synthétique :\n"
    "1. ✉️ Consulte mes e-mails récents reçus depuis hier via Gmail. "
    "IMPORTANT : Filtre et ignore TOTALEMENT toutes les publicités, spams, newsletters commerciales et promotions. "
    "Ne retiens que les messages importants (personnels, professionnels, école/EPITA, cours/examens, démarches). "
    "Pour chaque e-mail important retenu, résume en 1 ou 2 phrases l'essentiel et l'action requise.\n"
    "2. 📅 Consulte mon Google Agenda pour la journée d'aujourd'hui et demain matin, et liste mes rendez-vous avec leurs horaires.\n"
    "3. 🌤️ Consulte la météo du jour pour ma ville (température, ciel, pluie éventuelle).\n"
    "4. 📋 Consulte mes tâches Google Tasks en cours.\n"
    "Présente le résultat avec un ton motivant, clair et structuré avec des émojis."
)


def _save_chat_id(chat_id: int) -> None:
    """Enregistre l'ID du chat Telegram pour permettre les notifications automatiques."""
    try:
        chats = set()
        if SAVED_CHATS_FILE.exists():
            with open(SAVED_CHATS_FILE, "r", encoding="utf-8") as f:
                chats = set(json.load(f))
        chats.add(chat_id)
        with open(SAVED_CHATS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(chats), f)
    except Exception as exc:
        logger.warning("Erreur sauvegarde chat_id %s: %s", chat_id, exc)


def get_saved_chat_ids() -> list[int]:
    """Retourne la liste des chats Telegram enregistrés."""
    if SAVED_CHATS_FILE.exists():
        try:
            with open(SAVED_CHATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


async def send_broadcast_message(text: str) -> None:
    """Envoie un message formaté en HTML à tous les utilisateurs Telegram enregistrés."""
    app = get_application()
    html_text = markdown_to_telegram_html(text)
    chunks = split_telegram_message(html_text)
    for cid in get_saved_chat_ids():
        for chunk in chunks:
            try:
                await app.bot.send_message(chat_id=cid, text=chunk, parse_mode="HTML", disable_web_page_preview=True)
            except Exception as exc:
                logger.warning("Erreur diffusion Telegram HTML vers %s: %s", cid, exc)
                try:
                    await app.bot.send_message(chat_id=cid, text=text)
                except Exception:
                    pass


async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Génère le briefing matinal complet (tri des e-mails, agenda, météo)."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    _save_chat_id(update.effective_chat.id)
    waiting_msg = await update.message.reply_text("⏳ Préparation de votre briefing matinal (analyse e-mails, agenda, météo)...")
    res = await forward_to_runner(BRIEFING_PROMPT, continue_session=False, timeout=120)
    output = res.get("output", "").strip() or "⚠️ Impossible de générer le briefing matinal."
    await send_or_edit_telegram_response(waiting_msg, output, update.message)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Traite les messages texte entrants avec mémoire de contexte conversationnel et mise en page riche."""
    user = update.effective_user
    if not _is_allowed(user.id):
        logger.warning("Message refusé de l'utilisateur Telegram %d.", user.id)
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    _save_chat_id(update.effective_chat.id)

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

    # Édite le message "en cours" avec la réponse magnifiquement formatée en HTML
    await send_or_edit_telegram_response(waiting_msg, response_text, update.message)



from telegram.request import HTTPXRequest

# ── Application Telegram ──────────────────────────────────────────────────────

BOT_COMMANDS = [
    BotCommand("briefing", "Briefing matinal (mails triés, agenda, météo)"),
    BotCommand("models", "Choisir le modèle d'IA (boutons cliquables)"),
    BotCommand("effort", "Régler l'effort de réflexion (boutons)"),
    BotCommand("mode", "Changer le mode d'action (auto, plan)"),
    BotCommand("permissions", "Régler les permissions (auto/manuel)"),
    BotCommand("new", "Démarrer une nouvelle discussion"),
    BotCommand("session", "Voir les réglages actifs"),
    BotCommand("usage", "Consulter quotas et jetons"),
    BotCommand("skills", "Voir les compétences actives"),
    BotCommand("help", "Afficher l'aide complète"),
]



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
    app.add_handler(CommandHandler("briefing", cmd_briefing))
    app.add_handler(CommandHandler("brief", cmd_briefing))
    app.add_handler(CommandHandler("models", cmd_models))
    app.add_handler(CommandHandler("model", cmd_models))
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

    # Handler pour les boutons interactifs (clics)
    app.add_handler(CallbackQueryHandler(on_callback_query))

    # Handler messages texte normaux
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    _application = app
    logger.info("Application Telegram configurée avec l'ensemble des commandes et boutons interactifs.")
    return _application


async def setup_webhook(app: Application, webhook_url: str) -> None:
    """Enregistre le webhook Telegram auprès de l'API Telegram et configure les commandes."""
    await app.bot.set_webhook(url=webhook_url)
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
        logger.info("Commandes Telegram enregistrées avec succès (autocomplétion active).")
    except Exception as exc:
        logger.warning("Impossible d'enregistrer les commandes Telegram: %s", exc)
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
            try:
                await app.bot.set_my_commands(BOT_COMMANDS)
                logger.info("Commandes Telegram enregistrées avec succès (autocomplétion active).")
            except Exception as exc:
                logger.warning("Impossible d'enregistrer les commandes Telegram: %s", exc)
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
