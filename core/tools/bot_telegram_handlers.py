"""
core/tools/bot_telegram_handlers.py — Handlers de commandes et callbacks Telegram pour les modules.

Intègre tous les outils modulaires (Moodle, Budget, Courses, Obsidian, Focus,
Idées, Projets, Contrôle PC, Radar, Départs, etc.) dans le bot Telegram.
"""

import base64
import html
import io
import logging
from pathlib import Path
from typing import Any, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from core.hardware_relay import HARDWARE_RELAY
from core.tools.base import ToolRegistry
from core.tools.dispatcher import execute_tool_action
from core.tools.nlp_router import classify_natural_input

logger = logging.getLogger("core.bot_telegram")


async def try_nlp_route(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str) -> bool:
    """Tente de classifier et d'exécuter localement l'intention en 0 token (<5ms).

    Returns:
        bool: True si une intention native a été traitée et répondue, False sinon (transmettre à l'IA).
    """
    try:
        classification = classify_natural_input(prompt)
    except Exception as exc:
        logger.debug(f"Erreur classification NLP: {exc}")
        return False

    intent = classification.get("intent")
    if not intent:
        return False

    logger.info(f"NLP Router : intention détectée '{intent}' pour '{prompt[:60]}'")

    # 1. Shopping list (Courses)
    if intent == "courses_add":
        res = await execute_tool_action("shopping", "add", {"text": classification.get("items", "")}, platform="telegram")
        if res.get("success"):
            await update.message.reply_text(res["message"], parse_mode=ParseMode.HTML)
            return True
    elif intent == "courses_list":
        res = await execute_tool_action("shopping", "list", platform="telegram")
        if res.get("success"):
            await update.message.reply_text(res["message"], parse_mode=ParseMode.HTML)
            return True
    elif intent == "courses_clear":
        res = await execute_tool_action("shopping", "clear_done", platform="telegram")
        if res.get("success"):
            await update.message.reply_text(res["message"], parse_mode=ParseMode.HTML)
            return True

    # 2. Dépenses & Budget
    elif intent == "expense_record":
        res = await execute_tool_action(
            "expenses",
            "add",
            {
                "amount": classification.get("amount", 0.0),
                "description": classification.get("merchant", "Dépense"),
                "category": classification.get("category"),
            },
            platform="telegram",
        )
        if res.get("success"):
            await update.message.reply_text(res["message"], parse_mode=ParseMode.HTML)
            return True
    elif intent == "expenses_summary":
        res = await execute_tool_action("expenses", "summary", platform="telegram")
        if res.get("success"):
            await update.message.reply_text(res["message"], parse_mode=ParseMode.HTML)
            return True
    elif intent in ("budget_status", "budget_proj"):
        res = await execute_tool_action("expenses", "budget", {"subaction": "status"}, platform="telegram")
        if res.get("success"):
            await update.message.reply_text(res["message"], parse_mode=ParseMode.HTML)
            return True

    # 3. Obsidian Vault
    elif intent == "obsidian_search":
        res = await execute_tool_action("obsidian", "search", {"query": classification.get("query", "")}, platform="telegram")
        if res.get("success"):
            await update.message.reply_text(res["message"], parse_mode=ParseMode.HTML)
            return True
    elif intent == "obsidian_note":
        res = await execute_tool_action("obsidian", "note", {"text": classification.get("text", "")}, platform="telegram")
        if res.get("success"):
            await update.message.reply_text(res["message"], parse_mode=ParseMode.HTML)
            return True
    elif intent == "obsidian_overview":
        res = await execute_tool_action("obsidian", "overview", platform="telegram")
        if res.get("success"):
            await update.message.reply_text(res["message"], parse_mode=ParseMode.HTML)
            return True

    # 4. Focus & Pomodoro
    elif intent == "focus_control":
        sub = classification.get("text", "stop")
        if sub == "stop":
            await update.message.reply_text("🛑 Session Focus terminée.", parse_mode=ParseMode.HTML)
            return True
        res = await execute_tool_action("focus", "start", {"raw_text": sub}, platform="telegram")
        if res.get("success"):
            await update.message.reply_text(res["message"], parse_mode=ParseMode.HTML)
            return True
    elif intent in ("focus_stats", "productivity_stats"):
        res = await execute_tool_action("focus", "stats", platform="telegram")
        if res.get("success"):
            await update.message.reply_text(res["message"], parse_mode=ParseMode.HTML)
            return True

    # 5. Idées & Projets
    elif intent == "ideas_list":
        res = await execute_tool_action("ideas", "list", platform="telegram")
        if res.get("success"):
            await update.message.reply_text(res["message"], parse_mode=ParseMode.HTML)
            return True
    elif intent == "idea_incubate":
        wait_msg = await update.message.reply_text("💡 Incubation de l'idée en cours...")
        res = await execute_tool_action("ideas", "incubate", {"text": classification.get("text", "")}, platform="telegram")
        await wait_msg.edit_text(res.get("message", "Idée incubée."), parse_mode=ParseMode.HTML)
        return True
    elif intent == "projects_overview":
        res = await execute_tool_action("projects", "list", platform="telegram")
        if res.get("success"):
            await update.message.reply_text(res["message"], parse_mode=ParseMode.HTML)
            return True
    elif intent == "project_cockpit":
        res = await execute_tool_action("projects", "cockpit", {"name": classification.get("project", "")}, platform="telegram")
        if res.get("success"):
            await update.message.reply_text(res["message"], parse_mode=ParseMode.HTML)
            return True

    # 6. Tech & Cyber Radar + Universal Inbox
    elif intent in ("tech_radar", "radar"):
        wait_msg = await update.message.reply_text("🔍 Récupération des tendances tech & cyber...")
        res = await execute_tool_action("inbox", "radar", {"topic": classification.get("topic", "cyber")}, platform="telegram")
        await wait_msg.edit_text(res.get("message", "Actualités récupérées."), parse_mode=ParseMode.HTML)
        return True
    elif intent == "inbox_link":
        wait_msg = await update.message.reply_text("📥 Capture et analyse du lien...")
        res = await execute_tool_action(
            "inbox",
            "capture",
            {"url": classification.get("url", ""), "note": classification.get("note", "")},
            platform="telegram",
        )
        await wait_msg.edit_text(res.get("message", "Lien capturé."), parse_mode=ParseMode.HTML)
        return True

    # 7. Contrôle PC & Matériel
    elif intent in ("pc_screenshot", "system_screenshot"):
        wait_msg = await update.message.reply_text("📸 Prise de capture d'écran...")
        res = await execute_tool_action("system_pc", "screenshot", platform="telegram")
        if res.get("success"):
            data = res.get("data", {})
            img_b64 = data.get("image_base64")
            file_path = data.get("file_path")
            if img_b64:
                img_bytes = base64.b64decode(img_b64)
                await update.message.reply_photo(photo=io.BytesIO(img_bytes), caption="📸 Capture d'écran PC")
                await wait_msg.delete()
                return True
            elif file_path and Path(file_path).exists():
                try:
                    with open(file_path, "rb") as fp:
                        await update.message.reply_photo(photo=fp, caption="📸 Capture d'écran PC")
                finally:
                    if "nc_screenshot_" in str(file_path):
                        Path(file_path).unlink(missing_ok=True)
                await wait_msg.delete()
                return True
        await wait_msg.edit_text(res.get("message", "Échec de la capture."), parse_mode=ParseMode.HTML)
        return True

    elif intent in ("pc_cam", "system_cam"):
        wait_msg = await update.message.reply_text("📷 Capture webcam discrète...")
        res = await execute_tool_action("system_pc", "cam", platform="telegram")
        if res.get("success"):
            data = res.get("data", {})
            img_b64 = data.get("image_base64")
            file_path = data.get("file_path")
            if img_b64:
                img_bytes = base64.b64decode(img_b64)
                await update.message.reply_photo(photo=io.BytesIO(img_bytes), caption="📷 Capture Webcam")
                await wait_msg.delete()
                return True
            elif file_path and Path(file_path).exists():
                try:
                    with open(file_path, "rb") as fp:
                        await update.message.reply_photo(photo=fp, caption="📷 Capture Webcam")
                finally:
                    if "nc_cam_" in str(file_path):
                        Path(file_path).unlink(missing_ok=True)
                await wait_msg.delete()
                return True
        await wait_msg.edit_text(res.get("message", "Échec webcam."), parse_mode=ParseMode.HTML)
        return True

    elif intent in ("pc_lock", "system_lock"):
        res = await execute_tool_action("system_pc", "lock", platform="telegram")
        await update.message.reply_text(res.get("message", "Session verrouillée."), parse_mode=ParseMode.HTML)
        return True

    elif intent in ("pc_status", "system_pc"):
        res = await execute_tool_action("system_pc", "hardware_health", platform="telegram")
        await update.message.reply_text(res.get("message", "Statut PC récupéré."), parse_mode=ParseMode.HTML)
        return True

    elif intent in ("git_summary", "system_git"):
        wait_msg = await update.message.reply_text("🔍 Scan des dépôts Git...")
        res = await execute_tool_action("system_pc", "git_scan", platform="telegram")
        await wait_msg.edit_text(res.get("message", "Scan terminé."), parse_mode=ParseMode.HTML)
        return True

    # 8. Conseiller Départs
    elif intent in ("travel_depart", "depart"):
        res = await execute_tool_action(
            "travel",
            "plan",
            {
                "text": classification.get("text"),
                "destination": classification.get("destination", "Paris"),
                "time": classification.get("time", "08:00"),
            },
            platform="telegram",
        )
        if res.get("success"):
            await update.message.reply_text(res["message"], parse_mode=ParseMode.HTML)
            return True

    return False


