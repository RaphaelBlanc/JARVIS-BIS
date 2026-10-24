"""
bots/telegram_bot.py — Bot Telegram en mode webhook.

Reçoit les messages Telegram via un webhook FastAPI,
les transmet au home-runner via `gateway.forward_to_runner()`,
et répond à l'utilisateur avec le résultat.
"""

import asyncio
import base64
import hashlib
import html
import io
import json
import logging
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from formatters import (
    extract_and_render_latex,
    extract_downloadable_code,
    latex_to_unicode,
)
from stt import MAX_AUDIO_BYTES, transcribe_audio
from briefing import get_briefing_text
from reminders import (
    add_reminder,
    cancel_reminder,
    complete_reminder,
    detect_natural_reminder_full,
    format_remaining_time,
    format_reminder_confirmation,
    format_reminder_notification,
    list_active_reminders,
    parse_reminder_command,
    recurrence_label,
    snooze_reminder,
)


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
    add_memory_fact,
    clear_memory_facts,
    delete_conversation_remote,
    delete_memory_fact,
    delete_memory_profile,
    fetch_conversations,
    fetch_health,
    fetch_memory,
    fetch_models,
    fetch_session,
    fetch_weather_direct,
    forward_to_runner,
    reset_session_remote,
    resume_conversation_remote,
    set_memory_profile,
    update_session_remote,
)


logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_WEBHOOK_URL: str = os.getenv("TELEGRAM_WEBHOOK_URL", "")


def _derive_webhook_secret() -> str:
    """Retourne le secret du webhook Telegram.

    Un secret explicite (``TELEGRAM_WEBHOOK_SECRET``) est prioritaire. Sinon,
    on dérive un secret stable à partir de ``SECRET_KEY`` : cela protège le
    webhook sans configuration supplémentaire et sans exposer la clé partagée.
    """
    explicit = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if explicit:
        if re.fullmatch(r"[A-Za-z0-9_-]{1,256}", explicit):
            return explicit
        logger.warning(
            "TELEGRAM_WEBHOOK_SECRET invalide (caractères autorisés : A-Za-z0-9_-) ; "
            "un secret sera dérivé de SECRET_KEY."
        )
    base = os.getenv("SECRET_KEY", "").strip()
    if not base:
        return ""
    return hashlib.sha256(f"telegram-webhook:{base}".encode("utf-8")).hexdigest()


TELEGRAM_WEBHOOK_SECRET: str = _derive_webhook_secret()

_RAW_ALLOWED_IDS: str = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS: set[int] = (
    {int(uid.strip()) for uid in _RAW_ALLOWED_IDS.split(",") if uid.strip()}
    if _RAW_ALLOWED_IDS
    else set()
)
ALLOW_ALL_USERS: bool = os.getenv("ALLOW_ALL_USERS", "").strip().lower() in {
    "1", "true", "yes", "on"
}

# Singleton Application (initialisé via get_application())
_application: Optional[Application] = None

# Boucle asyncio du polling Telegram (thread séparé). Utilisée par le
# planificateur de briefing pour diffuser sans conflit de boucle.
_TELEGRAM_LOOP: Optional[asyncio.AbstractEventLoop] = None

# ── Helpers ───────────────────────────────────────────────────────────────────


def _is_allowed(user_id: int) -> bool:
    """Vérifie si un utilisateur est autorisé à utiliser le bot.

    Par défaut, une liste vide refuse tout le monde (fail-closed). Pour ouvrir
    l'accès sans whitelist, il faut définir explicitement ``ALLOW_ALL_USERS=1``.
    """
    if ALLOWED_USER_IDS:
        return user_id in ALLOWED_USER_IDS
    return ALLOW_ALL_USERS


if not ALLOWED_USER_IDS and not ALLOW_ALL_USERS:
    logger.warning(
        "TELEGRAM_ALLOWED_USER_IDS est vide et ALLOW_ALL_USERS n'est pas activé : "
        "tous les messages Telegram seront refusés. Renseignez votre ID "
        "utilisateur ou définissez ALLOW_ALL_USERS=1 pour ouvrir l'accès."
    )


def markdown_to_telegram_html(text: str) -> str:
    """Convertit du Markdown GitHub Flavored en HTML Telegram riche avec code copiable et symboles mathématiques."""
    if not text:
        return ""

    # 1. Extraction et protection des blocs de code
    code_blocks: list[tuple[str, str]] = []
    def save_code_block(match):
        lang = (match.group(1) or "").strip().lower()
        code_blocks.append((lang, match.group(2)))
        return f"@@CODE_BLOCK_{len(code_blocks)-1}@@"
    text = re.sub(r"```([a-zA-Z0-9_\-\+]*)\n(.*?)```", save_code_block, text, flags=re.DOTALL)

    # 2. Extraction du code inline
    inline_codes: list[str] = []
    def save_inline_code(match):
        inline_codes.append(match.group(1))
        return f"@@INLINE_CODE_{len(inline_codes)-1}@@"
    text = re.sub(r"`([^`\n]+)`", save_inline_code, text)

    # 3. Conversion mathématique LaTeX inline résiduelle vers Unicode
    text = latex_to_unicode(text)

    # 4. Échappement HTML du texte hors code
    text = html.escape(text, quote=False)

    # 5. Citations / Blockquotes (> texte) avec support natif Telegram
    def replace_blockquotes(match):
        lines = [re.sub(r"^&gt;\s?", "", l) for l in match.group(0).split("\n")]
        content = "\n".join(lines).strip()
        # Supprime les badges GitHub style [!NOTE] etc.
        content = re.sub(r"\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*", "", content)
        return f"<blockquote>{content}</blockquote>"
    text = re.sub(r"(?:^&gt;[^\n]*\n?)+", replace_blockquotes, text, flags=re.MULTILINE)

    # 6. Titres avec mise en valeur
    text = re.sub(r"^###\s+(.+)$", r"<b><u>\1</u></b>", text, flags=re.MULTILINE)
    text = re.sub(r"^##\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^#\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # 7. Séparateurs horizontaux
    text = re.sub(r"^\s*[-*_]{3,}\s*$", "────────────────────────────", text, flags=re.MULTILINE)

    # 8. Gras et Italique
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<![a-zA-Z0-9])\*([^* \n][^*\n]*?)\*(?![a-zA-Z0-9])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![a-zA-Z0-9])_([^_ \n][^_\n]*?)_(?![a-zA-Z0-9])", r"<i>\1</i>", text)

    # 9. Barré
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # 10. Liens [texte](url)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\)]+)\)", r'<a href="\2">\1</a>', text)

    # 11. Listes à puces avec jolis points
    text = re.sub(r"^\s*[\*\-]\s+", "• ", text, flags=re.MULTILINE)

    # 12. Restauration des blocs de code avec classe de langage (bouton Copier natif Telegram)
    for i, (lang, code) in enumerate(code_blocks):
        escaped = html.escape(code)
        lang_attr = f' class="language-{lang}"' if lang else ""
        text = text.replace(f"@@CODE_BLOCK_{i}@@", f"<pre><code{lang_attr}>{escaped}</code></pre>")

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


_TRACKED_CHAT_MESSAGES: dict[int, list[int]] = {}


def track_chat_message(chat_id: int, message_id: int) -> None:
    """Enregistre l'ID d'un message afin de pouvoir le supprimer lors d'un changement de conversation."""
    if chat_id not in _TRACKED_CHAT_MESSAGES:
        _TRACKED_CHAT_MESSAGES[chat_id] = []
    _TRACKED_CHAT_MESSAGES[chat_id].append(message_id)


async def clear_telegram_chat(bot, chat_id: int, current_message_id: Optional[int] = None) -> None:
    """Efface les messages précédents enregistrés et récents dans la conversation."""
    tracked = _TRACKED_CHAT_MESSAGES.pop(chat_id, [])
    ids_to_delete = set(tracked)

    if current_message_id:
        for mid in range(max(1, current_message_id - 200), current_message_id + 1):
            ids_to_delete.add(mid)

    if not ids_to_delete:
        return

    sorted_ids = sorted(list(ids_to_delete), reverse=True)

    for i in range(0, len(sorted_ids), 100):
        batch = sorted_ids[i:i + 100]
        try:
            await bot.delete_messages(chat_id=chat_id, message_ids=batch)
        except Exception:
            for mid in batch:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=mid)
                except Exception:
                    pass


async def replay_telegram_history(bot, chat_id: int, history: list[dict]) -> None:
    """Affiche les anciens prompts et réponses d'une conversation passée."""
    if not history:
        return

    for turn in history:
        prompt_txt = turn.get("prompt", "").strip()
        resp_txt = turn.get("response", "").strip()

        if prompt_txt:
            prompt_html = markdown_to_telegram_html(prompt_txt)
            text_p = f"👤 <b>Vous :</b>\n{prompt_html}"
            chunks_p = split_telegram_message(text_p)
            for cp in chunks_p:
                try:
                    m = await bot.send_message(chat_id=chat_id, text=cp, parse_mode="HTML", disable_web_page_preview=True)
                    track_chat_message(chat_id, m.message_id)
                except Exception:
                    m = await bot.send_message(chat_id=chat_id, text=f"👤 Vous :\n{prompt_txt[:3500]}")
                    track_chat_message(chat_id, m.message_id)

        if resp_txt:
            resp_html = markdown_to_telegram_html(resp_txt)
            text_r = f"🤖 <b>Assistant :</b>\n{resp_html}"
            chunks_r = split_telegram_message(text_r)
            for cr in chunks_r:
                try:
                    m = await bot.send_message(chat_id=chat_id, text=cr, parse_mode="HTML", disable_web_page_preview=True)
                    track_chat_message(chat_id, m.message_id)
                except Exception:
                    m = await bot.send_message(chat_id=chat_id, text=f"🤖 Assistant :\n{resp_txt[:3500]}")
                    track_chat_message(chat_id, m.message_id)


