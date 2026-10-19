"""
core/tools/bot_discord_handlers.py — Commandes Slash Discord pour les modules de l'assistant.

Fournit l'intégration Discord des outils (Moodle, Budget, Courses, Obsidian,
Focus, Idées, Diagnostic PC, Captures d'écran, Radar, Départs).
"""

import base64
import io
import logging
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands

from core.tools.dispatcher import execute_tool_action

logger = logging.getLogger("core.bot_discord")


def _clean_html_for_discord(text: str) -> str:
    """Nettoie ou convertit le HTML Telegram en Markdown Discord."""
    import re
    res = text.replace("<b>", "**").replace("</b>", "**")
    res = res.replace("<i>", "*").replace("</i>", "*")
    res = res.replace("<code>", "`").replace("</code>", "`")
    res = res.replace("<pre>", "```\n").replace("</pre>", "```")
    res = res.replace("<s>", "~~").replace("</s>", "~~")
    res = re.sub(r'<a\s+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', r"[\2](\1)", res)
    res = re.sub(r"<[^>]+>", "", res)
    return res


# ── COMMANDES SLASH ──────────────────────────────────────────────────────────

@app_commands.command(name="devoirs", description="Consulter les devoirs et TPs Moodle EPITA")
async def devoirs_slash(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    res = await execute_tool_action("moodle", "devoirs", platform="discord")
    msg = _clean_html_for_discord(res.get("message", "Aucun devoir trouvé."))
    await interaction.followup.send(msg[:2000])


@app_commands.command(name="depense", description="Enregistrer une dépense et vérifier le budget")
@app_commands.describe(montant="Montant en euros (€)", description="Description ou commerçant")
async def depense_slash(interaction: discord.Interaction, montant: float, description: str = "Dépense") -> None:
    await interaction.response.defer()
    res = await execute_tool_action("expenses", "add", {"amount": montant, "description": description}, platform="discord")
    msg = _clean_html_for_discord(res.get("message", "Dépense enregistrée."))
    await interaction.followup.send(msg[:2000])


@app_commands.command(name="budget", description="Consulter ou régler le budget mensuel")
@app_commands.describe(montant="Plafond mensuel en euros (optionnel)")
async def budget_slash(interaction: discord.Interaction, montant: Optional[float] = None) -> None:
    await interaction.response.defer()
    if montant is not None and montant > 0:
        res = await execute_tool_action("expenses", "budget", {"subaction": "set", "cap": montant}, platform="discord")
    else:
        res = await execute_tool_action("expenses", "budget", {"subaction": "status"}, platform="discord")
    msg = _clean_html_for_discord(res.get("message", "Budget indisponible."))
    await interaction.followup.send(msg[:2000])


@app_commands.command(name="courses", description="Gérer la liste de courses interactive")
@app_commands.describe(articles="Articles à ajouter (séparés par virgules), ou 'clear' pour vider")
async def courses_slash(interaction: discord.Interaction, articles: Optional[str] = None) -> None:
    await interaction.response.defer()
    if articles:
        if articles.strip().lower() in ("clear", "done", "clean"):
            res = await execute_tool_action("shopping", "clear_done", platform="discord")
        elif articles.strip().lower() == "all":
            res = await execute_tool_action("shopping", "clear_all", platform="discord")
        else:
            res = await execute_tool_action("shopping", "add", {"text": articles}, platform="discord")
    else:
        res = await execute_tool_action("shopping", "list", platform="discord")

    msg = _clean_html_for_discord(res.get("message", "Liste de courses."))
    await interaction.followup.send(msg[:2000])


@app_commands.command(name="kb", description="Rechercher dans le coffre Obsidian")
@app_commands.describe(requete="Termes de recherche ou question")
async def kb_slash(interaction: discord.Interaction, requete: str) -> None:
    await interaction.response.defer()
    res = await execute_tool_action("obsidian", "search", {"query": requete}, platform="discord")
    msg = _clean_html_for_discord(res.get("message", "Aucun résultat trouvé."))
    await interaction.followup.send(msg[:2000])


@app_commands.command(name="note", description="Ajouter une note dans le coffre Obsidian")
@app_commands.describe(texte="Contenu de la note à sauvegarder")
async def note_slash(interaction: discord.Interaction, texte: str) -> None:
    await interaction.response.defer()
    res = await execute_tool_action("obsidian", "note", {"text": texte}, platform="discord")
    msg = _clean_html_for_discord(res.get("message", "Note enregistrée."))
    await interaction.followup.send(msg[:2000])


@app_commands.command(name="focus", description="Lancer une session Pomodoro / Deep Work")
@app_commands.describe(duree="Durée en minutes", sujet="Sujet de la session")
async def focus_slash(interaction: discord.Interaction, duree: int = 25, sujet: str = "Deep Work") -> None:
    await interaction.response.defer()
    res = await execute_tool_action("focus", "start", {"duration": duree, "topic": sujet}, platform="discord")
    msg = _clean_html_for_discord(res.get("message", "Session démarrée."))
    await interaction.followup.send(msg[:2000])


@app_commands.command(name="stats", description="Statistiques de productivité Deep Work")
async def stats_slash(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    res = await execute_tool_action("focus", "stats", platform="discord")
    msg = _clean_html_for_discord(res.get("message", "Stats indisponibles."))
    await interaction.followup.send(msg[:2000])


@app_commands.command(name="idea", description="Incuber une nouvelle idée de projet")
@app_commands.describe(description="Description de votre idée")
async def idea_slash(interaction: discord.Interaction, description: str) -> None:
    await interaction.response.defer()
    res = await execute_tool_action("ideas", "incubate", {"text": description}, platform="discord")
    msg = _clean_html_for_discord(res.get("message", "Idée incubée."))
    await interaction.followup.send(msg[:2000])


@app_commands.command(name="ideas", description="Lister le backlog des idées de projets")
async def ideas_slash(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    res = await execute_tool_action("ideas", "list", platform="discord")
    msg = _clean_html_for_discord(res.get("message", "Aucune idée enregistrée."))
    await interaction.followup.send(msg[:2000])


@app_commands.command(name="radar", description="Veille technologique et dépôts GitHub tendances")
@app_commands.describe(sujet="Sujet (cyber, ai, rust, python, etc.)")
async def radar_slash(interaction: discord.Interaction, sujet: str = "cyber") -> None:
    await interaction.response.defer()
    res = await execute_tool_action("inbox", "radar", {"topic": sujet}, platform="discord")
    msg = _clean_html_for_discord(res.get("message", "Actualités tech."))
    await interaction.followup.send(msg[:2000])


@app_commands.command(name="pc", description="Diagnostic matériel PC (CPU, RAM, Batterie, Disque)")
async def pc_slash(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    res = await execute_tool_action("system_pc", "hardware_health", platform="discord")
    msg = _clean_html_for_discord(res.get("message", "Diagnostic PC."))
    await interaction.followup.send(msg[:2000])


@app_commands.command(name="screenshot", description="Prendre une capture d'écran du PC à distance")
async def screenshot_slash(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    res = await execute_tool_action("system_pc", "screenshot", platform="discord")
    if res.get("success"):
        data = res.get("data", {})
        img_b64 = data.get("image_base64")
        file_path = data.get("file_path")
        if img_b64:
            img_bytes = base64.b64decode(img_b64)
            file = discord.File(io.BytesIO(img_bytes), filename="screenshot.png")
            await interaction.followup.send("📸 **Capture d'écran PC :**", file=file)
            return
        elif file_path and Path(file_path).exists():
            file = discord.File(file_path, filename="screenshot.png")
            await interaction.followup.send("📸 **Capture d'écran PC :**", file=file)
            return

    msg = _clean_html_for_discord(res.get("message", "Échec de la capture d'écran."))
    await interaction.followup.send(msg[:2000])


@app_commands.command(name="cam", description="Prendre une photo webcam discrète")
async def cam_slash(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    res = await execute_tool_action("system_pc", "cam", platform="discord")
    if res.get("success"):
        data = res.get("data", {})
        img_b64 = data.get("image_base64")
        file_path = data.get("file_path")
        if img_b64:
            img_bytes = base64.b64decode(img_b64)
            file = discord.File(io.BytesIO(img_bytes), filename="webcam.jpg")
            await interaction.followup.send("📷 **Capture Webcam :**", file=file)
            return
        elif file_path and Path(file_path).exists():
            file = discord.File(file_path, filename="webcam.jpg")
            await interaction.followup.send("📷 **Capture Webcam :**", file=file)
            return

    msg = _clean_html_for_discord(res.get("message", "Échec de la webcam."))
    await interaction.followup.send(msg[:2000])


@app_commands.command(name="lock", description="Verrouiller la session graphique du PC")
async def lock_slash(interaction: discord.Interaction) -> None:
    await interaction.response.defer()
    res = await execute_tool_action("system_pc", "lock", platform="discord")
    msg = _clean_html_for_discord(res.get("message", "Session verrouillée."))
    await interaction.followup.send(msg[:2000])


@app_commands.command(name="depart", description="Calculer l'heure de départ conseillée avec marge")
@app_commands.describe(destination="Ville ou adresse", heure_arrivee="Heure d'arrivée souhaitée (ex: 08:30)")
async def depart_slash(interaction: discord.Interaction, destination: str, heure_arrivee: str = "08:30") -> None:
    await interaction.response.defer()
    res = await execute_tool_action("travel", "plan", {"destination": destination, "time": heure_arrivee}, platform="discord")
    msg = _clean_html_for_discord(res.get("message", "Plan de départ."))
    await interaction.followup.send(msg[:2000])


def register_modular_discord_commands(tree: app_commands.CommandTree) -> None:
    """Ajoute les commandes slash modulaires au CommandTree de Discord."""
    commands = [
        devoirs_slash,
        depense_slash,
        budget_slash,
        courses_slash,
        kb_slash,
        note_slash,
        focus_slash,
        stats_slash,
        idea_slash,
        ideas_slash,
        radar_slash,
        pc_slash,
        screenshot_slash,
        cam_slash,
        lock_slash,
        depart_slash,
    ]

    for cmd in commands:
        tree.add_command(cmd)

    logger.info(f"{len(commands)} commandes slash modulaires enregistrées dans Discord.")