# ── HANDLERS DES COMMANDES TELEGRAM ──────────────────────────────────────────

async def cmd_devoirs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /devoirs : affiche les devoirs et TPs Moodle EPITA."""
    res = await execute_tool_action("moodle", "devoirs", platform="telegram")
    await update.message.reply_text(res.get("message", "Aucun devoir trouvé."), parse_mode=ParseMode.HTML)


async def cmd_depense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /depense <montant> [description] : enregistre une dépense."""
    args = context.args or []
    if not args:
        await update.message.reply_text("💡 Utilisation : <code>/depense 12.50 repas du midi</code>", parse_mode=ParseMode.HTML)
        return

    try:
        amt = float(args[0].replace("€", "").replace(",", "."))
        desc = " ".join(args[1:]) if len(args) > 1 else "Dépense"
        res = await execute_tool_action("expenses", "add", {"amount": amt, "description": desc}, platform="telegram")
        await update.message.reply_text(res.get("message", "Dépense enregistrée."), parse_mode=ParseMode.HTML)
    except ValueError:
        await update.message.reply_text("⚠️ Montant invalide. Exemple : <code>/depense 8.50</code>", parse_mode=ParseMode.HTML)


async def cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /budget [montant] : affiche ou règle le budget mensuel."""
    args = context.args or []
    if args and args[0].replace(".", "", 1).isdigit():
        cap = float(args[0])
        res = await execute_tool_action("expenses", "budget", {"subaction": "set", "cap": cap}, platform="telegram")
        await update.message.reply_text(res.get("message", "Budget mis à jour."), parse_mode=ParseMode.HTML)
    else:
        res = await execute_tool_action("expenses", "budget", {"subaction": "status"}, platform="telegram")
        await update.message.reply_text(res.get("message", "Budget indisponible."), parse_mode=ParseMode.HTML)


async def cmd_courses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /courses [articles] : affiche ou ajoute des articles à la liste de courses."""
    args = context.args or []
    if args:
        raw_text = " ".join(args)
        if raw_text.strip().lower() in ("clear", "clean", "done"):
            res = await execute_tool_action("shopping", "clear_done", platform="telegram")
        elif raw_text.strip().lower() == "all":
            res = await execute_tool_action("shopping", "clear_all", platform="telegram")
        else:
            res = await execute_tool_action("shopping", "add", {"text": raw_text}, platform="telegram")
    else:
        res = await execute_tool_action("shopping", "list", platform="telegram")

    await update.message.reply_text(res.get("message", "Liste de courses."), parse_mode=ParseMode.HTML)