async def send_or_edit_telegram_response(
    waiting_msg,
    raw_text: str,
    reply_to_message=None,
) -> None:
    """Met en forme en HTML Telegram et édite/envoie la réponse avec gestion du LaTeX et des extraits de code."""
    if waiting_msg and hasattr(waiting_msg, "chat_id") and hasattr(waiting_msg, "message_id"):
        track_chat_message(waiting_msg.chat_id, waiting_msg.message_id)

    # 1. Rendu graphique des équations LaTeX et extraction des gros blocs de code
    processed_text, rendered_images = await extract_and_render_latex(raw_text, dark_mode=True)
    code_snippets = extract_downloadable_code(raw_text, min_chars=800)

    html_text = markdown_to_telegram_html(processed_text)
    chunks = split_telegram_message(html_text)

    # 2. Tente d'éditer le message d'attente avec le premier morceau
    first_chunk = chunks[0] if chunks else "✅ Tâche terminée."
    try:
        await waiting_msg.edit_text(first_chunk, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as exc:
        logger.warning("Échec affichage HTML Telegram (%s), repli sur texte brut.", exc)
        raw_chunks = split_telegram_message(processed_text)
        await waiting_msg.edit_text(raw_chunks[0] if raw_chunks else processed_text)
        chunks = raw_chunks

    # 3. S'il y a d'autres morceaux (message > 4000 chars), on les envoie à la suite
    target = reply_to_message or waiting_msg
    if len(chunks) > 1 and target:
        for chunk in chunks[1:]:
            try:
                m = await target.reply_text(chunk, parse_mode="HTML", disable_web_page_preview=True)
                track_chat_message(m.chat_id, m.message_id)
            except Exception:
                m = await target.reply_text(chunk)
                track_chat_message(m.chat_id, m.message_id)

    # 4. Envoi des équations mathématiques rendues en images
    for img in rendered_images:
        try:
            m = await target.reply_photo(
                photo=io.BytesIO(img["bytes"]),
                caption=f"📐 <b>{html.escape(img['title'])}</b>",
                parse_mode="HTML",
            )
            track_chat_message(m.chat_id, m.message_id)
        except Exception as exc:
            logger.warning("Erreur envoi image d'équation Telegram: %s", exc)

    # 5. Envoi des extraits de code volumineux en documents téléchargeables
    for snippet in code_snippets:
        try:
            m = await target.reply_document(
                document=io.BytesIO(snippet["bytes"]),
                filename=snippet["filename"],
                caption=f"📄 Extrait de code (<b>{html.escape(snippet['lang'])}</b>)",
                parse_mode="HTML",
            )
            track_chat_message(m.chat_id, m.message_id)
        except Exception as exc:
            logger.warning("Erreur envoi document de code Telegram: %s", exc)



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
    """Affiche la liste complète des commandes de l'Assistant Personnel."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    help_text = (
        "🤖 *Commandes de l'Assistant Personnel*\n\n"
        "⚡ *Productivité & Outils*\n"
        "• `/briefing` — Résumé matinal (mails importants, agenda, météo, tâches)\n"
        "• `/resume` — Reprendre une discussion précédente (boutons interactifs)\n"
        "• `/agenda` — Consulter votre Google Agenda (aujourd'hui, semaine)\n"
        "• `/tasks` — Gérer vos Google Tasks (lister, ajouter, terminer)\n"
        "• `/meteo [ville]` — Météo en direct (Open-Meteo mondiale)\n"
        "• `/search <requête>` — Recherche web DuckDuckGo\n"
        "• `/summarize <url>` — Résumer une page web ou un article\n"
        "• `/rappel [récurrence] <durée> <message>` — Programmer un rappel\n"
        "  Ex: `/rappel chaque jour 8h Faire mes étirements`\n"
        "• `/rappels` — Lister vos rappels actifs\n"
        "• `/snooze <id> [durée]` — Reporter un rappel (10m par défaut)\n"
        "• `/rappel_termine <id>` — Clôturer un rappel\n"
        "• `/annuler_rappel <id>` — Annuler un rappel\n"
        "• `/status` — État complet du système et des services\n\n"
        "💬 *Discussion & Mémoire*\n"
        "• `/souviens <texte>` — Ajouter un souvenir durable\n"
        "• `/oublie <id|all>` — Supprimer un souvenir\n"
        "• `/profil [clé] [valeur]` — Afficher ou modifier votre profil\n"
        "• Écrivez simplement votre message pour échanger naturellement.\n"
        "• 🎤 Envoyez un message vocal : il est transcrit puis traité automatiquement.\n"
        "• 👁️ Envoyez une photo : elle est analysée directement.\n"
        "• ⏰ « Rappelle-moi dans 15 min de... » crée un rappel automatiquement.\n"
        "• Vos messages s'enchaînent avec mémoire du contexte !\n"
        "• `/new` — Démarre une nouvelle conversation vierge\n"
        "• `/compact` — Compacte la mémoire de la conversation en cours\n"
        "• `/session` — Affiche la configuration de la session active\n\n"
        "🧠 *Modèles d'IA & Réglages*\n"
        "• `/models` — Choisir le modèle d'IA (boutons cliquables)\n"
        "• `/setmodel <nom>` — Changer de modèle directement\n"
        "• `/effort` — Régler l'effort de réflexion (low/medium/high/max)\n"
        "• `/mode` — Mode d'exécution (auto, plan, accept-edits)\n"
        "• `/permissions` — Politique de sécurité des outils\n"
        "• `/usage` — Quotas et jetons consommés\n"
        "• `/skills` — Liste des outils et intégrations\n"
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
    valid = ["default", "request-review", "plan", "ask", "auto_edit", "auto"]
    chat_id = update.effective_chat.id if update.effective_chat else user.id
    target_sk = f"telegram:{chat_id}"

    if not args or args[0].lower() not in valid:
        keyboard = [
            [
                InlineKeyboardButton("🛡️ Standard (Default)", callback_data="perm:default"),
            ],
            [
                InlineKeyboardButton("🔍 Demander confirmation (Request Review)", callback_data="perm:request-review"),
            ],
        ]
        await update.message.reply_text(
            "🛡️ *Politique de permissions des outils sensibles :*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    perm_val = args[0].lower()
    await update_session_remote(permission_mode=perm_val, session_key=target_sk)
    await update.message.reply_text(f"✅ Politique de permissions : `{perm_val}`", parse_mode="Markdown")


async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gère les clics sur les boutons interactifs Telegram."""
    query = update.callback_query
    user = update.effective_user
    if not user or not _is_allowed(user.id):
        try:
            await query.answer("⛔ Accès non autorisé.", show_alert=True)
        except Exception:
            pass
        return
    try:
        await query.answer()
    except Exception:
        pass
    data = query.data or ""

    if data.startswith("totp_deny:"):
        req_id = data.split(":", 1)[1]
        from core.node_hub import NODE_HUB
        await NODE_HUB.send_confirm_response(req_id, decision="deny")
        await _cleanup_pending_totp_request(req_id, update_messages=True, result_status="❌ Demande refusée.")
        return

    if data.startswith("node_session_end:"):
        raw_token = data.split(":", 1)[1].strip() or "all"
        target_nid = _session_end_cb_targets.get(raw_token, raw_token)
        from core.node_hub import NODE_HUB
        closed = await NODE_HUB.send_session_end(node_id=target_nid)
        if closed:
            try:
                await query.edit_message_text(f"🛑 Session de confiance fermée sur {', '.join(closed)}.", reply_markup=None)
            except Exception:
                pass
        else:
            try:
                await query.edit_message_text(f"⚠️ Impossible de joindre « {target_nid} » pour fermer la session (nœud déconnecté ou session déjà expirée).", reply_markup=None)
            except Exception:
                pass
        return

    if data.startswith("reminder:"):
        parts = data.split(":")
        chat_id = query.message.chat_id if query.message else None
        try:
            if len(parts) == 4 and parts[1] == "snooze":
                minutes = int(parts[2])
                reminder_id = int(parts[3])
                reminder = snooze_reminder(
                    reminder_id, f"{minutes}m", chat_id=chat_id
                )
                if reminder is None:
                    await query.edit_message_text(
                        f"❌ Rappel #{reminder_id} introuvable ou déjà traité."
                    )
                    return
                await query.edit_message_text(
                    f"⏰ Rappel #{reminder_id} reporté.\n"
                    f"{format_reminder_confirmation(reminder)}",
                    reply_markup=None,
                )
                return

            if len(parts) == 3 and parts[1] == "done":
                reminder_id = int(parts[2])
                reminder = complete_reminder(reminder_id, chat_id=chat_id)
                if reminder is None:
                    await query.edit_message_text(
                        f"❌ Rappel #{reminder_id} introuvable ou déjà traité."
                    )
                    return
                if (
                    reminder.get("status") == "pending"
                    and reminder.get("completed_occurrence")
                ):
                    await query.edit_message_text(
                        f"✅ Occurrence terminée.\n"
                        f"{format_reminder_confirmation(reminder)}",
                        reply_markup=None,
                    )
                else:
                    await query.edit_message_text(
                        f"✅ Rappel #{reminder_id} terminé.",
                        reply_markup=None,
                    )
                return
        except (TypeError, ValueError):
            pass
        await query.edit_message_text("❌ Action de rappel invalide.")
        return

    s_key = f"telegram:{user.id}"
    if data.startswith("setmodel:"):
        model_id = data.split(":", 1)[1]
        await update_session_remote(model=model_id, session_key=s_key)
        await query.edit_message_text(f"✅ Modèle activé : `{model_id}`", parse_mode="Markdown")

    elif data.startswith("effort:"):
        eff = data.split(":", 1)[1]
        await update_session_remote(effort=eff, session_key=s_key)
        await query.edit_message_text(f"✅ Niveau d'effort réglé sur : `{eff}`", parse_mode="Markdown")

    elif data.startswith("mode:"):
        m = data.split(":", 1)[1]
        await update_session_remote(mode=m, session_key=s_key)
        await query.edit_message_text(f"✅ Mode d'exécution réglé sur : `{m}`", parse_mode="Markdown")

    elif data.startswith("perm:"):
        p = data.split(":", 1)[1]
        await update_session_remote(permission_mode=p, session_key=s_key)
        await query.edit_message_text(f"✅ Politique de permissions : `{p}`", parse_mode="Markdown")

    elif data.startswith("resume:"):
        cid = data.split(":", 1)[1]
        chat_id = query.message.chat_id
        mid = query.message.message_id

        # 1. Efface tous les messages précédents (y compris le menu resume)
        await clear_telegram_chat(context.bot, chat_id, current_message_id=mid)

        # 2. Nouvelle conversation
        if cid == "new":
            await reset_session_remote(session_key=s_key)
            msg = await context.bot.send_message(chat_id=chat_id, text="✨ **Nouvelle conversation prête.**", parse_mode="Markdown")
            track_chat_message(chat_id, msg.message_id)
            return

        # 3. Ancienne conversation
        res = await resume_conversation_remote(cid, session_key=s_key)
        history = res.get("history", [])
        await replay_telegram_history(context.bot, chat_id, history)

    elif data.startswith("delconv:"):
        cid = data.split(":", 1)[1]
        if cid == "cancel":
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        res = await delete_conversation_remote(cid)
        if res.get("success"):
            if cid == "all":
                count = res.get("deleted_count", 0)
                await query.edit_message_text(f"🗑️ **{count} conversation(s) supprimée(s) avec succès !**", parse_mode="Markdown")
            else:
                await query.edit_message_text(f"🗑️ **Conversation `{cid[:8]}...` supprimée avec succès !**", parse_mode="Markdown")
        else:
            await query.edit_message_text(f"❌ Erreur lors de la suppression : {res.get('error', 'Inconnue')}", parse_mode="Markdown")

    elif data.startswith("agenda:"):
        period = data.split(":", 1)[1]
        waiting_msg = await query.message.reply_text(f"⏳ Consultation de votre agenda ({period})...")
        prompt = (
            f"Consulte mon Google Agenda pour la période suivante : '{period}'. "
            "Liste les rendez-vous et événements avec leurs horaires de début et de fin, "
            "leur titre, lieu éventuel et détails pertinents. "
            "RÈGLE STRICTE : Utilise directement l'outil de calendrier Google, sans exécuter de scripts bash ni python. "
            "S'il n'y a aucun événement, confirme-le clairement."
        )
        res = await forward_to_runner(prompt, continue_session=False, timeout=120)
        if not res.get("success"):
            output = f"❌ Erreur agenda : {res.get('error', 'Le serveur maison n\'a pas répondu à temps.')}"
        else:
            output = res.get("output", "").strip() or "📅 Aucun événement trouvé pour cette période."
        await send_or_edit_telegram_response(waiting_msg, output, query.message)

    elif data == "tasks:list":
        waiting_msg = await query.message.reply_text("⏳ Récupération de vos tâches...")
        prompt = (
            "Consulte Google Tasks. "
            "Liste toutes les tâches en cours non terminées avec leur statut et date d'échéance éventuelle. "
            "RÈGLE STRICTE : Utilise directement l'outil Google Tasks, sans exécuter de scripts bash ni python. "
            "S'il n'y en a aucune, indique-le clairement."
        )
        res = await forward_to_runner(prompt, continue_session=False, timeout=120)
        if not res.get("success"):
            output = f"❌ Erreur tâches : {res.get('error', 'Le serveur maison n\'a pas répondu à temps.')}"
        else:
            output = res.get("output", "").strip() or "✅ Aucune tâche en cours."
        await send_or_edit_telegram_response(waiting_msg, output, query.message)

    elif data == "tasks:help_add":
        await query.message.reply_text(
            "➕ <b>Ajouter une tâche Google Tasks :</b>\n\n"
            "Envoyez simplement la commande suivante :\n"
            "<code>/tasks add Acheter du café</code>\n"
            "<i>(remplacez par l'intitulé de votre tâche)</i>",
            parse_mode="HTML",
        )

    elif data == "tasks:help_done":
        await query.message.reply_text(
            "✅ <b>Terminer une tâche Google Tasks :</b>\n\n"
            "Envoyez simplement la commande suivante :\n"
            "<code>/tasks done Acheter du café</code>\n"
            "<i>(remplacez par le nom de la tâche à terminer)</i>",
            parse_mode="HTML",
        )

    elif data.startswith("meteo:"):
        city = data.split(":", 1)[1]
        waiting_msg = await query.message.reply_text(f"⏳ Consultation météo pour {city}...")
        direct = await fetch_weather_direct(city)
        if direct:
            output = direct
        else:
            prompt = (
                f"Consulte la météo actuelle et les prévisions pour la ville de '{city}'.\n"
                "RÈGLE STRICTE : Utilise l'outil météo et donne immédiatement la température actuelle, le ressenti, l'état du ciel, les précipitations éventuelles et un conseil adapté. "
                "Ne lance aucune commande bash ni script python."
            )
            res = await forward_to_runner(prompt, continue_session=False, timeout=120)
            if not res.get("success"):
                output = f"❌ Erreur météo : {res.get('error', 'Le serveur maison n\'a pas répondu à temps.')}"
            else:
                output = res.get("output", "").strip() or "⚠️ Aucune information météo trouvée."
        await send_or_edit_telegram_response(waiting_msg, output, query.message)


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reprend une conversation passée (boutons interactifs ou ID direct)."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    chat_id = update.effective_chat.id
    track_chat_message(chat_id, update.message.message_id)

    args = context.args
    s_key = f"telegram:{user.id}"
    if args:
        cid = args[0].strip()
        await clear_telegram_chat(context.bot, chat_id, current_message_id=update.message.message_id)

        if cid.lower() in ("new", "nouvelle", "reset"):
            await reset_session_remote(session_key=s_key)
            msg = await context.bot.send_message(chat_id=chat_id, text="✨ **Nouvelle conversation prête.**", parse_mode="Markdown")
            track_chat_message(chat_id, msg.message_id)
            return

        res = await resume_conversation_remote(cid, session_key=s_key)
        history = res.get("history", [])
        await replay_telegram_history(context.bot, chat_id, history)
        return

    convs = await fetch_conversations(limit=6)
    if not convs:
        await update.message.reply_text("ℹ️ Aucune conversation passée trouvée.")
        return

    keyboard = [
        [InlineKeyboardButton("➕ [Nouvelle discussion vierge]", callback_data="resume:new")]
    ]
    for c in convs:
        cid = c.get("id", "")
        title = c.get("title", "Discussion")
        date = c.get("date", "")
        btn_label = f"💬 {date} — {title}"[:55]
        keyboard.append([InlineKeyboardButton(btn_label, callback_data=f"resume:{cid}")])

    m = await update.message.reply_text(
        "📂 <b>Reprendre une discussion précédente :</b>\n"
        "<i>Cliquez sur une session ci-dessous pour recharger l'échange :</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )
    track_chat_message(chat_id, m.message_id)


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Supprime une conversation passée (menu interactif avec boutons ou ID direct)."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    chat_id = update.effective_chat.id
    track_chat_message(chat_id, update.message.message_id)

    args = context.args
    if args:
        cid = args[0].strip()
        res = await delete_conversation_remote(cid)
        if res.get("success"):
            if cid.lower() == "all":
                count = res.get("deleted_count", 0)
                msg = await update.message.reply_text(f"🗑️ **{count} conversation(s) supprimée(s) avec succès !**", parse_mode="Markdown")
            else:
                msg = await update.message.reply_text(f"🗑️ **Conversation `{cid[:8]}...` supprimée avec succès !**", parse_mode="Markdown")
        else:
            msg = await update.message.reply_text(f"❌ Erreur : {res.get('error', 'Suppression échouée')}", parse_mode="Markdown")
        track_chat_message(chat_id, msg.message_id)
        return

    convs = await fetch_conversations(limit=10)
    if not convs:
        msg = await update.message.reply_text("ℹ️ Aucune conversation passée à supprimer.")
        track_chat_message(chat_id, msg.message_id)
        return

    keyboard = []
    for c in convs:
        cid = c.get("id", "")
        title = c.get("title", "Discussion")
        date = c.get("date", "")
        btn_text = f"🗑️ [{date}] {title[:28]}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"delconv:{cid}")])

    keyboard.append([
        InlineKeyboardButton("💥 Tout supprimer", callback_data="delconv:all"),
        InlineKeyboardButton("❌ Annuler", callback_data="delconv:cancel"),
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = await update.message.reply_text(
        "🗑️ <b>Sélectionnez la conversation à supprimer définitivement :</b>",
        reply_markup=reply_markup,
        parse_mode="HTML",
    )
    track_chat_message(chat_id, msg.message_id)


async def cmd_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Consulte Google Agenda avec boutons interactifs ou argument."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    args = context.args
    if not args:
        keyboard = [
            [
                InlineKeyboardButton("📅 Aujourd'hui", callback_data="agenda:aujourd'hui"),
                InlineKeyboardButton("🌅 Demain", callback_data="agenda:demain"),
            ],
            [
                InlineKeyboardButton("🗓️ Cette semaine", callback_data="agenda:cette semaine"),
                InlineKeyboardButton("📆 Ce mois-ci", callback_data="agenda:ce mois-ci"),
            ],
        ]
        await update.message.reply_text(
            "📅 <b>Consulter votre Google Agenda :</b>\n<i>Choisissez la période souhaitée :</i>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
        return

    period = " ".join(args).strip()
    waiting_msg = await update.message.reply_text(f"⏳ Consultation de votre agenda ({period})...")
    prompt = (
        f"Consulte mon Google Agenda pour la période suivante : '{period}'. "
        "Liste les rendez-vous et événements avec leurs horaires de début et de fin, "
        "leur titre, lieu éventuel (ou salle de cours) et détails pertinents. "
        "RÈGLE STRICTE : Utilise directement l'outil de calendrier Google, sans exécuter de scripts bash ni python. "
        "S'il n'y a aucun événement, confirme-le clairement."
    )
    res = await forward_to_runner(prompt, continue_session=False, timeout=120)
    if not res.get("success"):
        output = f"❌ Erreur agenda : {res.get('error', 'Le serveur maison n\'a pas répondu à temps.')}"
    else:
        output = res.get("output", "").strip() or "📅 Aucun événement trouvé pour cette période."
    await send_or_edit_telegram_response(waiting_msg, output, update.message)


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gère Google Tasks (lister, ajouter, terminer) avec boutons ou arguments."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    args = context.args
    if not args:
        keyboard = [
            [InlineKeyboardButton("📋 Lister mes tâches en cours", callback_data="tasks:list")],
            [
                InlineKeyboardButton("➕ Aide Ajout", callback_data="tasks:help_add"),
                InlineKeyboardButton("✅ Aide Terminer", callback_data="tasks:help_done"),
            ],
        ]
        await update.message.reply_text(
            "📋 <b>Gestion de Google Tasks :</b>\n\n"
            "• Cliquez sur le bouton ci-dessous pour voir vos tâches\n"
            "• Ou tapez <code>/tasks add &lt;titre&gt;</code> pour en ajouter une\n"
            "• Ou tapez <code>/tasks done &lt;titre&gt;</code> pour la terminer",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
        return

    subcmd = args[0].lower()
    task_text = " ".join(args[1:]).strip()

    if subcmd in ("list", "lister", "ls"):
        waiting_msg = await update.message.reply_text("⏳ Récupération de vos tâches...")
        prompt = (
            "Consulte Google Tasks. "
            "Liste toutes les tâches en cours non terminées avec leur statut et date d'échéance éventuelle. "
            "S'il n'y en a aucune, indique-le clairement."
        )
        res = await forward_to_runner(prompt, continue_session=False, timeout=120)
        if not res.get("success"):
            output = f"❌ Erreur tâches : {res.get('error', 'Le serveur maison n\'a pas répondu à temps.')}"
        else:
            output = res.get("output", "").strip() or "📋 Aucune tâche en cours."
        await send_or_edit_telegram_response(waiting_msg, output, update.message)
    elif subcmd in ("add", "ajouter", "new", "creer"):
        if not task_text:
            await update.message.reply_text("⚠️ Précisez le titre de la tâche. Ex: <code>/tasks add Acheter du pain</code>", parse_mode="HTML")
            return
        waiting_msg = await update.message.reply_text(f"⏳ Ajout de la tâche '{task_text}'...")
        prompt = f"Ajoute une nouvelle tâche dans Google Tasks avec le titre : '{task_text}'."
        res = await forward_to_runner(prompt, continue_session=False, timeout=120)
        if not res.get("success"):
            output = f"❌ Erreur ajout : {res.get('error', 'Le serveur maison n\'a pas répondu à temps.')}"
        else:
            output = res.get("output", "").strip() or "✅ Tâche ajoutée."
        await send_or_edit_telegram_response(waiting_msg, output, update.message)
    elif subcmd in ("done", "terminer", "complete", "finir"):
        if not task_text:
            await update.message.reply_text("⚠️ Précisez le titre de la tâche à terminer. Ex: <code>/tasks done Acheter du pain</code>", parse_mode="HTML")
            return
        waiting_msg = await update.message.reply_text(f"⏳ Finalisation de la tâche '{task_text}'...")
        prompt = f"Marque la tâche '{task_text}' comme terminée dans Google Tasks."
        res = await forward_to_runner(prompt, continue_session=False, timeout=120)
        if not res.get("success"):
            output = f"❌ Erreur : {res.get('error', 'Le serveur maison n\'a pas répondu à temps.')}"
        else:
            output = res.get("output", "").strip() or "✅ Tâche complétée."
        await send_or_edit_telegram_response(waiting_msg, output, update.message)
    else:
        full_query = " ".join(args)
        waiting_msg = await update.message.reply_text("⏳ Traitement de votre demande Google Tasks...")
        prompt = f"Gère Google Tasks selon cette demande : {full_query}."
        res = await forward_to_runner(prompt, continue_session=False, timeout=120)
        if not res.get("success"):
            output = f"❌ Erreur : {res.get('error', 'Le serveur maison n\'a pas répondu à temps.')}"
        else:
            output = res.get("output", "").strip() or "✅ Demande traitée."
        await send_or_edit_telegram_response(waiting_msg, output, update.message)


async def cmd_meteo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Consulte la météo en temps réel (Open-Meteo mondiale sans clé)."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    args = context.args
    if not args:
        keyboard = [
            [
                InlineKeyboardButton("📍 Paris", callback_data="meteo:Paris"),
                InlineKeyboardButton("📍 Toulouse", callback_data="meteo:Toulouse"),
            ],
            [
                InlineKeyboardButton("📍 Lyon", callback_data="meteo:Lyon"),
                InlineKeyboardButton("📍 Marseille", callback_data="meteo:Marseille"),
            ],
        ]
        await update.message.reply_text(
            "🌤️ <b>Météo mondiale en temps réel :</b>\n"
            "<i>Choisissez une ville ou tapez <code>/meteo &lt;nom_de_ville&gt;</code> :</i>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
        return

    city = " ".join(args).strip()
    waiting_msg = await update.message.reply_text(f"⏳ Consultation de la météo pour {city}...")
    direct = await fetch_weather_direct(city)
    if direct:
        output = direct
    else:
        prompt = (
            f"Consulte la météo actuelle et les prévisions pour la ville de '{city}'.\n"
            "RÈGLE STRICTE : Utilise l'outil météo et donne immédiatement la température actuelle, le ressenti, l'état du ciel, les précipitations éventuelles et un conseil adapté. "
            "Ne lance aucune commande bash ni script python."
        )
        res = await forward_to_runner(prompt, continue_session=False, timeout=120)
        if not res.get("success"):
            output = f"❌ Erreur météo : {res.get('error', 'Le serveur maison n\'a pas répondu à temps.')}"
        else:
            output = res.get("output", "").strip() or "⚠️ Aucune information météo trouvée."
    await send_or_edit_telegram_response(waiting_msg, output, update.message)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Effectue une recherche web via DuckDuckGo."""
    if not update.message:
        return
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    if _get_pending_totp_entry_for_chat(update.effective_chat.id) is not None:
        await update.message.reply_text(
            "⏳ Une demande de confirmation est en attente. Saisis ton code à 6 chiffres (en répondant au message de la demande) ou clique sur « ❌ Refuser »."
        )
        return

    args = context.args
    if not args:
        await update.message.reply_text("🔍 Veuillez préciser votre recherche. Ex: <code>/search actualités IA</code>", parse_mode="HTML")
        return

    query = " ".join(args).strip()
    waiting_msg = await update.message.reply_text(f"🔍 Recherche web pour '{query}'...")
    prompt = (
        f"Effectue une recherche sur le web pour la requête suivante : '{query}'.\n"
        "RÈGLE STRICTE : Dès la première recherche effectuée, rédige immédiatement une synthèse claire, concise et structurée avec les chiffres clés et les sources en un seul tour. "
        "Ne lance AUCUNE commande bash ni script python, ne lis aucun fichier local. Sois direct et rapide."
    )
    res = await forward_to_runner(prompt, continue_session=False, timeout=120)
    if not res.get("success"):
        output = f"❌ Erreur recherche : {res.get('error', 'Le serveur maison n\'a pas répondu à temps.')}"
    else:
        output = res.get("output", "").strip() or "⚠️ Aucun résultat trouvé."
    await send_or_edit_telegram_response(waiting_msg, output, update.message)


async def cmd_summarize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Récupère une page web et la résume."""
    if not update.message:
        return
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    if _get_pending_totp_entry_for_chat(update.effective_chat.id) is not None:
        await update.message.reply_text(
            "⏳ Une demande de confirmation est en attente. Saisis ton code à 6 chiffres (en répondant au message de la demande) ou clique sur « ❌ Refuser »."
        )
        return

    args = context.args
    if not args:
        await update.message.reply_text("📄 Veuillez préciser l'URL de la page à résumer. Ex: <code>/summarize https://example.com/article</code>", parse_mode="HTML")
        return

    target_url = args[0].strip()
    waiting_msg = await update.message.reply_text(f"⏳ Lecture et résumé de {target_url}...")
    prompt = (
        f"Consulte le contenu de la page web suivante : '{target_url}'.\n"
        "RÈGLE STRICTE : Résume les points clés de manière synthétique et structurée avec des puces en un seul tour. "
        "Ne lance aucune commande bash ni script python."
    )
    res = await forward_to_runner(prompt, continue_session=False, timeout=120)
    if not res.get("success"):
        output = f"❌ Erreur résumé : {res.get('error', 'Le serveur maison n\'a pas répondu à temps.')}"
    else:
        output = res.get("output", "").strip() or "⚠️ Impossible de résumer la page."
    await send_or_edit_telegram_response(waiting_msg, output, update.message)


async def cmd_compact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Compacte la mémoire de contexte de la conversation en cours."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    s_key = f"telegram:{user.id}"
    waiting_msg = await update.message.reply_text("🧹 Compactage de la mémoire de conversation...")
    res = await forward_to_runner("/compact", continue_session=True, timeout=60, session_key=s_key)
    output = res.get("output", "").strip() or "✅ Mémoire compactée avec succès."
    await send_or_edit_telegram_response(waiting_msg, output, update.message)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche le statut global du système et de l'assistant."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    waiting_msg = await update.message.reply_text("⏳ Vérification des composants...")
    health, session = await asyncio.gather(fetch_health(), fetch_session())

    runner_ok = health.get("status") == "ok"
    agy_ok = health.get("agy_available", False)

    status_lines = [
        "📊 <b>État du Système Assistant Personnel</b>\n",
        f"• <b>Serveur Maison (home-runner) :</b> {'🟢 En ligne' if runner_ok else '🔴 Inaccessible'}",
        f"• <b>Moteur Antigravity (agy) :</b> {'🟢 Opérationnel' if agy_ok else '🔴 Dégradé'}",
        f"• <b>Modèle Actif :</b> <code>{session.get('model', 'défaut')}</code>",
        f"• <b>Effort de réflexion :</b> <code>{session.get('effort') or 'par défaut'}</code>",
        f"• <b>Mode d'exécution :</b> <code>{session.get('mode') or 'auto'}</code>",
        f"• <b>Conversation active :</b> {'🟢 Oui' if session.get('has_active_session') else '⚪ Vierge'}",
        "\n🔧 <b>Outils & Intégrations actifs :</b>",
        "• ✉️ Gmail & Google Calendar",
        "• 📋 Google Tasks",
        "• 🌤️ Météo mondiale (Open-Meteo)",
        "• 🔍 Recherche Web DuckDuckGo",
        "• 📄 Lecteur Web (Fetch)",
        "\n💡 <i>Utilisez <code>/models</code> pour choisir un autre modèle d'IA.</i>",
    ]
    await send_or_edit_telegram_response(waiting_msg, "\n".join(status_lines), update.message)



async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Démarre une nouvelle conversation vierge (vide le chat)."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    chat_id = update.effective_chat.id
    current_mid = update.message.message_id
    track_chat_message(chat_id, current_mid)
    await reset_session_remote(session_key=f"telegram:{user.id}")
    await clear_telegram_chat(context.bot, chat_id, current_message_id=current_mid)
    # Message discret pour garder la discussion active et visible dans Telegram
    msg = await context.bot.send_message(chat_id=chat_id, text="✨ **Nouvelle conversation prête.**", parse_mode="Markdown")
    track_chat_message(chat_id, msg.message_id)


async def cmd_fin_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ferme immédiatement la session de confiance active sur une machine cliente (ou toutes)."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    target_nid = (context.args[0].strip() if context.args else "all") or "all"
    from core.node_hub import NODE_HUB
    closed = await NODE_HUB.send_session_end(node_id=target_nid)
    if closed:
        await update.message.reply_text(f"🛑 Session de confiance fermée sur : {', '.join(closed)}.")
    else:
        await update.message.reply_text("⚠️ Aucune session active n'a pu être fermée (ou machine injoignable : arrête-la sur le PC).")


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche les paramètres de la session active."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    s = await fetch_session(session_key=f"telegram:{user.id}")
    text = (
        "⚙️ *Configuration de la session active :*\n\n"
        f"• *Modèle :* `{s.get('model', 'défaut')}`\n"
        f"• *Effort de réflexion :* `{s.get('effort') or 'par défaut'}`\n"
        f"• *Mode d'exécution :* `{s.get('mode') or 'auto'}`\n"
        f"• *Permissions :* `{s.get('permission_mode', 'default')}`\n"
        f"• *Conversation en cours :* `{'Oui' if s.get('has_active_session') else 'Non (vierge)'}`"
    )
    try:
        from core.node_hub import NODE_HUB
        active_nodes = [n for n in NODE_HUB.list_nodes() if n.get("active_session")]
        if active_nodes:
            text += "\n\n🟢 *Sessions de confiance actives :*"
            for n in active_nodes:
                asess = n["active_session"]
                text += (
                    f"\n• `{n['node_id']}` : {asess.get('reste_actions')} action(s) / "
                    f"{asess.get('reste_s')}s restantes (`/fin_session`)"
                )
    except Exception:
        pass
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


async def _send_broadcast_message_impl(text: str) -> bool:
    """Implémentation de la diffusion, exécutée dans la boucle du bot Telegram."""
    app = get_application()
    html_text = markdown_to_telegram_html(text)
    chunks = split_telegram_message(html_text)
    sent = 0
    for cid in get_saved_chat_ids():
        for chunk in chunks:
            try:
                await app.bot.send_message(chat_id=cid, text=chunk, parse_mode="HTML", disable_web_page_preview=True)
                sent += 1
            except Exception as exc:
                logger.warning("Erreur diffusion Telegram HTML vers %s: %s", cid, exc)
                try:
                    await app.bot.send_message(chat_id=cid, text=text)
                    sent += 1
                except Exception:
                    pass
    return sent > 0


async def send_broadcast_message(text: str) -> bool:
    """Diffuse un message en respectant la boucle asyncio du bot Telegram."""
    loop = _TELEGRAM_LOOP
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if loop is not None and loop.is_running() and loop is not current_loop:
        future = asyncio.run_coroutine_threadsafe(
            _send_broadcast_message_impl(text), loop
        )
        return await asyncio.wrap_future(future)

    return await _send_broadcast_message_impl(text)


async def _send_telegram_message_impl(chat_id: str, text: str) -> None:
    """Envoie un message à un chat Telegram précis, dans la boucle du bot."""
    app = get_application()
    await app.bot.send_message(chat_id=int(chat_id), text=text)


async def send_telegram_message(chat_id: str, text: str) -> None:
    """Envoie un message Telegram en respectant la boucle asyncio du bot."""
    loop = _TELEGRAM_LOOP
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if loop is not None and loop.is_running() and loop is not current_loop:
        future = asyncio.run_coroutine_threadsafe(
            _send_telegram_message_impl(chat_id, text), loop
        )
        await asyncio.wrap_future(future)
        return

    await _send_telegram_message_impl(chat_id, text)


def _reminder_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    """Boutons d'action d'un rappel Telegram."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⏰ +10 min", callback_data=f"reminder:snooze:10:{reminder_id}"
                ),
                InlineKeyboardButton(
                    "⏰ +1 h", callback_data=f"reminder:snooze:60:{reminder_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "✅ Terminé", callback_data=f"reminder:done:{reminder_id}"
                )
            ],
        ]
    )


async def _send_telegram_reminder_impl(reminder: dict) -> None:
    """Envoie un rappel avec ses boutons d'action, dans la boucle Telegram."""
    app = get_application()
    await app.bot.send_message(
        chat_id=int(reminder["chat_id"]),
        text=format_reminder_notification(reminder),
        reply_markup=_reminder_keyboard(int(reminder["id"])),
    )


async def send_telegram_reminder(reminder: dict) -> None:
    """Envoie un rappel Telegram en respectant la boucle asyncio du bot."""
    loop = _TELEGRAM_LOOP
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if loop is not None and loop.is_running() and loop is not current_loop:
        future = asyncio.run_coroutine_threadsafe(
            _send_telegram_reminder_impl(reminder), loop
        )
        await asyncio.wrap_future(future)
        return

    await _send_telegram_reminder_impl(reminder)


# ── Confirmation TOTP distante pour les nœuds clients (nc-noeud) ─────────────

_pending_totp_by_chat: Dict[int, Dict[str, Any]] = {}
_pending_totp_prompts: Dict[str, List[Tuple[int, int, str]]] = {}
_pending_totp_tasks: Dict[str, asyncio.Task] = {}
_handlers_initialized = False


def _extract_totp_code(raw: str) -> Optional[str]:
    """Normalise et extrait un code TOTP à 6 chiffres ASCII (ignore espaces, espaces insécables, tirets, points)."""
    cleaned = re.sub(r"[\s\u00a0\u202f.\-]+", "", raw or "")
    if re.fullmatch(r"[0-9]{6}", cleaned, flags=re.ASCII):
        return cleaned
    return None


async def _call_in_telegram_loop(coro_fn, *args, **kwargs) -> Any:
    """Exécute une coroutine dans la boucle événementielle du bot Telegram si séparée."""
    loop = _TELEGRAM_LOOP
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if loop is not None and loop.is_running() and loop is not current_loop:
        future = asyncio.run_coroutine_threadsafe(coro_fn(*args, **kwargs), loop)
        return await asyncio.wrap_future(future)
    return await coro_fn(*args, **kwargs)


async def _send_totp_request_impl(chat_id: int, text: str, req_id: str) -> Optional[int]:
    """Envoie la demande de confirmation avec le bouton Refuser en texte brut."""
    try:
        app = get_application()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Refuser", callback_data=f"totp_deny:{req_id}")]
        ])
        msg = await app.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        return msg.message_id
    except Exception as exc:
        logger.error("Erreur envoi message confirmation TOTP vers %s : %s", chat_id, exc)
        return None


async def _edit_totp_message_impl(chat_id: int, message_id: int, text: str) -> None:
    """Édite le message de confirmation pour retirer les boutons et afficher l'état final."""
    try:
        app = get_application()
        await app.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=None,
        )
    except Exception as exc:
        logger.debug("Impossible d'éditer le message TOTP %s:%s : %s", chat_id, message_id, exc)


async def _delete_message_safe_impl(chat_id: int, message_id: int) -> None:
    """Supprime un message (ex: code TOTP saisi) sans lever d'exception."""
    try:
        app = get_application()
        await app.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


def _get_pending_totp_entry_for_chat(chat_id: int) -> Optional[Dict[str, Any]]:
    """Retourne l'entrée TOTP active pour un chat donné si non expirée."""
    import time as _time
    entry = _pending_totp_by_chat.get(chat_id)
    if not entry:
        return None
    if _time.time() > entry.get("expires_at", 0):
        _pending_totp_by_chat.pop(chat_id, None)
        return None
    return entry


def _get_pending_totp_for_chat(chat_id: int) -> Optional[str]:
    """Retourne l'identifiant de la requête TOTP en attente pour un chat donné."""
    entry = _get_pending_totp_entry_for_chat(chat_id)
    return entry["req_id"] if entry else None


async def _cleanup_pending_totp_request(req_id: str, update_messages: bool = True, result_status: str = "") -> None:
    """Nettoie une requête TOTP terminée (réponse, refus ou annulation)."""
    task = _pending_totp_tasks.pop(req_id, None)
    if task and not task.done():
        try:
            t_loop = task.get_loop()
            if t_loop.is_running():
                t_loop.call_soon_threadsafe(task.cancel)
            else:
                task.cancel()
        except Exception:
            task.cancel()

    prompts = _pending_totp_prompts.pop(req_id, [])
    for cid, entry in list(_pending_totp_by_chat.items()):
        if entry.get("req_id") == req_id:
            _pending_totp_by_chat.pop(cid, None)

    if update_messages:
        for cid, mid, text in prompts:
            status_text = f"\n\n{result_status}" if result_status else ""
            await _call_in_telegram_loop(_edit_totp_message_impl, cid, mid, f"{text}{status_text}")


async def _expire_pending_totp(req_id: str) -> None:
    """Gère l'expiration d'une requête de confirmation."""
    _pending_totp_tasks.pop(req_id, None)
    prompts = _pending_totp_prompts.pop(req_id, [])
    for cid, entry in list(_pending_totp_by_chat.items()):
        if entry.get("req_id") == req_id:
            _pending_totp_by_chat.pop(cid, None)

    for cid, mid, text in prompts:
        await _call_in_telegram_loop(_edit_totp_message_impl, cid, mid, f"{text}\n\n⏱ Expiré.")


async def handle_node_confirm_request(node_id: str, message: Dict[str, Any]) -> None:
    """Relaye une demande de confirmation sensible vers Telegram."""
    import time as _time
    req_id = message.get("req_id")
    if not req_id or not isinstance(req_id, str):
        return

    summary = str(message.get("summary") or "")
    try:
        raw_exp = message.get("expires_in")
        expires_in = max(1, min(int(float(raw_exp if raw_exp is not None else 32)), 120))
    except (ValueError, TypeError, OverflowError):
        expires_in = 32
    code_required = bool(message.get("code_required", True))

    # Texte brut sans mise en page HTML/Markdown pour préserver fidèlement le résumé
    text = summary
    if code_required:
        text += "\n\n🔐 Réponds à ce message avec le code de ton appli (6 chiffres)."

    # Ciblage strict des utilisateurs explicitement autorisés (TELEGRAM_ALLOWED_USER_IDS), jamais saved_chats ni groupes
    target_chats: set[int] = {cid for cid in ALLOWED_USER_IDS if cid > 0}
    if not target_chats:
        logger.warning("confirm_request : TELEGRAM_ALLOWED_USER_IDS est vide, aucun relais effectué.")
        from core.node_hub import NODE_HUB
        await NODE_HUB.send_confirm_unavailable(req_id, node_id=node_id)
        return

    prompts_list: List[Tuple[int, int, str]] = []
    expires_at = _time.time() + expires_in

    for cid in target_chats:
        # Une seule demande active à la fois par conversation : réserver l'entrée AVANT l'await d'envoi
        if _get_pending_totp_entry_for_chat(cid) is not None:
            logger.warning("confirm_request (%s) refusée dans le chat %s : une demande est déjà en attente.", req_id, cid)
            continue

        _pending_totp_by_chat[cid] = {
            "node_id": node_id,
            "req_id": req_id,
            "message_id": None,
            "expires_at": expires_at,
        }
        msg_id = await _call_in_telegram_loop(_send_totp_request_impl, cid, text, req_id)
        if msg_id:
            if _pending_totp_by_chat.get(cid, {}).get("req_id") == req_id:
                _pending_totp_by_chat[cid]["message_id"] = msg_id
            prompts_list.append((cid, msg_id, text))
        else:
            if _pending_totp_by_chat.get(cid, {}).get("req_id") == req_id:
                _pending_totp_by_chat.pop(cid, None)

    if not prompts_list:
        from core.node_hub import NODE_HUB
        await NODE_HUB.send_confirm_unavailable(req_id, node_id=node_id)
        return

    _pending_totp_prompts[req_id] = prompts_list

    async def _expire_task():
        await asyncio.sleep(expires_in)
        await _expire_pending_totp(req_id)

    _pending_totp_tasks[req_id] = asyncio.create_task(_expire_task())


async def handle_node_confirm_cancel(node_id: str, message: Dict[str, Any]) -> None:
    """Annule l'affichage d'une requête de confirmation expirée ou annulée localement."""
    req_id = message.get("req_id")
    if req_id:
        await _cleanup_pending_totp_request(req_id, update_messages=True, result_status="❌ Annulé par la machine cliente.")


_active_session_messages: Dict[Tuple[int, str], int] = {}
_session_end_cb_targets: Dict[str, str] = {}


def _session_end_callback_data(node_id: str) -> str:
    import hashlib
    if len(node_id.encode("utf-8")) <= 32 and ":" not in node_id:
        cb_key = node_id
    else:
        cb_key = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:16]
    if len(_session_end_cb_targets) >= 128 and cb_key not in _session_end_cb_targets:
        _session_end_cb_targets.pop(next(iter(_session_end_cb_targets)), None)
    _session_end_cb_targets[cb_key] = node_id
    return f"node_session_end:{cb_key}"


async def _upsert_session_message_impl(
    chat_id: int,
    node_id: str,
    text: str,
    active: bool,
) -> None:
    try:
        app = get_application()
        kb = (
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🛑 Fin de session", callback_data=_session_end_callback_data(node_id))]
            ])
            if active
            else None
        )
        key = (chat_id, node_id)
        existing_mid = _active_session_messages.get(key)
        if not active:
            _active_session_messages.pop(key, None)
            if existing_mid:
                try:
                    await app.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=existing_mid,
                        text=text,
                        reply_markup=None,
                    )
                except Exception:
                    pass
            return

        if existing_mid:
            try:
                await app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=existing_mid,
                    text=text,
                    reply_markup=kb,
                )
                return
            except Exception:
                _active_session_messages.pop(key, None)

        msg = await app.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        if msg and getattr(msg, "message_id", None):
            _active_session_messages[key] = msg.message_id
    except Exception as exc:
        logger.debug("Erreur mise à jour message session Telegram (%s, %s): %s", chat_id, node_id, exc)


