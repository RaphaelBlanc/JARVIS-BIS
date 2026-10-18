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
from typing import Optional

from formatters import (
    extract_and_render_latex,
    extract_downloadable_code,
    latex_to_unicode,
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
    delete_conversation_remote,
    fetch_conversations,
    fetch_health,
    fetch_models,
    fetch_session,
    forward_to_runner,
    reset_session_remote,
    resume_conversation_remote,
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
        "• `/status` — État complet du système et des services\n\n"
        "💬 *Discussion & Mémoire*\n"
        "• Écrivez simplement votre message pour échanger naturellement.\n"
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
    user = update.effective_user
    if not user or not _is_allowed(user.id):
        await query.answer("⛔ Accès non autorisé.", show_alert=True)
        return
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

    elif data.startswith("resume:"):
        cid = data.split(":", 1)[1]
        chat_id = query.message.chat_id
        mid = query.message.message_id

        # 1. Efface tous les messages précédents (y compris le menu resume)
        await clear_telegram_chat(context.bot, chat_id, current_message_id=mid)

        # 2. Nouvelle conversation
        if cid == "new":
            await reset_session_remote()
            msg = await context.bot.send_message(chat_id=chat_id, text="✨ **Nouvelle conversation prête.**", parse_mode="Markdown")
            track_chat_message(chat_id, msg.message_id)
            return

        # 3. Ancienne conversation
        res = await resume_conversation_remote(cid)
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
    if args:
        cid = args[0].strip()
        await clear_telegram_chat(context.bot, chat_id, current_message_id=update.message.message_id)

        if cid.lower() in ("new", "nouvelle", "reset"):
            await reset_session_remote()
            msg = await context.bot.send_message(chat_id=chat_id, text="✨ **Nouvelle conversation prête.**", parse_mode="Markdown")
            track_chat_message(chat_id, msg.message_id)
            return

        res = await resume_conversation_remote(cid)
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
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
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
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
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

    waiting_msg = await update.message.reply_text("🧹 Compactage de la mémoire de conversation...")
    res = await forward_to_runner("/compact", continue_session=True, timeout=60)
    output = res.get("output", "").strip() or "✅ Mémoire compactée avec succès."
    await send_or_edit_telegram_response(waiting_msg, output, update.message)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche le statut global du système et de l'assistant."""
    user = update.effective_user
    if not _is_allowed(user.id):
        await update.message.reply_text("⛔ Accès non autorisé.")
        return

    waiting_msg = await update.message.reply_text("⏳ Vérification des composants...")
    health = await fetch_health()
    session = await fetch_session()

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
    await reset_session_remote()
    await clear_telegram_chat(context.bot, chat_id, current_message_id=current_mid)
    # Message discret pour garder la discussion active et visible dans Telegram
    msg = await context.bot.send_message(chat_id=chat_id, text="✨ **Nouvelle conversation prête.**", parse_mode="Markdown")
    track_chat_message(chat_id, msg.message_id)


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
    "Effectue mon briefing matinal rapide, motivant et structuré :\n"
    "1. 📅 EMPLOI DU TEMPS DU JOUR (Heure / Cours / Salles) :\n"
    "Consulte mon Google Agenda aujourd'hui (en incluant mon calendrier de cours EPITA/PREPA). "
    "Liste uniquement les horaires, le cours et la salle exacte : • Début - Fin : Intitulé (📍 Salle). "
    "S'il n'y a pas de cours, mentionne-le brièvement.\n\n"
    "2. 📈 ACTUALITÉS ÉCONOMIQUES :\n"
    "Recherche rapidement les 2 faits économiques ou tech marquants de la veille (1 seule recherche concise) et résume-les en 2 puces percutantes.\n\n"
    "3. ✉️ E-MAILS IMPORTANTS :\n"
    "Consulte mes 5 derniers e-mails récents via Gmail. Ignore totalement spams, pubs et newsletters. "
    "Ne retiens que les messages clés (EPITA, examens, personnel) et résume chaque message en 1 ligne.\n\n"
    "4. 🌤️ MÉTÉO DU JOUR :\n"
    "Météo du jour pour ma ville (température, ciel, pluie).\n\n"
    "5. 📋 TÂCHES EN COURS :\n"
    "Consulte mes tâches Google Tasks non terminées si disponibles.\n\n"
    "CONSIGNE DE STYLE : Sois ultra-synthétique, direct et dynamique. Pas de bavardage, va droit au but avec des puces courtes."
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
    res = await forward_to_runner(BRIEFING_PROMPT, continue_session=False, timeout=240)
    output = res.get("output", "").strip() or "⚠️ Impossible de générer le briefing matinal."
    await send_or_edit_telegram_response(waiting_msg, output, update.message)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Traite les messages texte entrants avec mémoire de contexte conversationnel et mise en page riche."""
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
        result = await forward_to_runner(
            prompt,
            continue_session=True,
            image_base64=b64,
            image_filename=filename,
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



from telegram.request import HTTPXRequest

# ── Application Telegram ──────────────────────────────────────────────────────

BOT_COMMANDS = [
    BotCommand("briefing", "Briefing matinal (mails triés, agenda, météo)"),
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
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("quota", cmd_usage))
    app.add_handler(CommandHandler("skills", cmd_skills))

    # Handler pour les boutons interactifs (clics)
    app.add_handler(CallbackQueryHandler(on_callback_query))

    # Handler messages texte normaux
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Handler images et photos
    app.add_handler(MessageHandler(filters.PHOTO | (filters.Document.IMAGE), handle_photo))

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