async def cmd_kb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /kb <requête> : recherche dans le coffre Obsidian."""
    query = " ".join(context.args or [])
    if not query:
        await update.message.reply_text("💡 Utilisation : <code>/kb linux privilege escalation</code>", parse_mode=ParseMode.HTML)
        return

    res = await execute_tool_action("obsidian", "search", {"query": query}, platform="telegram")
    await update.message.reply_text(res.get("message", "Aucun résultat."), parse_mode=ParseMode.HTML)


async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /note <texte> : ajoute une note dans Obsidian."""
    text = " ".join(context.args or [])
    if not text:
        await update.message.reply_text("💡 Utilisation : <code>/note Penser à réviser la cryptographie RSA</code>", parse_mode=ParseMode.HTML)
        return

    res = await execute_tool_action("obsidian", "note", {"text": text}, platform="telegram")
    await update.message.reply_text(res.get("message", "Note enregistrée."), parse_mode=ParseMode.HTML)


async def cmd_focus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /focus [durée] [sujet] : lance une session Pomodoro."""
    args = context.args or []
    duration = 25
    topic = "Deep Work"
    if args and args[0].replace("m", "").isdigit():
        duration = int(args[0].replace("m", ""))
        if len(args) > 1:
            topic = " ".join(args[1:])
    elif args:
        topic = " ".join(args)

    res = await execute_tool_action("focus", "start", {"duration": duration, "topic": topic}, platform="telegram")
    await update.message.reply_text(res.get("message", "Focus démarré."), parse_mode=ParseMode.HTML)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /stats : statistiques de productivité et de travail profond."""
    res = await execute_tool_action("focus", "stats", platform="telegram")
    await update.message.reply_text(res.get("message", "Stats indisponibles."), parse_mode=ParseMode.HTML)