async def handle_node_session_update(
    node_id: str,
    session_info: Optional[Dict[str, Any]],
    origin: Optional[Dict[str, Any]] = None,
) -> None:
    """Affiche ou met à jour l'état de la session de confiance dans Telegram avec le bouton Fin de session."""
    target_chats: set[int] = set()
    if isinstance(origin, dict) and origin.get("via") == "telegram":
        raw_chat = origin.get("chat")
        try:
            cid = int(raw_chat)
            if cid in ALLOWED_USER_IDS and cid > 0:
                target_chats.add(cid)
        except (ValueError, TypeError):
            pass

    if not target_chats:
        if session_info is None:
            for cid, nid in list(_active_session_messages.keys()):
                if nid == node_id:
                    target_chats.add(cid)
        else:
            target_chats = {cid for cid in ALLOWED_USER_IDS if cid > 0}

    if not target_chats:
        return

    if isinstance(session_info, dict):
        try:
            reste_s = max(0, int(session_info.get("reste_s", 0)))
            reste_actions = max(0, int(session_info.get("reste_actions", 0)))
        except (ValueError, TypeError):
            reste_s = 0
            reste_actions = 0
        if reste_s > 0 and reste_actions > 0:
            mins, secs = divmod(reste_s, 60)
            text = (
                f"🟢 Session de confiance active sur {node_id}\n"
                f"⏱ Temps restant : {mins}m {secs:02d}s ({reste_s}s)\n"
                f"⚡ Actions couvertes restantes : {reste_actions}"
            )
            for cid in target_chats:
                await _call_in_telegram_loop(_upsert_session_message_impl, cid, node_id, text, True)
            return

    text = f"🛑 Session de confiance terminée sur {node_id}."
    for cid in target_chats:
        await _call_in_telegram_loop(_upsert_session_message_impl, cid, node_id, text, False)


def init_node_hub_confirm_handlers() -> None:
    """Branche les handlers de confirmation distante et de session de confiance sur le NODE_HUB."""
    global _handlers_initialized
    if _handlers_initialized:
        return
    _handlers_initialized = True
    try:
        from core.node_hub import NODE_HUB
        NODE_HUB.on_confirm_request(handle_node_confirm_request)
        NODE_HUB.on_confirm_cancel(handle_node_confirm_cancel)
        NODE_HUB.on_session_update(handle_node_session_update)
        logger.info("Handlers de confirmation TOTP et de session enregistrés sur NODE_HUB.")
    except Exception as exc:
        logger.error("Erreur enregistrement handlers confirmation TOTP sur NODE_HUB: %s", exc)


def _format_memory(memory: dict) -> str:
    """Formate le profil et les souvenirs pour Telegram."""
    profile = memory.get("profile", {})
    facts = memory.get("facts", [])
    if not profile and not facts:
        return "🧠 Aucune mémoire durable enregistrée."
    lines = ["🧠 Mémoire durable"]
    if profile:
        lines.append("\nProfil :")
        for key, value in sorted(profile.items()):
            lines.append(f"• {key} : {value}")
    if facts:
        lines.append("\nSouvenirs :")
        for fact in facts:
            lines.append(f"• {fact['id']} — {fact['text']}")
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3790].rstrip() + "\n…"
    return text