async def cmd_idea(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /idea <description> : incube une idée de projet."""
    text = " ".join(context.args or [])
    if not text:
        await update.message.reply_text("💡 Utilisation : <code>/idea Scanner de vulnérabilités rapide en Rust</code>", parse_mode=ParseMode.HTML)
        return

    wait_msg = await update.message.reply_text("💡 Incubation de l'idée en cours...")
    res = await execute_tool_action("ideas", "incubate", {"text": text}, platform="telegram")
    await wait_msg.edit_text(res.get("message", "Idée incubée."), parse_mode=ParseMode.HTML)


async def cmd_ideas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /ideas : liste le backlog des idées incubées."""
    res = await execute_tool_action("ideas", "list", platform="telegram")
    await update.message.reply_text(res.get("message", "Aucune idée enregistrée."), parse_mode=ParseMode.HTML)


async def cmd_radar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /radar [sujet] : veille technologique et dépôts GitHub tendances."""
    topic = " ".join(context.args or []) or "cyber"
    wait_msg = await update.message.reply_text(f"🔍 Veille technologique pour : <i>{html.escape(topic)}</i>...")
    res = await execute_tool_action("inbox", "radar", {"topic": topic}, platform="telegram")
    await wait_msg.edit_text(res.get("message", "Veille terminée."), parse_mode=ParseMode.HTML)


async def cmd_pc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /pc : diagnostic matériel en direct (batterie, RAM, CPU)."""
    res = await execute_tool_action("system_pc", "hardware_health", platform="telegram")
    await update.message.reply_text(res.get("message", "Diagnostic PC."), parse_mode=ParseMode.HTML)


async def cmd_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /screenshot : capture d'écran Wayland via grim."""
    wait_msg = await update.message.reply_text("📸 Prise de capture d'écran...")
    res = await execute_tool_action("system_pc", "screenshot", platform="telegram")
    if res.get("success"):
        data = res.get("data", {})
        img_b64 = data.get("image_base64")
        file_path = data.get("file_path")
        try:
            if img_b64:
                img_bytes = base64.b64decode(img_b64)
                await update.message.reply_photo(photo=io.BytesIO(img_bytes), caption="📸 Capture d'écran PC")
                await wait_msg.delete()
                return
            elif file_path and Path(file_path).exists():
                with open(file_path, "rb") as fp:
                    await update.message.reply_photo(photo=fp, caption="📸 Capture d'écran PC")
                await wait_msg.delete()
                return
        finally:
            if file_path and "nc_screenshot_" in str(file_path):
                Path(file_path).unlink(missing_ok=True)

    await wait_msg.edit_text(res.get("message", "Échec de la capture."), parse_mode=ParseMode.HTML)


async def cmd_cam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /cam : capture webcam discrète."""
    wait_msg = await update.message.reply_text("📷 Capture webcam discrète...")
    res = await execute_tool_action("system_pc", "cam", platform="telegram")
    if res.get("success"):
        data = res.get("data", {})
        img_b64 = data.get("image_base64")
        file_path = data.get("file_path")
        try:
            if img_b64:
                img_bytes = base64.b64decode(img_b64)
                await update.message.reply_photo(photo=io.BytesIO(img_bytes), caption="📷 Capture Webcam")
                await wait_msg.delete()
                return
            elif file_path and Path(file_path).exists():
                with open(file_path, "rb") as fp:
                    await update.message.reply_photo(photo=fp, caption="📷 Capture Webcam")
                await wait_msg.delete()
                return
        finally:
            if file_path and "nc_cam_" in str(file_path):
                Path(file_path).unlink(missing_ok=True)

    await wait_msg.edit_text(res.get("message", "Échec webcam."), parse_mode=ParseMode.HTML)


async def cmd_lock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /lock : verrouille la session graphique."""
    res = await execute_tool_action("system_pc", "lock", platform="telegram")
    await update.message.reply_text(res.get("message", "Session verrouillée."), parse_mode=ParseMode.HTML)


async def cmd_say(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /say <texte> : synthèse vocale sur les enceintes du PC."""
    text = " ".join(context.args or [])
    if not text:
        await update.message.reply_text("💡 Utilisation : <code>/say Bonjour, le repas est prêt !</code>", parse_mode=ParseMode.HTML)
        return
    res = await execute_tool_action("system_pc", "say", {"text": text}, platform="telegram")
    await update.message.reply_text(res.get("message", "Texte diffusé."), parse_mode=ParseMode.HTML)


async def cmd_copy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /copy <texte> : copie dans le presse-papier du PC."""
    text = " ".join(context.args or [])
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""
    if not text:
        await update.message.reply_text("💡 Utilisation : <code>/copy mon texte à copier</code>", parse_mode=ParseMode.HTML)
        return
    res = await execute_tool_action("system_pc", "clipboard_set", {"text": text}, platform="telegram")
    await update.message.reply_text(res.get("message", "Copié dans le presse-papier."), parse_mode=ParseMode.HTML)


async def cmd_paste(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /paste : lit le contenu du presse-papier du PC."""
    res = await execute_tool_action("system_pc", "clipboard_get", platform="telegram")
    content = res.get("message", "")
    await update.message.reply_text(f"📋 <b>Presse-papier PC :</b>\n\n<code>{html.escape(content)}</code>", parse_mode=ParseMode.HTML)


async def cmd_git(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /git : scanne l'état des dépôts Git sur la machine."""
    wait_msg = await update.message.reply_text("🔍 Scan des dépôts Git...")
    res = await execute_tool_action("system_pc", "git_scan", platform="telegram")
    await wait_msg.edit_text(res.get("message", "Scan terminé."), parse_mode=ParseMode.HTML)


async def cmd_depart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /depart <destination> à <heure> : calcul de départ avec marge."""
    args_str = " ".join(context.args or [])
    dest = "Paris"
    target_time = "08:00"
    if " à " in args_str:
        parts = args_str.split(" à ", 1)
        dest = parts[0].strip()
        target_time = parts[1].strip()
    elif args_str:
        dest = args_str.strip()

    res = await execute_tool_action("travel", "plan", {"destination": dest, "time": target_time}, platform="telegram")
    await update.message.reply_text(res.get("message", "Plan de départ."), parse_mode=ParseMode.HTML)


def register_modular_telegram_commands(app: Any) -> None:
    """Enregistre l'ensemble des commandes modulaires auprès de l'application Telegram."""
    from telegram.ext import CommandHandler

    handlers = [
        ("devoirs", cmd_devoirs),
        ("depense", cmd_depense),
        ("depenses", cmd_depense),
        ("budget", cmd_budget),
        ("courses", cmd_courses),
        ("kb", cmd_kb),
        ("note", cmd_note),
        ("focus", cmd_focus),
        ("stats", cmd_stats),
        ("idea", cmd_idea),
        ("ideas", cmd_ideas),
        ("radar", cmd_radar),
        ("pc", cmd_pc),
        ("screenshot", cmd_screenshot),
        ("capture", cmd_screenshot),
        ("cam", cmd_cam),
        ("lock", cmd_lock),
        ("say", cmd_say),
        ("copy", cmd_copy),
        ("paste", cmd_paste),
        ("git", cmd_git),
        ("depart", cmd_depart),
    ]

    for cmd, fn in handlers:
        app.add_handler(CommandHandler(cmd, fn))
    logger.info(f"{len(handlers)} commandes modulaires enregistrées dans le bot Telegram.")