async def cmd_souviens(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ajoute un souvenir durable : /souviens <texte>."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text(
            "Utilisation : `/souviens <texte>`. Ex: `/souviens J'aime le café sans sucre`.",
            parse_mode="Markdown",
        )
        return

    result = await add_memory_fact(text)
    if not result.get("success"):
        await update.message.reply_text(
            f"❌ Impossible d'enregistrer le souvenir : {result.get('error', 'inconnue')}"
        )
        return
    fact = result.get("fact", {})
    await update.message.reply_text(
        f"🧠 Souvenir enregistré ({fact.get('id', '?')}) : {fact.get('text', text)}"
    )


async def cmd_oublie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Supprime un souvenir ou toute la liste : /oublie <id|all>."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    if not context.args:
        await update.message.reply_text(
            "Utilisation : `/oublie <id>` ou `/oublie all`.",
            parse_mode="Markdown",
        )
        return

    target = context.args[0].strip()
    if target.lower() in {"all", "tout", "tous"}:
        result = await clear_memory_facts()
        if not result.get("success"):
            await update.message.reply_text(
                f"❌ Erreur : {result.get('error', 'inconnue')}"
            )
            return
        await update.message.reply_text(
            f"🧹 {result.get('deleted_count', 0)} souvenir(s) supprimé(s)."
        )
        return

    result = await delete_memory_fact(target)
    if not result.get("success"):
        await update.message.reply_text(f"❌ Souvenir {target} introuvable.")
        return
    await update.message.reply_text(f"🧹 Souvenir {target} supprimé.")


async def cmd_profil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche ou modifie le profil durable."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    args = context.args or []
    if not args:
        result = await fetch_memory()
        if not result.get("success"):
            await update.message.reply_text(
                f"❌ Impossible de lire la mémoire : {result.get('error', 'inconnue')}"
            )
            return
        await update.message.reply_text(
            _format_memory(result.get("memory", {}))
        )
        return

    if args[0].lower() in {"suppr", "supprime", "delete"}:
        if len(args) < 2:
            await update.message.reply_text("Utilisation : `/profil suppr <clé>`.")
            return
        result = await delete_memory_profile(args[1])
        if not result.get("success"):
            await update.message.reply_text(f"❌ Préférence {args[1]} introuvable.")
            return
        await update.message.reply_text(f"🧹 Préférence {args[1]} supprimée.")
        return

    if len(args) < 2:
        await update.message.reply_text(
            "Utilisation : `/profil <clé> <valeur>`. Ex: `/profil ville Toulouse`.",
            parse_mode="Markdown",
        )
        return

    key = args[0]
    value = " ".join(args[1:]).strip()
    result = await set_memory_profile(key, value)
    if not result.get("success"):
        await update.message.reply_text(
            f"❌ Impossible de modifier le profil : {result.get('error', 'inconnue')}"
        )
        return
    await update.message.reply_text(f"🧠 Profil mis à jour : {key} = {value}")


_MEMORY_CAPTURE_RE = re.compile(
    r"^\s*(?:souviens[- ]toi|retiens|m[ée]morise)"
    r"(?:\s+que|\s+qu['’]|\s*:)\s+(.+)",
    re.IGNORECASE,
)


async def _try_capture_memory(
    update: Update, prompt: str, waiting_msg
) -> bool:
    """Détecte « souviens-toi que... » et enregistre un souvenir."""
    match = _MEMORY_CAPTURE_RE.match(prompt)
    if not match:
        return False
    text = match.group(1).strip()
    if not text:
        return False
    result = await add_memory_fact(text)
    if not result.get("success"):
        return False
    fact = result.get("fact", {})
    logger.info("Souvenir mémoire créé (#%s) : %s", fact.get("id"), text)
    await waiting_msg.edit_text(
        f"🧠 Souvenir enregistré ({fact.get('id', '?')}) : {fact.get('text', text)}"
    )
    return True


async def cmd_rappel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Crée un rappel : /rappel <durée> <message>."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    _save_chat_id(update.effective_chat.id)
    raw = " ".join(context.args or []).strip()
    if not raw:
        await update.message.reply_text(
            "⏰ Utilisation : `/rappel [récurrence] <durée> <message>`\n"
            "Exemples :\n"
            "• `/rappel 15m Sortir le pain`\n"
            "• `/rappel 14h30 Appeler le médecin`\n"
            "• `/rappel chaque jour 8h Faire mes étirements`\n"
            "• `/rappel tous les lundis 9h Réunion d'équipe`",
            parse_mode="Markdown",
        )
        return

    remind_at, message, recurrence, error = parse_reminder_command(raw)
    if remind_at is None or not message:
        await update.message.reply_text(f"❌ {error}")
        return

    reminder = add_reminder(
        platform="telegram",
        chat_id=update.effective_chat.id,
        target_id=user.id,
        message=message,
        remind_at=remind_at,
        target_name=getattr(user, "full_name", "") or "",
        recurrence=recurrence,
    )
    logger.info(
        "Rappel #%s créé via /rappel (telegram -> %s, récurrence=%s) : %s",
        reminder["id"],
        update.effective_chat.id,
        recurrence,
        message,
    )
    await update.message.reply_text(format_reminder_confirmation(reminder))


async def cmd_rappels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Liste les rappels actifs du chat Telegram."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    reminders = list_active_reminders(update.effective_chat.id)
    if not reminders:
        await update.message.reply_text("⏰ Aucun rappel actif.")
        return

    lines = ["⏰ Rappels actifs :"]
    for reminder in reminders:
        line = (
            f"• #{reminder['id']} — {reminder['message']} — "
            f"{format_remaining_time(reminder['remind_at'])}"
        )
        if reminder.get("recurrence"):
            line += f" — 🔁 {recurrence_label(reminder['recurrence'])}"
        lines.append(line)
    await update.message.reply_text("\n".join(lines))


async def cmd_annuler_rappel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Annule un rappel : /annuler_rappel <id>."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    if not context.args:
        await update.message.reply_text(
            "Utilisation : `/annuler_rappel <id>`. Utilisez `/rappels` pour voir les IDs.",
            parse_mode="Markdown",
        )
        return

    try:
        reminder_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ L'ID doit être un nombre.")
        return

    if cancel_reminder(reminder_id, update.effective_chat.id):
        logger.info("Rappel #%s annulé (telegram).", reminder_id)
        await update.message.reply_text(f"✅ Rappel #{reminder_id} annulé.")
    else:
        await update.message.reply_text(
            f"❌ Aucun rappel actif #{reminder_id} dans ce chat."
        )


async def cmd_snooze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reporte un rappel : /snooze <id> [durée]."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Utilisation : `/snooze <id> [durée]`. Ex: `/snooze 4 10m`.",
            parse_mode="Markdown",
        )
        return

    try:
        reminder_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ L'ID doit être un nombre.")
        return

    duration = args[1] if len(args) > 1 else "10m"
    if duration.isdigit():
        duration += "m"

    reminder = snooze_reminder(
        reminder_id, duration, chat_id=update.effective_chat.id
    )
    if reminder is None:
        await update.message.reply_text(
            f"❌ Aucun rappel actif #{reminder_id} dans ce chat, ou durée invalide."
        )
        return

    await update.message.reply_text(
        f"⏰ Rappel #{reminder_id} reporté : {format_reminder_confirmation(reminder)}"
    )


async def cmd_rappel_termine(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Clôt un rappel : /rappel_termine <id>."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Utilisation : `/rappel_termine <id>`.",
            parse_mode="Markdown",
        )
        return

    try:
        reminder_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ L'ID doit être un nombre.")
        return

    reminder = complete_reminder(reminder_id, chat_id=update.effective_chat.id)
    if reminder is None:
        await update.message.reply_text(
            f"❌ Aucun rappel actif #{reminder_id} dans ce chat."
        )
        return

    if reminder.get("status") == "pending" and reminder.get("completed_occurrence"):
        await update.message.reply_text(
            f"✅ Occurrence terminée.\n{format_reminder_confirmation(reminder)}"
        )
    else:
        await update.message.reply_text(f"✅ Rappel #{reminder_id} terminé.")


async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Génère le briefing matinal complet (tri des e-mails, agenda, météo)."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    _save_chat_id(update.effective_chat.id)
    waiting_msg = await update.message.reply_text("⏳ Préparation de votre briefing matinal (analyse e-mails, agenda, météo)...")
    res = await get_briefing_text(timeout=240)
    output = res.get("output", "").strip() or "⚠️ Impossible de générer le briefing matinal."
    await send_or_edit_telegram_response(waiting_msg, output, update.message)


async def _try_create_natural_reminder(
    update: Update, prompt: str, waiting_msg
) -> bool:
    """Crée un rappel si le message contient une formulation naturelle.

    Retourne True si un rappel a été créé (le message ne doit alors pas être
    transmis à agy).
    """
    detected = detect_natural_reminder_full(prompt)
    if not detected:
        return False

    remind_at, message, recurrence = detected
    try:
        reminder = add_reminder(
            platform="telegram",
            chat_id=update.effective_chat.id,
            target_id=update.effective_user.id,
            message=message,
            remind_at=remind_at,
            target_name=getattr(update.effective_user, "full_name", "") or "",
            recurrence=recurrence,
        )
    except Exception as exc:
        logger.error("Erreur création rappel naturel Telegram: %s", exc)
        return False

    logger.info(
        "Rappel #%s créé (telegram -> %s) pour %s : %s",
        reminder["id"],
        update.effective_chat.id,
        reminder["remind_at"],
        message,
    )
    await waiting_msg.edit_text(format_reminder_confirmation(reminder))
    return True


async def _process_prompt_and_reply(update: Update, prompt: str, waiting_msg) -> None:
    """Envoie un prompt au runner et édite le message d'attente avec la réponse."""
    if await _try_capture_memory(update, prompt, waiting_msg):
        return
    if await _try_create_natural_reminder(update, prompt, waiting_msg):
        return

    user = update.effective_user
    user_id = user.id if user else (update.effective_chat.id if update.effective_chat else "unknown")
    session_key = f"telegram:{user_id}"
    result = await forward_to_runner(prompt, continue_session=True, session_key=session_key)

    if result.get("success"):
        response_text = result.get("output", "").strip() or "✅ Tâche terminée (pas de sortie)."
    else:
        error = result.get("error", "Erreur inconnue")
        response_text = f"❌ Erreur : {error}"
        logger.error("Erreur retournée par le runner: %s", error)

    await send_or_edit_telegram_response(waiting_msg, response_text, update.message)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Traite les messages texte entrants avec mémoire de contexte conversationnel et mise en page riche."""
    if not update.message or not update.message.text:
        return
    user = update.effective_user
    if not _is_allowed(user.id):
        logger.warning("Message refusé de l'utilisateur Telegram %d.", user.id)
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    chat_id = update.effective_chat.id
    _save_chat_id(chat_id)
    track_chat_message(chat_id, update.message.message_id)

    prompt = update.message.text.strip()
    if not prompt:
        return

    # Vérification code TOTP à 6 chiffres (normalisé : espaces, insécables, tirets, points)
    # Traiter immédiatement SANS journaliser le code, SANS l'envoyer au runner/LLM, et supprimer le message du code
    totp_code = _extract_totp_code(prompt)
    pending_entry = _get_pending_totp_entry_for_chat(chat_id)
    if totp_code is not None:
        try:
            await update.message.delete()
        except Exception:
            await _call_in_telegram_loop(_delete_message_safe_impl, chat_id, update.message.message_id)

        if pending_entry is not None:
            reply_msg = getattr(update.message, "reply_to_message", None)
            reply_id = getattr(reply_msg, "message_id", None)
            expected_mid = pending_entry.get("message_id")
            if not expected_mid or reply_id != expected_mid:
                await update.message.reply_text(
                    "↩️ Pour lier ton code à la demande lue, réponds directement au message de la demande "
                    "(« Répondre » / glisser vers la gauche) avec tes 6 chiffres."
                )
                return

            req_id = pending_entry["req_id"]
            node_id = pending_entry.get("node_id")
            from core.node_hub import NODE_HUB
            success = await NODE_HUB.send_confirm_response(req_id, decision="allow", code=totp_code, node_id=node_id)
            await _cleanup_pending_totp_request(req_id, update_messages=True, result_status="✅ Code transmis.")
            if success:
                await update.message.reply_text("✅ Code transmis au nœud client.")
            else:
                await update.message.reply_text("❌ Échec de transmission du code (demande expirée ou nœud déconnecté).")
        else:
            await update.message.reply_text("⏱ Aucun code n'est attendu actuellement (la demande a expiré ou a déjà été traitée).")
        return

    # Tant qu'une demande de confirmation attend dans ce chat, ne rien envoyer ni journaliser vers le runner
    if pending_entry is not None:
        await update.message.reply_text(
            "🔐 Une demande de confirmation est en attente dans ce chat. "
            "Réponds directement au message de la demande avec ton code à 6 chiffres ou clique sur « ❌ Refuser »."
        )
        return

    logger.info("Message Telegram reçu de %d: '%s'", user.id, prompt[:80])

    # Routeur NLP local 0-token (<5ms) pour intents natifs (courses, budget, obsidian, pc, focus)
    from core.tools.bot_telegram_handlers import try_nlp_route
    if await try_nlp_route(update, context, prompt):
        return

    # Envoie un message temporaire "en cours de traitement"
    waiting_msg = await update.message.reply_text("⏳ Traitement en cours...")

    # Exécution avec continuité de session activée (multi-turn)
    await _process_prompt_and_reply(update, prompt, waiting_msg)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Traite les images et photos envoyées par l'utilisateur pour analyse multimodale."""
    user = update.effective_user
    if not _is_allowed(user.id):
        logger.warning("Photo refusée de l'utilisateur Telegram %d.", user.id)
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    chat_id = update.effective_chat.id
    _save_chat_id(chat_id)
    track_chat_message(chat_id, update.message.message_id)

    caption_raw = (update.message.caption or "").strip()
    if _extract_totp_code(caption_raw) is not None or _get_pending_totp_entry_for_chat(chat_id) is not None:
        await update.message.reply_text(
            "🔐 Une demande de confirmation est en attente dans ce chat. "
            "Réponds en texte au message de la demande avec ton code à 6 chiffres ou clique sur « ❌ Refuser »."
        )
        return

    # Récupération du fichier image (photo ou document)
    photo_file = None
    filename = "upload.jpg"
    if update.message.photo:
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()
        filename = f"photo_{photo.file_unique_id}.jpg"
    elif update.message.document and update.message.document.mime_type and update.message.document.mime_type.startswith("image/"):
        photo_file = await update.message.document.get_file()
        filename = update.message.document.file_name or "image.png"

    if not photo_file:
        return

    waiting_msg = await update.message.reply_text("👁️ Réception et analyse de l'image...")
    try:
        photo_bytes = await photo_file.download_as_bytearray()
        b64 = base64.b64encode(photo_bytes).decode("utf-8")
        caption = (update.message.caption or "").strip()
        prompt = caption if caption else "Analyse cette image en détail et décris ce qu'elle contient ou réponds aux éléments visibles."

        logger.info("Image Telegram reçue de %d (%s, %d octets). Prompt: '%s'", user.id, filename, len(photo_bytes), prompt[:60])
        session_key = f"telegram:{user.id}"
        result = await forward_to_runner(
            prompt,
            continue_session=True,
            image_base64=b64,
            image_filename=filename,
            session_key=session_key,
        )

        if result.get("success"):
            response_text = result.get("output", "").strip() or "✅ Analyse terminée."
        else:
            error = result.get("error", "Erreur inconnue")
            response_text = f"❌ Erreur : {error}"

        await send_or_edit_telegram_response(waiting_msg, response_text, update.message)
    except Exception as exc:
        logger.error("Erreur traitement image Telegram: %s", exc)
        await waiting_msg.edit_text(f"❌ Erreur lors du traitement de l'image : {exc}")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Transcrit un message vocal/audio Telegram puis le traite comme un texte."""
    user = update.effective_user
    if not _is_allowed(user.id):
        logger.warning("Message vocal refusé de l'utilisateur Telegram %d.", user.id)
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    chat_id = update.effective_chat.id
    _save_chat_id(chat_id)
    track_chat_message(chat_id, update.message.message_id)

    media = update.message.voice or update.message.audio
    if media is None:
        return

    if _get_pending_totp_entry_for_chat(chat_id) is not None:
        await update.message.reply_text(
            "🔐 Une demande de confirmation est en attente dans ce chat. "
            "Saisis ton code à 6 chiffres en texte (en réponse au message de la demande) ou clique sur « ❌ Refuser »."
        )
        return

    file_size = int(getattr(media, "file_size", 0) or 0)
    if file_size > MAX_AUDIO_BYTES:
        await update.message.reply_text(
            f"❌ Fichier audio trop volumineux "
            f"({file_size / 1024 / 1024:.1f} Mo)."
        )
        return

    waiting_msg = await update.message.reply_text("🎤 Transcription en cours...")
    suffix = Path(getattr(media, "file_name", "") or "").suffix or ".ogg"
    temp_path = Path(f"/tmp/telegram_voice_{secrets.token_hex(8)}{suffix}")

    try:
        tg_file = await media.get_file()
        await tg_file.download_to_drive(custom_path=str(temp_path))
        text, engine = await transcribe_audio(temp_path)

        if not text:
            await waiting_msg.edit_text(
                "❌ Je n'ai pas réussi à transcrire ce message vocal. "
                "Réessayez ou envoyez-le en texte."
            )
            return

        if _extract_totp_code(text) is not None or _get_pending_totp_entry_for_chat(chat_id) is not None:
            try:
                await update.message.delete()
            except Exception:
                await _call_in_telegram_loop(_delete_message_safe_impl, chat_id, update.message.message_id)
            await waiting_msg.edit_text(
                "🔐 Par sécurité, un code à 6 chiffres ne doit pas être dicté en vocal. "
                "Réponds en texte directement au message de la demande."
            )
            return

        logger.info(
            "Message vocal Telegram transcrit (%s) : '%s'", engine, text[:200]
        )
        from core.tools.bot_telegram_handlers import try_nlp_route
        if await try_nlp_route(update, context, text):
            await waiting_msg.edit_text(f"🎤 « {text} »")
            return
        await waiting_msg.edit_text(f"🎤 « {text} »\n\n⏳ Traitement en cours...")
        await _process_prompt_and_reply(update, text, waiting_msg)

    except Exception as exc:
        logger.error("Erreur traitement vocal Telegram: %s", exc)
        try:
            await waiting_msg.edit_text(
                f"❌ Erreur lors du traitement du message vocal : {exc}"
            )
        except Exception:
            pass
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


from telegram.request import HTTPXRequest

# ── Application Telegram ──────────────────────────────────────────────────────

BOT_COMMANDS = [
    BotCommand("briefing", "Briefing matinal (mails triés, agenda, météo)"),
    BotCommand("rappel", "Programmer un rappel (ponctuel ou récurrent)"),
    BotCommand("rappels", "Lister vos rappels actifs"),
    BotCommand("snooze", "Reporter un rappel (+10 min par défaut)"),
    BotCommand("rappel_termine", "Clôturer un rappel"),
    BotCommand("annuler_rappel", "Annuler un rappel par son numéro"),
    BotCommand("souviens", "Ajouter un souvenir durable"),
    BotCommand("oublie", "Supprimer un souvenir"),
    BotCommand("profil", "Afficher ou modifier votre profil"),
    BotCommand("resume", "Reprendre une discussion précédente"),
    BotCommand("delete", "Supprimer des discussions passées"),
    BotCommand("agenda", "Consulter Google Agenda (aujourd'hui, semaine)"),
    BotCommand("tasks", "Gérer Google Tasks (lister, ajouter, terminer)"),
    BotCommand("meteo", "Météo mondiale en temps réel"),
    BotCommand("search", "Recherche web DuckDuckGo"),
    BotCommand("summarize", "Résumer une page web ou un lien"),
    BotCommand("status", "Statut du système et des composants"),
    BotCommand("compact", "Compacter la mémoire de discussion"),
    BotCommand("models", "Choisir le modèle d'IA (boutons cliquables)"),
    BotCommand("new", "Démarrer une nouvelle discussion"),
    BotCommand("session", "Voir les réglages actifs"),
    BotCommand("fin_session", "Fermer la session de confiance active"),
    BotCommand("effort", "Régler l'effort de réflexion"),
    BotCommand("mode", "Changer le mode d'action (auto, plan)"),
    BotCommand("permissions", "Régler les permissions (auto/manuel)"),
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
    app.add_handler(CommandHandler("rappel", cmd_rappel))
    app.add_handler(CommandHandler("reminder", cmd_rappel))
    app.add_handler(CommandHandler("rappels", cmd_rappels))
    app.add_handler(CommandHandler("reminders", cmd_rappels))
    app.add_handler(CommandHandler("snooze", cmd_snooze))
    app.add_handler(CommandHandler("report", cmd_snooze))
    app.add_handler(CommandHandler("rappel_termine", cmd_rappel_termine))
    app.add_handler(CommandHandler("rappel_done", cmd_rappel_termine))
    app.add_handler(CommandHandler("annuler_rappel", cmd_annuler_rappel))
    app.add_handler(CommandHandler("delrappel", cmd_annuler_rappel))
    app.add_handler(CommandHandler("souviens", cmd_souviens))
    app.add_handler(CommandHandler("retiens", cmd_souviens))
    app.add_handler(CommandHandler("oublie", cmd_oublie))
    app.add_handler(CommandHandler("profil", cmd_profil))
    app.add_handler(CommandHandler("memoire", cmd_profil))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("del", cmd_delete))
    app.add_handler(CommandHandler("delconv", cmd_delete))
    app.add_handler(CommandHandler("agenda", cmd_agenda))
    app.add_handler(CommandHandler("cal", cmd_agenda))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("task", cmd_tasks))
    app.add_handler(CommandHandler("todo", cmd_tasks))
    app.add_handler(CommandHandler("meteo", cmd_meteo))
    app.add_handler(CommandHandler("weather", cmd_meteo))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("recherche", cmd_search))
    app.add_handler(CommandHandler("summarize", cmd_summarize))
    app.add_handler(CommandHandler("compact", cmd_compact))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("models", cmd_models))
    app.add_handler(CommandHandler("model", cmd_models))
    app.add_handler(CommandHandler("setmodel", cmd_setmodel))
    app.add_handler(CommandHandler("effort", cmd_effort))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("permissions", cmd_permissions))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("clear", cmd_new))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("fin_session", cmd_fin_session))
    app.add_handler(CommandHandler("stop_session", cmd_fin_session))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("quota", cmd_usage))
    app.add_handler(CommandHandler("skills", cmd_skills))

    # Handler pour les boutons interactifs (clics)
    app.add_handler(CallbackQueryHandler(on_callback_query))

    # Handler messages texte normaux
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, handle_message))

    # Handler images et photos
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & (filters.PHOTO | (filters.Document.IMAGE)), handle_photo))

    # Handler messages vocaux et fichiers audio
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & (filters.VOICE | filters.AUDIO), handle_voice))

    # Handlers modulaires (Moodle, Budget, Courses, Obsidian, PC, etc.)
    from core.tools.bot_telegram_handlers import register_modular_telegram_commands
    register_modular_telegram_commands(app)

    # Initialisation des écouteurs de confirmation distante TOTP pour les nœuds clients
    init_node_hub_confirm_handlers()

    _application = app
    logger.info("Application Telegram configurée avec l'ensemble des commandes et boutons interactifs.")
    return _application


async def setup_webhook(app: Application, webhook_url: str) -> None:
    """Enregistre le webhook Telegram auprès de l'API Telegram et configure les commandes."""
    webhook_kwargs = {"url": webhook_url}
    if TELEGRAM_WEBHOOK_SECRET:
        webhook_kwargs["secret_token"] = TELEGRAM_WEBHOOK_SECRET
        logger.info("Webhook Telegram protégé par un secret token.")
    else:
        logger.warning(
            "TELEGRAM_WEBHOOK_SECRET absent et SECRET_KEY vide : "
            "le webhook Telegram n'est pas authentifié."
        )

    await app.bot.set_webhook(**webhook_kwargs)
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
            global _TELEGRAM_LOOP
            _TELEGRAM_LOOP = asyncio.get_running_loop()
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

        while not _stop_event.is_set():
            try:
                loop.run_until_complete(_async_polling())
                break
            except Exception as exc:
                if _stop_event.is_set():
                    break
                logger.error("Erreur dans le polling Telegram: %s. Nouvelle tentative dans 5s...", exc)
                import time
                time.sleep(5)
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
    if TELEGRAM_WEBHOOK_SECRET:
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secrets.compare_digest(provided, TELEGRAM_WEBHOOK_SECRET):
            logger.warning("Webhook Telegram refusé : secret token absent ou invalide.")
            return Response(content="Forbidden", status_code=403)

    tg_app = get_application()
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)

    async with tg_app:
        await tg_app.process_update(update)

    return Response(content="OK", status_code=200)
