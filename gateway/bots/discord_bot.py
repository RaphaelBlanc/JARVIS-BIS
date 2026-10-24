"""
bots/discord_bot.py — Bot Discord avec commandes slash et préfixées.

Lance un bot Discord en arrière-plan dans un thread daemon,
qui transmet les questions au home-runner via `gateway.forward_to_runner()`.
"""

import asyncio
import base64
import io
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from formatters import (
    extract_and_render_latex,
    extract_downloadable_code,
    latex_to_unicode,
    smart_split_discord,
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

# ── Configuration ─────────────────────────────────────────────────────────────

DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
_RAW_GUILD_ID: str = os.getenv("DISCORD_GUILD_ID", "").strip()
DISCORD_GUILD_ID: Optional[int] = int(_RAW_GUILD_ID) if _RAW_GUILD_ID else None

_RAW_ALLOWED_IDS: str = os.getenv("DISCORD_ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS: set[int] = (
    {int(uid.strip()) for uid in _RAW_ALLOWED_IDS.split(",") if uid.strip()}
    if _RAW_ALLOWED_IDS
    else set()
)
ALLOW_ALL_USERS: bool = os.getenv("ALLOW_ALL_USERS", "").strip().lower() in {
    "1", "true", "yes", "on"
}

# Boucle asyncio du bot Discord (thread séparé). Utilisée par le planificateur
# de briefing pour envoyer des messages sans conflit de boucle.
_DISCORD_LOOP: Optional[asyncio.AbstractEventLoop] = None


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
        "DISCORD_ALLOWED_USER_IDS est vide et ALLOW_ALL_USERS n'est pas activé : "
        "toutes les commandes Discord seront refusées. Renseignez votre ID "
        "utilisateur ou définissez ALLOW_ALL_USERS=1 pour ouvrir l'accès."
    )


# ── Actions serveur whitelistées ─────────────────────────────────────────────

SERVER_ACTIONS: tuple[str, ...] = (
    "status",
    "restart-runner",
    "restart-gateway",
    "tunnel-status",
    "logs-gateway",
    "logs-runner",
)
SERVER_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "server-control.sh"
SERVER_CONTROL_INSTALLED = Path("/usr/local/sbin/assistant-server-control")
SERVER_PRIVILEGED_ACTIONS = frozenset(
    {"restart-runner", "restart-gateway", "logs-gateway", "logs-runner"}
)
SERVER_ACTION_CHOICES = [
    app_commands.Choice(name="État des services", value="status"),
    app_commands.Choice(name="Redémarrer le runner", value="restart-runner"),
    app_commands.Choice(name="Redémarrer le gateway (différé)", value="restart-gateway"),
    app_commands.Choice(name="État du tunnel", value="tunnel-status"),
    app_commands.Choice(name="40 dernières lignes du gateway", value="logs-gateway"),
    app_commands.Choice(name="40 dernières lignes du runner", value="logs-runner"),
]


async def _run_server_action(action: str) -> tuple[bool, str]:
    """Exécute une action whitelistée sans passer par agy ni par un shell."""
    if action not in SERVER_ACTIONS:
        return False, "Action inconnue."
    if not SERVER_SCRIPT.is_file():
        return False, f"Script introuvable : {SERVER_SCRIPT}"

    # Si le script privilégié est installé en root, on l'appelle via sudo -n.
    # Sinon, le script source utilise lui-même sudo -n pour les commandes
    # concernées (et affiche une erreur claire si sudo n'est pas configuré).
    if action in SERVER_PRIVILEGED_ACTIONS and SERVER_CONTROL_INSTALLED.is_file():
        if os.geteuid() == 0:
            commande = [str(SERVER_CONTROL_INSTALLED), action]
        else:
            commande = ["sudo", "-n", str(SERVER_CONTROL_INSTALLED), action]
    else:
        commande = ["bash", str(SERVER_SCRIPT), action]

    try:
        proc = await asyncio.create_subprocess_exec(
            *commande,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=40)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return False, "Délai dépassé (40 s)."
    except OSError as exc:
        return False, f"Impossible d'exécuter le script : {exc}"

    morceaux = [
        bloc.decode(errors="replace").strip()
        for bloc in (stdout, stderr)
        if bloc and bloc.strip()
    ]
    texte = "\n".join(morceaux).strip() or "(aucune sortie)"
    if len(texte) > 1800:
        texte = texte[:1800] + "\n…"
    return proc.returncode == 0, texte


def _server_reply(action: str, success: bool) -> str:
    symbole = "✅" if success else "❌"
    return f"{symbole} **Action serveur : `{action}`**"


SAVED_DISCORD_CHANNELS_FILE = (
    Path(__file__).resolve().parent.parent / "saved_discord_channels.json"
)


def _save_discord_channel_id(channel_id: int) -> None:
    """Enregistre le salon Discord pour les diffusions automatiques."""
    try:
        channels = set()
        if SAVED_DISCORD_CHANNELS_FILE.exists():
            with open(SAVED_DISCORD_CHANNELS_FILE, "r", encoding="utf-8") as f:
                channels = set(json.load(f))
        channels.add(channel_id)
        with open(SAVED_DISCORD_CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(channels), f)
    except Exception as exc:
        logger.warning("Erreur sauvegarde salon Discord %s: %s", channel_id, exc)


def get_saved_discord_channel_ids() -> list[int]:
    """Retourne les salons Discord enregistrés pour les diffusions."""
    if not SAVED_DISCORD_CHANNELS_FILE.exists():
        return []
    try:
        with open(SAVED_DISCORD_CHANNELS_FILE, "r", encoding="utf-8") as f:
            return [int(cid) for cid in json.load(f)]
    except Exception:
        return []


async def _transcribe_audio_attachment(
    attachment: discord.Attachment,
) -> Optional[str]:
    """Télécharge une pièce jointe audio Discord et la transcrit en texte."""
    size = int(getattr(attachment, "size", 0) or 0)
    if size > MAX_AUDIO_BYTES:
        logger.warning(
            "Pièce jointe audio Discord trop volumineuse : %.1f Mo.",
            size / 1024 / 1024,
        )
        return None
    suffix = Path(attachment.filename or "voice.ogg").suffix or ".ogg"
    raw_bytes = await attachment.read()

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(raw_bytes)
        temp_path = Path(tmp.name)

    try:
        text, engine = await transcribe_audio(temp_path)
        if text:
            logger.info(
                "Message vocal Discord transcrit (%s) : '%s'", engine, text[:200]
            )
        return text
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


# ── Bot Discord ───────────────────────────────────────────────────────────────


class AssistantBot(commands.Bot):
    """Bot Discord avec commandes slash et préfixées pour l'assistant personnel."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # Nécessaire pour les commandes préfixées
        super().__init__(command_prefix="!", intents=intents)
        self._guild_obj: Optional[discord.Object] = (
            discord.Object(id=DISCORD_GUILD_ID) if DISCORD_GUILD_ID else None
        )
        # L'aide par défaut liste chaque alias comme une commande séparée, ce
        # qui donne une impression de doublons. On la remplace plus bas.
        self.remove_command("help")

    async def setup_hook(self) -> None:
        """Enregistre l'ensemble des commandes slash avec autocomplétion."""
        global _DISCORD_LOOP
        _DISCORD_LOOP = asyncio.get_running_loop()

        for cmd in [
            briefing_slash,
            reminder_slash,
            reminders_slash,
            snooze_slash,
            done_reminder_slash,
            cancel_reminder_slash,
            remember_slash,
            forget_slash,
            profile_slash,
            help_slash,
            ask_slash,
            resume_slash,
            delete_slash,
            agenda_slash,
            tasks_slash,
            meteo_slash,
            search_slash,
            summarize_slash,
            status_slash,
            server_slash,
            compact_slash,
            setmodel_slash,
            effort_slash,
            mode_slash,
            permissions_slash,
            new_slash,
            models_slash,
            session_slash,
            usage_slash,
            skills_slash,
        ]:
            self.tree.add_command(cmd)

        # Enregistre les commandes modulaires (Moodle, Budget, Courses, Obsidian, Focus, etc.)
        from core.tools.bot_discord_handlers import register_modular_discord_commands
        register_modular_discord_commands(self.tree)

        logger.info("Commandes slash enregistrées dans le command tree.")

    async def _remove_stale_global_commands(self) -> None:
        """Supprime les commandes globales qui doublonnent les commandes serveur.

        Les commandes de ce bot sont volontairement synchronisées au niveau du
        serveur (propagation immédiate). Une ancienne version avait laissé une
        commande globale ``/ask``, visible en plus de ``/ask`` serveur.
        """
        try:
            commands = await self.http.get_global_commands(self.application_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Impossible de lister les commandes globales : %s", exc)
            return

        for command in commands:
            name = command.get("name", "?")
            try:
                await self.http.delete_global_command(
                    self.application_id, int(command["id"])
                )
                logger.info("Commande globale obsolète supprimée : /%s", name)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Impossible de supprimer la commande globale /%s : %s",
                    name,
                    exc,
                )

    async def on_ready(self) -> None:
        logger.info("Bot Discord connecté en tant que %s (ID: %d).", self.user, self.user.id)
        await self._remove_stale_global_commands()
        if not self.guilds:
            invite_url = (
                f"https://discord.com/oauth2/authorize?client_id={self.user.id}"
                "&permissions=277025507392&scope=bot%20applications.commands"
            )
            logger.warning(
                "Le bot Discord n'a rejoint aucun serveur ! Invitez-le via ce lien : %s",
                invite_url,
            )
        else:
            for guild in self.guilds:
                logger.info("Serveur Discord: %s (ID: %d)", guild.name, guild.id)
                channels = [
                    c.name for c in guild.text_channels
                    if c.permissions_for(guild.me).send_messages
                ]
                logger.info("Salons accessibles dans '%s': %s", guild.name, ", ".join(f"#{c}" for c in channels))
                # Synchronisation immédiate des commandes slash sur le serveur !
                try:
                    self.tree.copy_global_to(guild=guild)
                    await self.tree.sync(guild=guild)
                    logger.info("Commandes slash synchronisées instantanément pour le serveur '%s' !", guild.name)
                except Exception as exc:
                    logger.warning("Erreur lors de la synchronisation slash pour '%s': %s", guild.name, exc)

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        """Gestion globale des erreurs de commandes préfixées."""
        if isinstance(error, commands.CommandNotFound):
            return
        logger.error("Erreur de commande Discord: %s", error, exc_info=True)
        await ctx.send(f"❌ Erreur inattendue : {error}")


# Instance globale du bot
_bot: Optional[AssistantBot] = None


def _get_bot() -> AssistantBot:
    """Retourne l'instance singleton du bot Discord."""
    global _bot
    if _bot is None:
        _bot = AssistantBot()
        _register_prefix_commands(_bot)
    return _bot


# ── Commandes slash avec autocomplétion & choix ───────────────────────────────


async def _fast_ack(interaction: discord.Interaction, message: str = "⏳ Traitement en cours...", ephemeral: bool = False) -> None:
    """Accuse réception immédiatement de l'interaction pour garantir aucune erreur 3s Discord."""
    try:
        await interaction.response.send_message(message, ephemeral=ephemeral)
    except Exception:
        try:
            await interaction.response.defer(ephemeral=ephemeral)
        except Exception:
            pass


async def send_discord_long_response(
    interaction: discord.Interaction,
    text: str,
    title: Optional[str] = None,
    extra_files: Optional[list[discord.File]] = None,
) -> None:
    """Envoie une réponse Discord optimisée avec support LaTeX et code copiable :
    - Rendu des équations LaTeX en images PNG attachées.
    - Export des gros blocs de code en fichiers attachés téléchargeables.
    - Découpage sécurisé préservant les blocs de code et leur coloration syntaxique.
    """
    if not text:
        text = "✅ Tâche terminée (pas de sortie)."

    # 1. Rendu LaTeX et extraction des gros extraits de code
    processed_text, rendered_images = await extract_and_render_latex(text, dark_mode=True)
    code_snippets = extract_downloadable_code(text, min_chars=700)

    # Préparation des pièces jointes Discord
    discord_files: list[discord.File] = []
    if extra_files:
        discord_files.extend(extra_files)

    for img in rendered_images:
        discord_files.append(
            discord.File(fp=io.BytesIO(img["bytes"]), filename=img["filename"])
        )

    for code in code_snippets:
        discord_files.append(
            discord.File(fp=io.BytesIO(code["bytes"]), filename=code["filename"])
        )

    # 2. Cas 1 : message court <= 2000 caractères
    if len(processed_text) <= 2000:
        try:
            await interaction.edit_original_response(
                content=processed_text,
                embeds=[],
                attachments=discord_files,
            )
            return
        except Exception:
            try:
                await interaction.followup.send(content=processed_text, files=discord_files)
                return
            except Exception as e:
                logger.warning("Erreur envoi message court Discord: %s", e)

    # 3. Cas 2 : message découpé intelligemment avec smart_split_discord (préserve les blocs ```lang)
    parts = smart_split_discord(processed_text, max_chars=1950)

    first = True
    for p in parts:
        if first:
            first = False
            try:
                await interaction.edit_original_response(
                    content=p,
                    embeds=[],
                    attachments=discord_files,
                )
                continue
            except Exception as exc:
                logger.warning("Erreur edit_original_response Discord: %s", exc)
        try:
            await interaction.followup.send(content=p)
        except Exception as e:
            logger.warning("Erreur followup Discord: %s", e)


@app_commands.command(name="briefing", description="Génère votre briefing matinal (e-mails triés, agenda, météo)")
async def briefing_slash(interaction: discord.Interaction) -> None:
    """Handler de la commande slash /briefing."""
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return

    logger.info("Commande /briefing de %d", interaction.user.id)
    await _fast_ack(interaction, "⏳ Préparation de votre briefing matinal (tri des e-mails, agenda, météo)...")
    result = await get_briefing_text(timeout=240)

    if result.get("success"):
        response_text = result.get("output", "").strip() or "✅ Briefing terminé."
    else:
        error = result.get("error", "Erreur inconnue")
        response_text = f"❌ Erreur : {error}"

    await send_discord_long_response(interaction, response_text, title="🌅 Votre Briefing Matinal")


@app_commands.command(name="rappel", description="Programmer un rappel ponctuel ou récurrent")
@app_commands.describe(
    delai="Durée ou heure : 15m, 1h30, 14h30, 2h",
    message="Ce que l'assistant doit vous rappeler",
    recurrence="Optionnel : chaque jour, chaque lundi, tous les 2 jours...",
)
async def reminder_slash(
    interaction: discord.Interaction,
    delai: str,
    message: str,
    recurrence: Optional[str] = None,
) -> None:
    """Handler de la commande slash /rappel."""
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return

    if not interaction.channel_id:
        await interaction.response.send_message(
            "❌ Impossible de déterminer le salon pour ce rappel.", ephemeral=True
        )
        return

    raw = " ".join(part for part in (recurrence, delai, message) if part).strip()
    remind_at, parsed_message, parsed_recurrence, error = parse_reminder_command(raw)
    if remind_at is None or not parsed_message:
        await interaction.response.send_message(
            f"❌ {error or 'Durée invalide.'}", ephemeral=True
        )
        return

    reminder = add_reminder(
        platform="discord",
        chat_id=interaction.channel_id,
        target_id=interaction.user.id,
        message=parsed_message,
        remind_at=remind_at,
        target_name=str(interaction.user),
        recurrence=parsed_recurrence,
    )
    logger.info(
        "Rappel #%s créé via /rappel (discord -> %s, récurrence=%s) : %s",
        reminder["id"],
        interaction.channel_id,
        parsed_recurrence,
        parsed_message,
    )
    await interaction.response.send_message(format_reminder_confirmation(reminder))


@app_commands.command(name="rappels", description="Afficher les rappels actifs de ce salon")
async def reminders_slash(interaction: discord.Interaction) -> None:
    """Handler de la commande slash /rappels."""
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return

    reminders = list_active_reminders(interaction.channel_id or 0)
    if not reminders:
        await interaction.response.send_message("⏰ Aucun rappel actif.")
        return

    lines = ["⏰ **Rappels actifs :**"]
    for reminder in reminders:
        line = (
            f"• **#{reminder['id']}** — {reminder['message']} — "
            f"{format_remaining_time(reminder['remind_at'])}"
        )
        if reminder.get("recurrence"):
            line += f" — 🔁 {recurrence_label(reminder['recurrence'])}"
        lines.append(line)
    await interaction.response.send_message("\n".join(lines))


@app_commands.command(name="annuler_rappel", description="Annuler un rappel actif par son numéro")
@app_commands.describe(rappel_id="Numéro affiché par /rappels")
async def cancel_reminder_slash(
    interaction: discord.Interaction,
    rappel_id: int,
) -> None:
    """Handler de la commande slash /annuler_rappel."""
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return

    if cancel_reminder(rappel_id, interaction.channel_id or 0):
        logger.info("Rappel #%s annulé (discord).", rappel_id)
        await interaction.response.send_message(f"✅ Rappel #{rappel_id} annulé.")
    else:
        await interaction.response.send_message(
            f"❌ Aucun rappel actif #{rappel_id} dans ce salon.", ephemeral=True
        )


@app_commands.command(name="snooze", description="Reporter un rappel actif")
@app_commands.describe(
    rappel_id="Numéro affiché par /rappels",
    duree="Durée du report : 10m par défaut, 1h, 2h...",
)
async def snooze_slash(
    interaction: discord.Interaction,
    rappel_id: int,
    duree: str = "10m",
) -> None:
    """Handler de la commande slash /snooze."""
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return

    if duree.isdigit():
        duree += "m"
    reminder = snooze_reminder(
        rappel_id, duree, chat_id=interaction.channel_id or 0
    )
    if reminder is None:
        await interaction.response.send_message(
            f"❌ Aucun rappel actif #{rappel_id} dans ce salon, ou durée invalide.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"⏰ Rappel #{rappel_id} reporté.\n{format_reminder_confirmation(reminder)}"
    )


@app_commands.command(name="rappel_termine", description="Clôturer un rappel actif")
@app_commands.describe(rappel_id="Numéro affiché par /rappels")
async def done_reminder_slash(
    interaction: discord.Interaction,
    rappel_id: int,
) -> None:
    """Handler de la commande slash /rappel_termine."""
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return

    reminder = complete_reminder(rappel_id, chat_id=interaction.channel_id or 0)
    if reminder is None:
        await interaction.response.send_message(
            f"❌ Aucun rappel actif #{rappel_id} dans ce salon.", ephemeral=True
        )
        return

    if reminder.get("status") == "pending" and reminder.get("completed_occurrence"):
        await interaction.response.send_message(
            f"✅ Occurrence terminée.\n{format_reminder_confirmation(reminder)}"
        )
    else:
        await interaction.response.send_message(f"✅ Rappel #{rappel_id} terminé.")


def _format_memory_discord(memory: dict) -> str:
    """Formate le profil et les souvenirs pour Discord."""
    profile = memory.get("profile", {})
    facts = memory.get("facts", [])
    if not profile and not facts:
        return "🧠 Aucune mémoire durable enregistrée."
    lines = ["🧠 **Mémoire durable**"]
    if profile:
        lines.append("\n**Profil :**")
        for key, value in sorted(profile.items()):
            lines.append(f"• `{key}` : {value}")
    if facts:
        lines.append("\n**Souvenirs :**")
        for fact in facts:
            lines.append(f"• `{fact['id']}` — {fact['text']}")
    text = "\n".join(lines)
    if len(text) > 1900:
        text = text[:1890].rstrip() + "\n…"
    return text


@app_commands.command(name="souviens", description="Ajouter un souvenir durable")
@app_commands.describe(texte="Le souvenir à conserver")
async def remember_slash(
    interaction: discord.Interaction, texte: str
) -> None:
    """Handler de la commande slash /souviens."""
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return

    result = await add_memory_fact(texte)
    if not result.get("success"):
        await interaction.response.send_message(
            f"❌ Impossible d'enregistrer le souvenir : {result.get('error', 'inconnue')}",
            ephemeral=True,
        )
        return
    fact = result.get("fact", {})
    await interaction.response.send_message(
        f"🧠 Souvenir enregistré (`{fact.get('id', '?')}`) : {fact.get('text', texte)}"
    )


@app_commands.command(name="oublie", description="Supprimer un souvenir ou toute la liste")
@app_commands.describe(cible="Identifiant du souvenir, ou 'all'")
async def forget_slash(
    interaction: discord.Interaction, cible: str
) -> None:
    """Handler de la commande slash /oublie."""
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return

    if cible.strip().lower() in {"all", "tout", "tous"}:
        result = await clear_memory_facts()
        if not result.get("success"):
            await interaction.response.send_message(
                f"❌ Erreur : {result.get('error', 'inconnue')}", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"🧹 {result.get('deleted_count', 0)} souvenir(s) supprimé(s)."
        )
        return

    result = await delete_memory_fact(cible.strip())
    if not result.get("success"):
        await interaction.response.send_message(
            f"❌ Souvenir `{cible}` introuvable.", ephemeral=True
        )
        return
    await interaction.response.send_message(f"🧹 Souvenir `{cible}` supprimé.")


@app_commands.command(name="profil", description="Afficher ou modifier la mémoire durable")
@app_commands.describe(
    cle="Clé du profil (ex: ville), ou 'suppr'",
    valeur="Valeur à enregistrer",
)
async def profile_slash(
    interaction: discord.Interaction,
    cle: Optional[str] = None,
    valeur: Optional[str] = None,
) -> None:
    """Handler de la commande slash /profil."""
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return

    if not cle:
        result = await fetch_memory()
        if not result.get("success"):
            await interaction.response.send_message(
                f"❌ Impossible de lire la mémoire : {result.get('error', 'inconnue')}",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            _format_memory_discord(result.get("memory", {}))
        )
        return

    if cle.lower() in {"suppr", "supprime", "delete"}:
        if not valeur:
            await interaction.response.send_message(
                "Utilisation : `/profil suppr <clé>`.", ephemeral=True
            )
            return
        result = await delete_memory_profile(valeur)
        if not result.get("success"):
            await interaction.response.send_message(
                f"❌ Préférence `{valeur}` introuvable.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"🧹 Préférence `{valeur}` supprimée."
        )
        return

    if not valeur:
        await interaction.response.send_message(
            "Utilisation : `/profil <clé> <valeur>`. Ex: `/profil ville Toulouse`.",
            ephemeral=True,
        )
        return

    result = await set_memory_profile(cle, valeur)
    if not result.get("success"):
        await interaction.response.send_message(
            f"❌ Impossible de modifier le profil : {result.get('error', 'inconnue')}",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        f"🧠 Profil mis à jour : `{cle}` = {valeur}"
    )


_MEMORY_CAPTURE_RE = re.compile(
    r"^\s*(?:souviens[- ]toi|retiens|m[ée]morise)"
    r"(?:\s+que|\s+qu['’]|\s*:)\s+(.+)",
    re.IGNORECASE,
)


def _build_discord_help() -> str:
    """Aide compacte : commandes canoniques uniquement, alias en note."""
    return "\n".join(
        [
            "🤖 **Assistant Personnel — commandes**",
            "",
            "**📅 Quotidien**",
            "`/briefing` · `/agenda` · `/tasks` · `/meteo` · `/search` · `/summarize`",
            "",
            "**⏰ Rappels**",
            "`/rappel` · `/rappels` · `/snooze` · `/rappel_termine` · `/annuler_rappel`",
            "",
            "**🧠 Mémoire**",
            "`/souviens` · `/oublie` · `/profil`",
            "",
            "**💬 Conversation**",
            "`/ask` · `/new` · `/resume` · `/delete` · `/compact` · `/session`",
            "",
            "**⚙️ Moteur**",
            "`/models` · `/setmodel` · `/effort` · `/mode` · `/permissions`",
            "",
            "**📊 Diagnostic**",
            "`/status` · `/usage` · `/skills`",
            "",
            "**🖥️ Serveur**",
            "`/server status` · `/server restart-runner` · `/server restart-gateway` "
            "· `/server tunnel-status` · `/server logs-gateway` · `/server logs-runner`",
            "",
            "Préfixe `!` : les mêmes commandes existent (`!briefing`, `!rappel`, etc.).",
            "Les variantes comme `!brief`, `!weather`, `!recherche` ou `!reminder` "
            "sont des **alias**, pas des commandes supplémentaires.",
        ]
    )


@app_commands.command(name="help", description="Afficher l'aide des commandes")
async def help_slash(interaction: discord.Interaction) -> None:
    """Handler de la commande slash /help."""
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    await interaction.response.send_message(_build_discord_help(), ephemeral=True)


@app_commands.command(name="ask", description="Pose une question à l'assistant personnel (supporte texte, image et audio)")
@app_commands.describe(
    question="Votre question ou tâche pour l'assistant",
    image="Image ou fichier audio optionnel à analyser",
)
async def ask_slash(
    interaction: discord.Interaction,
    question: str,
    image: Optional[discord.Attachment] = None,
) -> None:
    """Handler de la commande slash /ask."""
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return

    logger.info("Commande /ask de %d: '%s' (image=%s)", interaction.user.id, question[:80], bool(image))
    await _fast_ack(interaction, "⏳ Traitement en cours..." if not image else "👁️ Analyse de votre image en cours...")

    img_b64 = None
    img_name = None
    if image:
        content_type = image.content_type or ""
        if content_type.startswith("audio/"):
            transcript = await _transcribe_audio_attachment(image)
            if transcript:
                question = (
                    f"{question}\n\n[Transcription du message vocal: {transcript}]"
                )
            else:
                await interaction.edit_original_response(
                    content="❌ Je n'ai pas réussi à transcrire ce fichier audio."
                )
                return
        else:
            try:
                raw_bytes = await image.read()
                img_b64 = base64.b64encode(raw_bytes).decode("utf-8")
                img_name = image.filename
            except Exception as exc:
                logger.warning("Erreur lecture pièce jointe /ask: %s", exc)

    session_key = f"discord:{interaction.user.id}"
    result = await forward_to_runner(
        question,
        continue_session=True,
        timeout=180,
        image_base64=img_b64,
        image_filename=img_name,
        session_key=session_key,
    )

    if result.get("success"):
        response_text = result.get("output", "").strip() or "✅ Tâche terminée (pas de sortie)."
    else:
        error = result.get("error", "Erreur inconnue")
        response_text = f"❌ Erreur : {error}"

    await send_discord_long_response(interaction, response_text)


async def clear_channel_messages(channel: Optional[discord.abc.Messageable], limit: int = 50) -> None:
    """Efface les messages précédents dans le salon."""
    if not channel:
        return
    try:
        if hasattr(channel, "purge"):
            await channel.purge(limit=limit)
            return
    except discord.Forbidden:
        pass
    except Exception as exc:
        logger.warning("Erreur lors du purge du salon : %s", exc)

    # Repli : suppression des messages récents du bot
    try:
        bot = _get_bot()
        count = 0
        async for msg in channel.history(limit=min(limit, 30)):
            if msg.author == bot.user:
                try:
                    await msg.delete()
                    count += 1
                except Exception:
                    pass
    except Exception as exc:
        logger.warning("Erreur suppression message par message : %s", exc)


async def replay_conversation_history(channel: discord.abc.Messageable, history: list[dict]) -> None:
    """Affiche les anciens prompts et réponses pour la conversation réactivée."""
    if not history:
        return

    for turn in history:
        prompt_txt = turn.get("prompt", "").strip()
        resp_txt = turn.get("response", "").strip()

        if prompt_txt:
            msg_p = f"👤 **Vous :**\n{prompt_txt}"
            if len(msg_p) > 2000:
                msg_p = msg_p[:1990] + "..."
            try:
                await channel.send(msg_p)
            except Exception as e:
                logger.warning("Erreur affichage prompt historique : %s", e)

        if resp_txt:
            header = "🤖 **Assistant :**\n"
            full_text = header + resp_txt
            if len(full_text) <= 2000:
                try:
                    await channel.send(full_text)
                except Exception as e:
                    logger.warning("Erreur affichage réponse historique : %s", e)
            else:
                parts = [resp_txt[i:i + 1950] for i in range(0, len(resp_txt), 1950)]
                for idx, p in enumerate(parts):
                    prefix = header if idx == 0 else ""
                    try:
                        await channel.send(f"{prefix}{p}")
                    except Exception as e:
                        logger.warning("Erreur affichage bloc réponse : %s", e)


@app_commands.command(name="resume", description="Reprendre une conversation passée (vide et recharge le salon)")
@app_commands.describe(conversation="Choisissez la conversation à réactiver ou 'Nouvelle discussion'")
async def resume_slash(interaction: discord.Interaction, conversation: str) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return

    await _fast_ack(interaction, "⏳ Réactivation de la discussion...", ephemeral=True)

    # 1. Efface les messages précédents dans le salon
    if interaction.channel:
        await clear_channel_messages(interaction.channel)

    # 2. Si l'utilisateur choisit 'new' (Nouvelle discussion) : réinitialise et n'affiche rien
    if conversation == "new":
        await reset_session_remote()
        await interaction.followup.send("✨ **Nouvelle conversation prête.** Le salon a été vidé.", ephemeral=True)
        return

    # 3. Réactivation d'une ancienne conversation et rechargement de l'historique
    res = await resume_conversation_remote(conversation)
    if "error" in res:
        await interaction.followup.send(f"❌ Erreur : {res['error']}", ephemeral=True)
        return

    history = res.get("history", [])
    if interaction.channel and history:
        await replay_conversation_history(interaction.channel, history)

    await interaction.followup.send(
        f"🔄 **Conversation réactivée !** ({len(history)} échanges rechargés)",
        ephemeral=True,
    )


@resume_slash.autocomplete("conversation")
async def resume_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    convs = await fetch_conversations(limit=20)
    choices = [
        app_commands.Choice(name="➕ [Nouvelle discussion] Démarrer une conversation vierge", value="new")
    ]
    current_lower = current.lower()
    for c in convs:
        cid = c.get("id", "")
        title = c.get("title", "Discussion")
        date = c.get("date", "")
        label = f"[{date}] {title}"
        if len(label) > 100:
            label = label[:97] + "..."
        if not current or current_lower in label.lower() or current_lower in cid.lower():
            choices.append(app_commands.Choice(name=label, value=cid))
    return choices[:25]


@app_commands.command(name="delete", description="Supprimer définitivement une conversation passée")
@app_commands.describe(conversation="Choisissez la conversation à supprimer ou 'Tout supprimer'")
async def delete_slash(interaction: discord.Interaction, conversation: str) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return

    await _fast_ack(interaction, "⏳ Suppression de la conversation...", ephemeral=True)

    if conversation == "all":
        res = await delete_conversation_remote("all")
        if res.get("success"):
            count = res.get("deleted_count", 0)
            await interaction.followup.send(f"🗑️ **{count} conversation(s) passée(s) supprimée(s) définitivement.**", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Erreur : {res.get('error', 'Échec de suppression')}", ephemeral=True)
        return

    res = await delete_conversation_remote(conversation)
    if res.get("success"):
        await interaction.followup.send(f"🗑️ **Conversation `{conversation[:8]}...` supprimée avec succès !**", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Erreur lors de la suppression : {res.get('error', 'Conversation introuvable')}", ephemeral=True)


@delete_slash.autocomplete("conversation")
async def delete_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    convs = await fetch_conversations(limit=25)
    choices = [
        app_commands.Choice(name="💥 [TOUT SUPPRIMER] Effacer toutes les conversations passées", value="all")
    ]
    current_lower = current.lower()
    for c in convs:
        cid = c.get("id", "")
        title = c.get("title", "Discussion")
        date = c.get("date", "")
        label = f"🗑️ [{date}] {title}"
        if len(label) > 100:
            label = label[:97] + "..."
        if not current or current_lower in label.lower() or current_lower in cid.lower():
            choices.append(app_commands.Choice(name=label, value=cid))
    return choices[:25]


@app_commands.command(name="agenda", description="Consulter votre Google Agenda")
@app_commands.describe(periode="Période souhaitée")
@app_commands.choices(periode=[
    app_commands.Choice(name="📅 Aujourd'hui", value="aujourd'hui"),
    app_commands.Choice(name="🌅 Demain", value="demain"),
    app_commands.Choice(name="🗓️ Cette semaine", value="cette semaine"),
    app_commands.Choice(name="📆 Ce mois-ci", value="ce mois-ci"),
])
async def agenda_slash(interaction: discord.Interaction, periode: app_commands.Choice[str]) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    await _fast_ack(interaction, f"⏳ Consultation de votre Google Agenda ({periode.value})...")
    prompt = (
        f"Consulte mon Google Agenda pour la période suivante : {periode.value}. "
        "Liste les rendez-vous et événements avec leurs horaires de début et de fin, "
        "leur titre, lieu éventuel et détails pertinents. "
        "S'il n'y a aucun événement, confirme-le clairement."
    )
    res = await forward_to_runner(prompt, continue_session=False, timeout=120)
    if not res.get("success"):
        out = f"❌ Erreur agenda : {res.get('error', 'Le serveur maison n\'a pas répondu.')}"
    else:
        out = res.get("output", "").strip() or "📅 Aucun événement trouvé pour cette période."
    await send_discord_long_response(interaction, out)


@app_commands.command(name="tasks", description="Gérer vos tâches Google Tasks")
@app_commands.describe(
    action="Action à effectuer (lister, ajouter, terminer)",
    titre="Titre de la tâche (requis pour ajouter ou terminer)",
)
@app_commands.choices(action=[
    app_commands.Choice(name="📋 Lister mes tâches en cours", value="lister"),
    app_commands.Choice(name="➕ Ajouter une nouvelle tâche", value="ajouter"),
    app_commands.Choice(name="✅ Marquer comme terminée", value="terminer"),
])
async def tasks_slash(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
    titre: Optional[str] = None,
) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return

    await _fast_ack(interaction, "⏳ Gestion de vos tâches Google Tasks...")
    act = action.value

    if act == "lister":
        prompt = (
            "Consulte Google Tasks. "
            "Liste toutes les tâches en cours non terminées avec leur statut et date d'échéance éventuelle. "
            "S'il n'y en a aucune, indique-le clairement."
        )
    elif act == "ajouter":
        if not titre:
            await interaction.followup.send("⚠️ Veuillez renseigner le champ `titre` pour ajouter une tâche.")
            return
        prompt = f"Ajoute une nouvelle tâche dans Google Tasks avec le titre : '{titre}'."
    elif act == "terminer":
        if not titre:
            await interaction.followup.send("⚠️ Veuillez renseigner le champ `titre` pour la tâche à terminer.")
            return
        prompt = f"Marque la tâche '{titre}' comme terminée dans Google Tasks."
    else:
        prompt = f"Gère Google Tasks pour : {act} {titre or ''}"

    res = await forward_to_runner(prompt, continue_session=False, timeout=120)
    if not res.get("success"):
        out = f"❌ Erreur tâches : {res.get('error', 'Le serveur maison n\'a pas répondu.')}"
    else:
        out = res.get("output", "").strip() or "✅ Demande exécutée."
    await send_discord_long_response(interaction, out)


@app_commands.command(name="meteo", description="Consulter la météo mondiale en temps réel")
@app_commands.describe(ville="Ville ou commune (ex: Paris, Lyon, Toulouse, Tokyo)")
async def meteo_slash(interaction: discord.Interaction, ville: str = "Paris") -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    await _fast_ack(interaction, f"⏳ Consultation de la météo pour {ville}...")
    direct = await fetch_weather_direct(ville)
    if direct:
        out = direct
    else:
        prompt = (
            f"Consulte la météo actuelle et les prévisions pour la ville de '{ville}'.\n"
            "RÈGLE STRICTE : Utilise l'outil météo et donne immédiatement la température actuelle, le ressenti, l'état du ciel, les précipitations éventuelles et un conseil adapté. "
            "Ne lance aucune commande bash ni script python."
        )
        res = await forward_to_runner(prompt, continue_session=False, timeout=120)
        if not res.get("success"):
            out = f"❌ Erreur météo : {res.get('error', 'Le serveur maison n\'a pas répondu.')}"
        else:
            out = res.get("output", "").strip() or "⚠️ Aucune information météo trouvée."
    await send_discord_long_response(interaction, out)


@app_commands.command(name="search", description="Effectuer une recherche web")
@app_commands.describe(requete="Votre recherche sur le web")
async def search_slash(interaction: discord.Interaction, requete: str) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    await _fast_ack(interaction, f"⏳ Recherche sur le web : {requete[:50]}...")
    prompt = (
        f"Effectue une recherche sur le web pour la requête suivante : '{requete}'.\n"
        "RÈGLE STRICTE : Dès la première recherche effectuée, rédige immédiatement une synthèse claire, concise et structurée avec les chiffres clés et les sources en un seul tour. "
        "Ne lance AUCUNE commande bash ni script python, ne lis aucun fichier local. Sois direct et rapide."
    )
    res = await forward_to_runner(prompt, continue_session=False, timeout=120)
    if not res.get("success"):
        out = f"❌ Erreur recherche : {res.get('error', 'Le serveur maison n\'a pas répondu.')}"
    else:
        out = res.get("output", "").strip() or "⚠️ Aucun résultat trouvé."
    await send_discord_long_response(interaction, out)


@app_commands.command(name="summarize", description="Résumer une page web ou un article")
@app_commands.describe(url="Adresse URL complète à lire et résumer")
async def summarize_slash(interaction: discord.Interaction, url: str) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    await _fast_ack(interaction, "⏳ Analyse et résumé du lien en cours...")
    prompt = (
        f"Consulte le contenu de la page web suivante : '{url}'.\n"
        "RÈGLE STRICTE : Résume les points clés de manière synthétique et structurée avec des puces en un seul tour. "
        "Ne lance aucune commande bash ni script python."
    )
    res = await forward_to_runner(prompt, continue_session=False, timeout=120)
    if not res.get("success"):
        out = f"❌ Erreur résumé : {res.get('error', 'Le serveur maison n\'a pas répondu.')}"
    else:
        out = res.get("output", "").strip() or "⚠️ Impossible de résumer la page."
    await send_discord_long_response(interaction, out)


@app_commands.command(name="status", description="Afficher l'état du système et de l'assistant")
async def status_slash(interaction: discord.Interaction) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    await _fast_ack(interaction, "⏳ Récupération de l'état du système...")
    health, session = await asyncio.gather(fetch_health(), fetch_session())

    runner_ok = health.get("status") == "ok"
    agy_ok = health.get("agy_available", False)

    status_lines = [
        "**📊 État du Système Assistant Personnel**\n",
        f"• **Serveur Maison (home-runner) :** {'🟢 En ligne' if runner_ok else '🔴 Inaccessible'}",
        f"• **Moteur Antigravity (agy) :** {'🟢 Opérationnel' if agy_ok else '🔴 Dégradé'}",
        f"• **Modèle Actif :** `{session.get('model', 'défaut')}`",
        f"• **Effort de réflexion :** `{session.get('effort') or 'par défaut'}`",
        f"• **Mode d'exécution :** `{session.get('mode') or 'auto'}`",
        f"• **Conversation active :** {'🟢 Oui' if session.get('has_active_session') else '⚪ Vierge'}",
        "\n🔧 **Outils & Intégrations actifs :**",
        "• ✉️ Gmail & Google Calendar",
        "• 📋 Google Tasks",
        "• 🌤️ Météo mondiale (Open-Meteo)",
        "• 🔍 Recherche Web DuckDuckGo",
        "• 📄 Lecteur Web (Fetch)",
        "\n💡 *Pour changer de modèle, utilisez `/setmodel`.*",
    ]
    await interaction.followup.send("\n".join(status_lines))


@app_commands.command(name="server", description="Actions serveur whitelistées (services, tunnel, journaux)")
@app_commands.describe(action="Action serveur à exécuter")
@app_commands.choices(action=SERVER_ACTION_CHOICES)
async def server_slash(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
) -> None:
    """Exécute une action fixe sur le serveur, sans commande arbitraire."""
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    ok, texte = await _run_server_action(action.value)
    texte = texte.replace("```", "'''")
    await interaction.followup.send(
        f"{_server_reply(action.value, ok)}\n```\n{texte}\n```",
        ephemeral=True,
    )


@app_commands.command(name="compact", description="Compacter la mémoire de la conversation")
async def compact_slash(interaction: discord.Interaction) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    await _fast_ack(interaction, "⏳ Compactage de la mémoire en cours...")
    res = await forward_to_runner("/compact", continue_session=True, timeout=60)
    out = res.get("output", "").strip() or "✅ Mémoire compactée."
    await send_discord_long_response(interaction, out)


@app_commands.command(name="setmodel", description="Change le modèle d'IA actif (menu déroulant)")
@app_commands.describe(model="Choisissez le modèle à activer")
@app_commands.choices(model=[
    app_commands.Choice(name="⭐ Gemini 3.8 Flash (High) — Recommandé", value="gemini-3.8-flash-high"),
    app_commands.Choice(name="Gemini 3.8 Flash (Medium) — Équilibré", value="gemini-3.8-flash-medium"),
    app_commands.Choice(name="Gemini 3.8 Flash (Low) — Économe", value="gemini-3.8-flash-low"),
    app_commands.Choice(name="Claude Sonnet 4.6 (Thinking) — Code & Raisonnement", value="claude-sonnet-4-6"),
    app_commands.Choice(name="Claude Opus 4.6 (Thinking) — Capacité max", value="claude-opus-4-6-thinking"),
    app_commands.Choice(name="GPT-OSS 120B (Medium) — Open source", value="gpt-oss-120b-medium"),
])
async def setmodel_slash(interaction: discord.Interaction, model: app_commands.Choice[str]) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    res = await update_session_remote(model=model.value)
    if "error" in res:
        await interaction.response.send_message(f"❌ Erreur : {res['error']}")
    else:
        await interaction.response.send_message(f"✅ Modèle changé sur : `{model.value}` ({model.name})")


@app_commands.command(name="effort", description="Règle l'effort de réflexion (menu déroulant)")
@app_commands.describe(level="Choisissez le niveau d'effort")
@app_commands.choices(level=[
    app_commands.Choice(name="low (réponse rapide)", value="low"),
    app_commands.Choice(name="medium (standard)", value="medium"),
    app_commands.Choice(name="high (réflexion approfondie)", value="high"),
    app_commands.Choice(name="xhigh (très approfondie)", value="xhigh"),
    app_commands.Choice(name="max (maximum)", value="max"),
])
async def effort_slash(interaction: discord.Interaction, level: app_commands.Choice[str]) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    await update_session_remote(effort=level.value)
    await interaction.response.send_message(f"✅ Niveau d'effort réglé sur : `{level.value}`")


@app_commands.command(name="mode", description="Règle le mode d'exécution d'Antigravity")
@app_commands.describe(mode="Choisissez le mode d'exécution")
@app_commands.choices(mode=[
    app_commands.Choice(name="auto (Exécution standard)", value="auto"),
    app_commands.Choice(name="plan (Planification préalable)", value="plan"),
    app_commands.Choice(name="accept-edits (Acceptation automatique des modifs)", value="accept-edits"),
])
async def mode_slash(interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    await update_session_remote(mode=mode.value)
    await interaction.response.send_message(f"✅ Mode d'exécution réglé sur : `{mode.value}`")


@app_commands.command(name="permissions", description="Politique de permissions des outils sensibles")
@app_commands.describe(policy="Choisissez la politique de permissions")
@app_commands.choices(policy=[
    app_commands.Choice(name="always-proceed (Exécution automatique sans confirmation)", value="always-proceed"),
    app_commands.Choice(name="request-review (Demande confirmation avant actions)", value="request-review"),
])
async def permissions_slash(interaction: discord.Interaction, policy: app_commands.Choice[str]) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    await update_session_remote(permission_mode=policy.value)
    await interaction.response.send_message(f"✅ Politique de permissions : `{policy.value}`")


@app_commands.command(name="new", description="Démarre une nouvelle conversation vierge (vide le salon)")
async def new_slash(interaction: discord.Interaction) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    await interaction.response.send_message("✨ **Nouvelle conversation prête.** Le salon a été vidé.", ephemeral=True)
    await reset_session_remote()
    if interaction.channel:
        await clear_channel_messages(interaction.channel)


@app_commands.command(name="models", description="Liste les modèles Antigravity disponibles")
async def models_slash(interaction: discord.Interaction) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    data = await fetch_models()
    models_list = data.get("models", [])
    current = data.get("current", "")

    lines = ["**🧠 Modèles Antigravity disponibles :**\n"]
    for m in models_list:
        mid = m.get("id")
        name = m.get("name")
        desc = m.get("desc", "")
        prefix = "⭐ " if mid == current else "• "
        lines.append(f"{prefix}`{mid}` — *{name}* ({desc})")

    lines.append("\n👉 Pour changer : `/setmodel` (menu déroulant)")
    await interaction.response.send_message("\n".join(lines))


@app_commands.command(name="session", description="Affiche les réglages de la session active")
async def session_slash(interaction: discord.Interaction) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    s = await fetch_session()
    text = (
        "⚙️ **Configuration de la session :**\n"
        f"• **Modèle :** `{s.get('model', 'défaut')}`\n"
        f"• **Effort :** `{s.get('effort') or 'par défaut'}`\n"
        f"• **Mode :** `{s.get('mode') or 'auto'}`\n"
        f"• **Permissions :** `{s.get('permission_mode', 'always-proceed')}`\n"
        f"• **Conversation active :** `{'Oui' if s.get('has_active_session') else 'Non (vierge)'}`"
    )
    await interaction.response.send_message(text)


@app_commands.command(name="usage", description="Consulte vos quotas et jetons consommés")
async def usage_slash(interaction: discord.Interaction) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    await _fast_ack(interaction, "⏳ Récupération de l'usage...")
    res = await forward_to_runner("/usage", continue_session=False)
    await send_discord_long_response(interaction, res.get("output", "Aucune information reçue."))


@app_commands.command(name="skills", description="Affiche les compétences actives")
async def skills_slash(interaction: discord.Interaction) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    await _fast_ack(interaction, "⏳ Récupération des compétences...")
    res = await forward_to_runner("/skills", continue_session=False)
    await send_discord_long_response(interaction, res.get("output", "Aucune compétence trouvée."))


# ── Commandes préfixées ───────────────────────────────────────────────────────


def _register_prefix_commands(bot: AssistantBot) -> None:
    """Enregistre les commandes préfixées (!) sur l'instance bot."""

    @bot.command(name="help", help="Affiche l'aide des commandes")
    async def help_prefix(ctx: commands.Context) -> None:
        """Aide dédupliquée : seules les commandes canoniques sont listées."""
        if not _is_allowed(ctx.author.id):
            return
        await ctx.send(_build_discord_help())

    @bot.command(name="ask", help="Pose une question à l'assistant. Ex: !ask Quelle heure est-il ?")
    async def ask_prefix(ctx: commands.Context, *, question: str) -> None:
        """Handler de la commande préfixée !ask."""
        if not _is_allowed(ctx.author.id):
            logger.warning("Accès refusé pour l'utilisateur Discord %d.", ctx.author.id)
            await ctx.send("⛔ Accès non autorisé.")
            return

        logger.info("Commande !ask de %d: '%s'", ctx.author.id, question[:80])

        img_b64 = None
        img_name = None
        if ctx.message.attachments:
            for att in ctx.message.attachments:
                content_type = att.content_type or ""
                if content_type.startswith("image/") and img_b64 is None:
                    try:
                        raw_bytes = await att.read()
                        img_b64 = base64.b64encode(raw_bytes).decode("utf-8")
                        img_name = att.filename
                    except Exception as exc:
                        logger.warning("Erreur lecture image !ask: %s", exc)
                elif content_type.startswith("audio/"):
                    transcript = await _transcribe_audio_attachment(att)
                    if transcript:
                        question = (
                            f"{question}\n\n[Transcription du message vocal: {transcript}]"
                        )
                    break

        waiting_msg = await ctx.send("⏳ Traitement en cours..." if not img_b64 else "👁️ Analyse de votre image en cours...")
        result = await forward_to_runner(
            question,
            continue_session=True,
            image_base64=img_b64,
            image_filename=img_name,
        )

        if result.get("success"):
            response_text = result.get("output", "").strip() or "✅ Tâche terminée (pas de sortie)."
        else:
            error = result.get("error", "Erreur inconnue")
            response_text = f"❌ Erreur : {error}"
            logger.error("Erreur runner (!ask): %s", error)

        processed_text, rendered_images = await extract_and_render_latex(response_text, dark_mode=True)
        code_snippets = extract_downloadable_code(response_text, min_chars=700)

        discord_files: list[discord.File] = []
        for img in rendered_images:
            discord_files.append(discord.File(io.BytesIO(img["bytes"]), filename=img["filename"]))
        for code in code_snippets:
            discord_files.append(discord.File(io.BytesIO(code["bytes"]), filename=code["filename"]))

        parts = smart_split_discord(processed_text, max_chars=1950)
        first = True
        for p in parts:
            if first:
                first = False
                await waiting_msg.edit(content=p, attachments=discord_files)
            else:
                await ctx.send(content=p)

    @bot.command(name="models", help="Liste les modèles Antigravity disponibles")
    async def models_cmd(ctx: commands.Context) -> None:
        if not _is_allowed(ctx.author.id):
            return
        data = await fetch_models()
        models_list = data.get("models", [])
        current = data.get("current", "")

        lines = ["**🧠 Modèles Antigravity disponibles :**\n"]
        for m in models_list:
            mid = m.get("id")
            name = m.get("name")
            desc = m.get("desc", "")
            prefix = "⭐ " if mid == current else "• "
            lines.append(f"{prefix}`{mid}` — *{name}* ({desc})")

        lines.append("\n👉 Pour changer : `!setmodel <nom>`")
        await ctx.send("\n".join(lines))

    @bot.command(name="setmodel", help="Change le modèle actif. Ex: !setmodel claude-sonnet-4-6")
    async def setmodel_cmd(ctx: commands.Context, *, model_name: str = "") -> None:
        if not _is_allowed(ctx.author.id):
            return
        if not model_name:
            await ctx.send("⚠️ Spécifiez un modèle. Ex: `!setmodel claude-sonnet-4-6`. Tapez `!models` pour la liste.")
            return
        res = await update_session_remote(model=model_name.strip())
        if "error" in res:
            await ctx.send(f"❌ Erreur : {res['error']}")
        else:
            await ctx.send(f"✅ Modèle changé sur : `{model_name.strip()}`")

    @bot.command(name="effort", help="Règle l'effort de réflexion (low|medium|high|xhigh|max)")
    async def effort_cmd(ctx: commands.Context, *, level: str = "") -> None:
        if not _is_allowed(ctx.author.id):
            return
        valid = ["low", "medium", "high", "xhigh", "max"]
        if not level or level.lower() not in valid:
            await ctx.send(f"⚠️ Spécifiez un niveau valide : `{', '.join(valid)}`. Ex: `!effort high`")
            return
        await update_session_remote(effort=level.lower())
        await ctx.send(f"✅ Niveau d'effort réglé sur : `{level.lower()}`")

    @bot.command(name="mode", help="Règle le mode d'exécution (auto|plan|accept-edits)")
    async def mode_cmd(ctx: commands.Context, *, mode_val: str = "") -> None:
        if not _is_allowed(ctx.author.id):
            return
        valid = ["accept-edits", "auto", "plan"]
        if not mode_val or mode_val.lower() not in valid:
            await ctx.send(f"⚠️ Spécifiez un mode valide : `{', '.join(valid)}`. Ex: `!mode plan`")
            return
        await update_session_remote(mode=mode_val.lower())
        await ctx.send(f"✅ Mode d'exécution réglé sur : `{mode_val.lower()}`")

    @bot.command(name="permissions", help="Règle la politique de permissions (always-proceed|request-review)")
    async def perms_cmd(ctx: commands.Context, *, perm_val: str = "") -> None:
        if not _is_allowed(ctx.author.id):
            return
        valid = ["always-proceed", "request-review"]
        if not perm_val or perm_val.lower() not in valid:
            await ctx.send(f"⚠️ Spécifiez : `{', '.join(valid)}`. Ex: `!permissions always-proceed`")
            return
        await update_session_remote(permission_mode=perm_val.lower())
        await ctx.send(f"✅ Politique de permissions : `{perm_val.lower()}`")

    @bot.command(name="new", aliases=["clear", "reset"], help="Démarre une nouvelle conversation vierge (vide le salon)")
    async def new_cmd(ctx: commands.Context) -> None:
        if not _is_allowed(ctx.author.id):
            return
        await reset_session_remote()
        await clear_channel_messages(ctx.channel)

    @bot.command(name="session", help="Affiche l'état de la session active")
    async def session_cmd(ctx: commands.Context) -> None:
        if not _is_allowed(ctx.author.id):
            return
        s = await fetch_session()
        text = (
            "⚙️ **Configuration de la session :**\n"
            f"• **Modèle :** `{s.get('model', 'défaut')}`\n"
            f"• **Effort :** `{s.get('effort') or 'par défaut'}`\n"
            f"• **Mode :** `{s.get('mode') or 'auto'}`\n"
            f"• **Permissions :** `{s.get('permission_mode', 'always-proceed')}`\n"
            f"• **Conversation active :** `{'Oui' if s.get('has_active_session') else 'Non (vierge)'}`"
        )
        await ctx.send(text)

    @bot.command(name="usage", help="Affiche la consommation de quota")
    async def usage_cmd(ctx: commands.Context) -> None:
        if not _is_allowed(ctx.author.id):
            return
        waiting = await ctx.send("⏳ Récupération des quotas...")
        res = await forward_to_runner("/usage", continue_session=False)
        await waiting.edit(content=res.get("output", "Aucune information reçue."))

    @bot.command(name="rappel", aliases=["reminder"], help="Programmer un rappel. Ex: !rappel chaque jour 8h Sortir le pain")
    async def reminder_cmd(ctx: commands.Context, *, args: str = "") -> None:
        if not _is_allowed(ctx.author.id):
            return
        if not args.strip():
            await ctx.send(
                "⏰ Utilisation : `!rappel [récurrence] <durée> <message>`.\n"
                "Ex: `!rappel 15m Sortir le pain` ou `!rappel chaque jour 8h Faire mes étirements`."
            )
            return

        remind_at, message, recurrence, error = parse_reminder_command(args)
        if remind_at is None or not message:
            await ctx.send(f"❌ {error}")
            return

        reminder = add_reminder(
            platform="discord",
            chat_id=ctx.channel.id,
            target_id=ctx.author.id,
            message=message,
            remind_at=remind_at,
            target_name=str(ctx.author),
            recurrence=recurrence,
        )
        logger.info(
            "Rappel #%s créé via !rappel (discord -> %s, récurrence=%s) : %s",
            reminder["id"],
            ctx.channel.id,
            recurrence,
            message,
        )
        await ctx.send(format_reminder_confirmation(reminder))

    @bot.command(name="rappels", aliases=["reminders"], help="Lister les rappels actifs de ce salon")
    async def reminders_cmd(ctx: commands.Context) -> None:
        if not _is_allowed(ctx.author.id):
            return
        reminders = list_active_reminders(ctx.channel.id)
        if not reminders:
            await ctx.send("⏰ Aucun rappel actif.")
            return
        lines = ["⏰ **Rappels actifs :**"]
        for reminder in reminders:
            line = (
                f"• **#{reminder['id']}** — {reminder['message']} — "
                f"{format_remaining_time(reminder['remind_at'])}"
            )
            if reminder.get("recurrence"):
                line += f" — 🔁 {recurrence_label(reminder['recurrence'])}"
            lines.append(line)
        await ctx.send("\n".join(lines))

    @bot.command(name="snooze", aliases=["report"], help="Reporter un rappel. Ex: !snooze 3 10m")
    async def snooze_cmd(
        ctx: commands.Context, rappel_id: int = 0, duree: str = "10m"
    ) -> None:
        if not _is_allowed(ctx.author.id):
            return
        if not rappel_id:
            await ctx.send("Utilisation : `!snooze <id> [durée]`.")
            return
        if duree.isdigit():
            duree += "m"
        reminder = snooze_reminder(rappel_id, duree, chat_id=ctx.channel.id)
        if reminder is None:
            await ctx.send(
                f"❌ Aucun rappel actif #{rappel_id} dans ce salon, ou durée invalide."
            )
            return
        await ctx.send(
            f"⏰ Rappel #{rappel_id} reporté.\n{format_reminder_confirmation(reminder)}"
        )

    @bot.command(name="rappel_termine", aliases=["rappel_done", "done_rappel"], help="Clôturer un rappel. Ex: !rappel_termine 3")
    async def done_reminder_cmd(ctx: commands.Context, rappel_id: int = 0) -> None:
        if not _is_allowed(ctx.author.id):
            return
        if not rappel_id:
            await ctx.send("Utilisation : `!rappel_termine <id>`.")
            return
        reminder = complete_reminder(rappel_id, chat_id=ctx.channel.id)
        if reminder is None:
            await ctx.send(f"❌ Aucun rappel actif #{rappel_id} dans ce salon.")
            return
        if reminder.get("status") == "pending" and reminder.get("completed_occurrence"):
            await ctx.send(
                f"✅ Occurrence terminée.\n{format_reminder_confirmation(reminder)}"
            )
        else:
            await ctx.send(f"✅ Rappel #{rappel_id} terminé.")

    @bot.command(name="annuler_rappel", aliases=["annuler-rappel", "delrappel"], help="Annuler un rappel. Ex: !annuler_rappel 3")
    async def cancel_reminder_cmd(ctx: commands.Context, rappel_id: int = 0) -> None:
        if not _is_allowed(ctx.author.id):
            return
        if not rappel_id:
            await ctx.send("Utilisation : `!annuler_rappel <id>`.")
            return
        if cancel_reminder(rappel_id, ctx.channel.id):
            logger.info("Rappel #%s annulé (discord).", rappel_id)
            await ctx.send(f"✅ Rappel #{rappel_id} annulé.")
        else:
            await ctx.send(f"❌ Aucun rappel actif #{rappel_id} dans ce salon.")

    @bot.command(name="souviens", aliases=["retiens"], help="Ajouter un souvenir durable. Ex: !souviens J'aime le café sans sucre")
    async def remember_cmd(ctx: commands.Context, *, texte: str = "") -> None:
        if not _is_allowed(ctx.author.id):
            return
        if not texte.strip():
            await ctx.send("Utilisation : `!souviens <texte>`.")
            return
        result = await add_memory_fact(texte)
        if not result.get("success"):
            await ctx.send(
                f"❌ Impossible d'enregistrer le souvenir : {result.get('error', 'inconnue')}"
            )
            return
        fact = result.get("fact", {})
        await ctx.send(
            f"🧠 Souvenir enregistré (`{fact.get('id', '?')}`) : {fact.get('text', texte)}"
        )

    @bot.command(name="oublie", help="Supprimer un souvenir. Ex: !oublie abc12345 ou !oublie all")
    async def forget_cmd(ctx: commands.Context, cible: str = "") -> None:
        if not _is_allowed(ctx.author.id):
            return
        if not cible:
            await ctx.send("Utilisation : `!oublie <id>` ou `!oublie all`.")
            return
        if cible.lower() in {"all", "tout", "tous"}:
            result = await clear_memory_facts()
            if not result.get("success"):
                await ctx.send(f"❌ Erreur : {result.get('error', 'inconnue')}")
                return
            await ctx.send(f"🧹 {result.get('deleted_count', 0)} souvenir(s) supprimé(s).")
            return
        result = await delete_memory_fact(cible)
        if not result.get("success"):
            await ctx.send(f"❌ Souvenir `{cible}` introuvable.")
            return
        await ctx.send(f"🧹 Souvenir `{cible}` supprimé.")

    @bot.command(name="profil", aliases=["memoire"], help="Afficher ou modifier le profil. Ex: !profil ville Toulouse")
    async def profile_cmd(ctx: commands.Context, *, args: str = "") -> None:
        if not _is_allowed(ctx.author.id):
            return
        parts = args.split(maxsplit=1)
        if not parts:
            result = await fetch_memory()
            if not result.get("success"):
                await ctx.send(f"❌ Impossible de lire la mémoire : {result.get('error', 'inconnue')}")
                return
            await ctx.send(_format_memory_discord(result.get("memory", {})))
            return
        if parts[0].lower() in {"suppr", "supprime", "delete"}:
            if len(parts) < 2:
                await ctx.send("Utilisation : `!profil suppr <clé>`.")
                return
            result = await delete_memory_profile(parts[1])
            if not result.get("success"):
                await ctx.send(f"❌ Préférence `{parts[1]}` introuvable.")
                return
            await ctx.send(f"🧹 Préférence `{parts[1]}` supprimée.")
            return
        if len(parts) < 2:
            await ctx.send("Utilisation : `!profil <clé> <valeur>`. Ex: `!profil ville Toulouse`.")
            return
        result = await set_memory_profile(parts[0], parts[1])
        if not result.get("success"):
            await ctx.send(f"❌ Impossible de modifier le profil : {result.get('error', 'inconnue')}")
            return
        await ctx.send(f"🧠 Profil mis à jour : `{parts[0]}` = {parts[1]}")

    @bot.command(name="briefing", aliases=["brief"], help="Génère votre briefing matinal (mails triés, agenda, météo)")
    async def briefing_cmd(ctx: commands.Context) -> None:
        if not _is_allowed(ctx.author.id):
            return
        waiting = await ctx.send("⏳ Préparation de votre briefing matinal (tri des e-mails, agenda, météo)...")
        res = await get_briefing_text(timeout=120)
        output = res.get("output", "").strip() or "⚠️ Impossible de générer le briefing."
        if len(output) > 2000:
            output = output[:1990] + "..."
        await waiting.edit(content=output)

    @bot.command(name="skills", help="Affiche les compétences actives")
    async def skills_cmd(ctx: commands.Context) -> None:
        if not _is_allowed(ctx.author.id):
            return
        waiting = await ctx.send("⏳ Récupération des compétences...")
        res = await forward_to_runner("/skills", continue_session=False)
        await waiting.edit(content=res.get("output", "Aucune compétence trouvée."))

    @bot.command(name="resume", help="Reprendre une conversation passée (vide et recharge le salon). Ex: !resume <id>")
    async def resume_cmd(ctx: commands.Context, conversation_id: str = "") -> None:
        if not _is_allowed(ctx.author.id):
            return
        cid = conversation_id.strip()
        if not cid:
            convs = await fetch_conversations(limit=6)
            if not convs:
                await ctx.send("ℹ️ Aucune conversation passée trouvée.")
                return
            lines = [
                "📂 **Conversations passées disponibles :**",
                "• `!resume new` — ➕ Démarrer une nouvelle discussion vierge",
            ]
            for c in convs:
                lines.append(f"• `!resume {c['id']}` — *{c['date']}* : {c['title']}")
            lines.append("\n👉 Tapez `!resume <id>` pour réactiver et recharger une conversation.")
            await ctx.send("\n".join(lines))
            return

        # 1. Efface les messages précédents
        await clear_channel_messages(ctx.channel)

        # 2. Nouvelle conversation
        if cid.lower() in ("new", "nouvelle", "reset"):
            await reset_session_remote()
            return

        # 3. Ancienne conversation
        res = await resume_conversation_remote(cid)
        if "error" in res:
            await ctx.send(f"❌ Erreur : {res['error']}")
            return

        history = res.get("history", [])
        await replay_conversation_history(ctx.channel, history)

    @bot.command(name="delete", aliases=["del", "supprimer"], help="Supprimer une conversation passée. Ex: !delete <id> ou !delete all")
    async def delete_cmd(ctx: commands.Context, conversation_id: str = "") -> None:
        if not _is_allowed(ctx.author.id):
            return
        cid = conversation_id.strip()
        if not cid:
            convs = await fetch_conversations(limit=8)
            if not convs:
                await ctx.send("ℹ️ Aucune conversation passée à supprimer.")
                return
            lines = [
                "🗑️ **Conversations passées à supprimer :**",
                "• `!delete all` — 💥 Supprimer toutes les conversations passées",
            ]
            for c in convs:
                lines.append(f"• `!delete {c['id']}` — *{c['date']}* : {c['title']}")
            lines.append("\n👉 Tapez `!delete <id>` pour supprimer définitivement une conversation.")
            await ctx.send("\n".join(lines))
            return

        res = await delete_conversation_remote(cid)
        if res.get("success"):
            if cid.lower() == "all":
                count = res.get("deleted_count", 0)
                await ctx.send(f"🗑️ **{count} conversation(s) passée(s) supprimée(s) définitivement.**")
            else:
                await ctx.send(f"🗑️ **Conversation `{cid[:8]}...` supprimée définitivement avec succès !**")
        else:
            await ctx.send(f"❌ Erreur : {res.get('error', 'Suppression échouée')}")

    @bot.command(name="agenda", aliases=["cal"], help="Consulter votre Google Agenda. Ex: !agenda aujourd'hui")
    async def agenda_cmd(ctx: commands.Context, *, periode: str = "aujourd'hui") -> None:
        if not _is_allowed(ctx.author.id):
            return
        waiting = await ctx.send(f"⏳ Consultation de votre agenda ({periode})...")
        prompt = (
            f"Consulte mon Google Agenda pour la période suivante : {periode}. "
            "Liste les rendez-vous et événements avec leurs horaires de début et de fin, "
            "leur titre, lieu éventuel et détails pertinents. "
            "S'il n'y a aucun événement, confirme-le clairement."
        )
        res = await forward_to_runner(prompt, continue_session=False, timeout=120)
        if not res.get("success"):
            out = f"❌ Erreur agenda : {res.get('error', 'Le serveur maison n\'a pas répondu à temps.')}"
        else:
            out = res.get("output", "").strip() or "📅 Aucun événement trouvé pour cette période."
        if len(out) > 2000:
            out = out[:1990] + "..."
        await waiting.edit(content=out)

    @bot.command(name="tasks", aliases=["task", "todo"], help="Gérer Google Tasks. Ex: !tasks list OU !tasks add Acheter du pain")
    async def tasks_cmd(ctx: commands.Context, action: str = "list", *, details: str = "") -> None:
        if not _is_allowed(ctx.author.id):
            return
        act = action.lower()
        if act in ("list", "lister", "ls"):
            prompt = (
                "Consulte Google Tasks. "
                "Liste toutes les tâches en cours non terminées avec leur statut et date d'échéance éventuelle. "
                "S'il n'y en a aucune, indique-le clairement."
            )
        elif act in ("add", "ajouter", "new"):
            if not details:
                await ctx.send("⚠️ Spécifiez un titre : `!tasks add <titre>`")
                return
            prompt = f"Ajoute une nouvelle tâche dans Google Tasks avec le titre : '{details}'."
        elif act in ("done", "terminer", "complete"):
            if not details:
                await ctx.send("⚠️ Spécifiez la tâche à terminer : `!tasks done <titre>`")
                return
            prompt = f"Marque la tâche '{details}' comme terminée dans Google Tasks."
        else:
            prompt = f"Gère Google Tasks pour : {action} {details}"

        waiting = await ctx.send("⏳ Traitement Google Tasks...")
        res = await forward_to_runner(prompt, continue_session=False, timeout=120)
        if not res.get("success"):
            out = f"❌ Erreur tâches : {res.get('error', 'Le serveur maison n\'a pas répondu à temps.')}"
        else:
            out = res.get("output", "").strip() or "✅ Tâche effectuée."
        if len(out) > 2000:
            out = out[:1990] + "..."
        await waiting.edit(content=out)

    @bot.command(name="meteo", aliases=["weather"], help="Consulter la météo mondiale. Ex: !meteo Tokyo")
    async def meteo_cmd(ctx: commands.Context, *, ville: str = "Paris") -> None:
        if not _is_allowed(ctx.author.id):
            return
        waiting = await ctx.send(f"⏳ Consultation météo pour {ville}...")
        direct = await fetch_weather_direct(ville)
        if direct:
            out = direct
        else:
            prompt = (
                f"Consulte la météo actuelle et les prévisions pour la ville de '{ville}'.\n"
                "RÈGLE STRICTE : Utilise l'outil météo et donne immédiatement la température actuelle, le ressenti, l'état du ciel, les précipitations éventuelles et un conseil adapté. "
                "Ne lance aucune commande bash ni script python."
            )
            res = await forward_to_runner(prompt, continue_session=False, timeout=120)
            if not res.get("success"):
                out = f"❌ Erreur météo : {res.get('error', 'Le serveur maison n\'a pas répondu à temps.')}"
            else:
                out = res.get("output", "").strip() or "⚠️ Aucune information météo trouvée."
        if len(out) > 2000:
            out = out[:1990] + "..."
        await waiting.edit(content=out)

    @bot.command(name="search", aliases=["recherche"], help="Recherche web. Ex: !search intelligence artificielle")
    async def search_cmd(ctx: commands.Context, *, requete: str = "") -> None:
        if not _is_allowed(ctx.author.id):
            return
        if not requete:
            await ctx.send("⚠️ Spécifiez une recherche : `!search <mots-clés>`")
            return
        waiting = await ctx.send(f"🔍 Recherche pour '{requete}'...")
        prompt = (
            f"Effectue une recherche sur le web pour la requête suivante : '{requete}'.\n"
            "RÈGLE STRICTE : Dès la première recherche effectuée, rédige immédiatement une synthèse claire, concise et structurée avec les chiffres clés et les sources en un seul tour. "
            "Ne lance AUCUNE commande bash ni script python, ne lis aucun fichier local. Sois direct et rapide."
        )
        res = await forward_to_runner(prompt, continue_session=False, timeout=120)
        if not res.get("success"):
            out = f"❌ Erreur recherche : {res.get('error', 'Le serveur maison n\'a pas répondu à temps.')}"
        else:
            out = res.get("output", "").strip() or "⚠️ Aucun résultat."
        if len(out) > 2000:
            out = out[:1990] + "..."
        await waiting.edit(content=out)

    @bot.command(name="summarize", help="Résumer une page web. Ex: !summarize https://...")
    async def summarize_cmd(ctx: commands.Context, url: str = "") -> None:
        if not _is_allowed(ctx.author.id):
            return
        if not url:
            await ctx.send("⚠️ Spécifiez une URL : `!summarize <url>`")
            return
        waiting = await ctx.send(f"⏳ Résumé de {url}...")
        prompt = (
            f"Consulte le contenu de la page web suivante : '{url}'.\n"
            "RÈGLE STRICTE : Résume les points clés de manière synthétique et structurée avec des puces en un seul tour. "
            "Ne lance aucune commande bash ni script python."
        )
        res = await forward_to_runner(prompt, continue_session=False, timeout=120)
        if not res.get("success"):
            out = f"❌ Erreur résumé : {res.get('error', 'Le serveur maison n\'a pas répondu à temps.')}"
        else:
            out = res.get("output", "").strip() or "⚠️ Impossible de résumer la page."
        if len(out) > 2000:
            out = out[:1990] + "..."
        await waiting.edit(content=out)

    @bot.command(name="status", help="Statut global du système et des services")
    async def status_cmd(ctx: commands.Context) -> None:
        if not _is_allowed(ctx.author.id):
            return
        waiting = await ctx.send("⏳ Vérification des composants...")
        health, session = await asyncio.gather(fetch_health(), fetch_session())

        runner_ok = health.get("status") == "ok"
        agy_ok = health.get("agy_available", False)

        status_lines = [
            "**📊 État du Système Assistant Personnel**\n",
            f"• **Serveur Maison (home-runner) :** {'🟢 En ligne' if runner_ok else '🔴 Inaccessible'}",
            f"• **Moteur Antigravity (agy) :** {'🟢 Opérationnel' if agy_ok else '🔴 Dégradé'}",
            f"• **Modèle Actif :** `{session.get('model', 'défaut')}`",
            f"• **Effort de réflexion :** `{session.get('effort') or 'par défaut'}`",
            f"• **Mode d'exécution :** `{session.get('mode') or 'auto'}`",
            f"• **Conversation active :** {'🟢 Oui' if session.get('has_active_session') else '⚪ Vierge'}",
            "\n🔧 **Outils & Intégrations actifs :**",
            "• ✉️ Gmail & Google Calendar",
            "• 📋 Google Tasks",
            "• 🌤️ Météo mondiale (Open-Meteo)",
            "• 🔍 Recherche Web DuckDuckGo",
            "• 📄 Lecteur Web (Fetch)",
        ]
        await waiting.edit(content="\n".join(status_lines))

    @bot.command(name="server", help="Actions serveur whitelistées : status, restart-runner, restart-gateway, tunnel-status, logs-gateway, logs-runner")
    async def server_cmd(ctx: commands.Context, action: str = "status") -> None:
        if not _is_allowed(ctx.author.id):
            return
        if action not in SERVER_ACTIONS:
            await ctx.reply(
                "Action inconnue. Actions : " + ", ".join(SERVER_ACTIONS)
            )
            return
        waiting = await ctx.send("⏳ Exécution de l'action serveur…")
        ok, texte = await _run_server_action(action)
        texte = texte.replace("```", "'''")
        await waiting.edit(
            content=f"{_server_reply(action, ok)}\n```\n{texte}\n```"
        )

    @bot.command(name="compact", help="Compacter la mémoire de contexte")
    async def compact_cmd(ctx: commands.Context) -> None:
        if not _is_allowed(ctx.author.id):
            return
        waiting = await ctx.send("🧹 Compactage de la mémoire...")
        res = await forward_to_runner("/compact", continue_session=True, timeout=60)
        out = res.get("output", "").strip() or "✅ Mémoire compactée."
        await waiting.edit(content=out)

    @bot.event
    async def on_message(message: discord.Message) -> None:
        """Gère les messages texte du salon (commandes préfixées ou chat direct)."""
        if message.author == bot.user:
            return
        if message.content.startswith("!"):
            await bot.process_commands(message)
            return
        if not _is_allowed(message.author.id):
            return

        _save_discord_channel_id(message.channel.id)

        img_b64 = None
        img_name = None
        audio_attachment = None
        if message.attachments:
            for att in message.attachments:
                content_type = att.content_type or ""
                if content_type.startswith("image/") and img_b64 is None:
                    try:
                        raw_bytes = await att.read()
                        img_b64 = base64.b64encode(raw_bytes).decode("utf-8")
                        img_name = att.filename
                    except Exception as e:
                        logger.warning("Erreur lecture pièce jointe on_message: %s", e)
                elif content_type.startswith("audio/") and audio_attachment is None:
                    audio_attachment = att

        prompt = message.content.strip()
        if not prompt and img_b64:
            prompt = "Analyse cette image en détail et décris ce qu'elle contient ou réponds aux éléments visibles."

        if not prompt and not img_b64 and audio_attachment is None:
            return

        async with message.channel.typing():
            transcript_prefix = ""
            if audio_attachment is not None:
                transcript = await _transcribe_audio_attachment(audio_attachment)
                if transcript:
                    transcript_prefix = f"🎤 **Transcription :** {transcript}\n\n"
                    prompt = (
                        f"{prompt}\n\n{transcript}".strip() if prompt else transcript
                    )
                elif not prompt and not img_b64:
                    await message.reply(
                        "❌ Je n'ai pas réussi à transcrire ce message vocal. "
                        "Réessayez ou envoyez-le en texte."
                    )
                    return

            memory_match = _MEMORY_CAPTURE_RE.match(prompt)
            if memory_match:
                memory_text = memory_match.group(1).strip()
                if memory_text:
                    result = await add_memory_fact(memory_text)
                    if result.get("success"):
                        fact = result.get("fact", {})
                        await message.reply(
                            f"🧠 Souvenir enregistré (`{fact.get('id', '?')}`) : "
                            f"{fact.get('text', memory_text)}"
                        )
                        return

            detected = detect_natural_reminder_full(prompt)
            if detected:
                remind_at, reminder_message, recurrence = detected
                reminder = add_reminder(
                    platform="discord",
                    chat_id=message.channel.id,
                    target_id=message.author.id,
                    message=reminder_message,
                    remind_at=remind_at,
                    target_name=str(message.author),
                    recurrence=recurrence,
                )
                logger.info(
                    "Rappel #%s créé (discord -> %s) : %s",
                    reminder["id"],
                    message.channel.id,
                    reminder_message,
                )
                await message.reply(format_reminder_confirmation(reminder))
                return

            session_key = f"discord:{message.author.id}"
            res = await forward_to_runner(
                prompt,
                continue_session=True,
                image_base64=img_b64,
                image_filename=img_name,
                session_key=session_key,
            )
            if res.get("success"):
                out = res.get("output", "").strip() or "✅ Tâche terminée."
            else:
                out = f"❌ Erreur : {res.get('error', 'Le serveur maison n\'a pas répondu.')}"

            if transcript_prefix:
                out = transcript_prefix + out

            processed_text, rendered_images = await extract_and_render_latex(out, dark_mode=True)
            code_snippets = extract_downloadable_code(out, min_chars=700)

            discord_files: list[discord.File] = []
            for img in rendered_images:
                discord_files.append(discord.File(io.BytesIO(img["bytes"]), filename=img["filename"]))
            for code in code_snippets:
                discord_files.append(discord.File(io.BytesIO(code["bytes"]), filename=code["filename"]))

            parts = smart_split_discord(processed_text, max_chars=1950)
            first = True
            for p in parts:
                if first:
                    first = False
                    await message.reply(p, files=discord_files)
                else:
                    await message.channel.send(p)


async def _send_discord_chunks(channel, text: str) -> None:
    """Envoie un texte en respectant la limite de 2000 caractères de Discord."""
    if len(text) > 1950:
        for i in range(0, len(text), 1900):
            await channel.send(text[i:i + 1900])
    else:
        await channel.send(text)


class ReminderActionView(discord.ui.View):
    """Boutons de report/clôture attachés à un rappel Discord."""

    def __init__(self, reminder_id: int) -> None:
        super().__init__(timeout=None)
        self.reminder_id = reminder_id
        for label, minutes in (("⏰ +10 min", 10), ("⏰ +1 h", 60)):
            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                custom_id=f"reminder:snooze:{minutes}:{reminder_id}",
            )
            button.callback = self._make_snooze_callback(minutes)
            self.add_item(button)
        done = discord.ui.Button(
            label="✅ Terminé",
            style=discord.ButtonStyle.success,
            custom_id=f"reminder:done:{reminder_id}",
        )
        done.callback = self._done_callback
        self.add_item(done)

    def _make_snooze_callback(self, minutes: int):
        async def callback(interaction: discord.Interaction) -> None:
            reminder = snooze_reminder(
                self.reminder_id, f"{minutes}m", chat_id=interaction.channel_id
            )
            if reminder is None:
                await interaction.response.send_message(
                    f"❌ Rappel #{self.reminder_id} introuvable ou déjà traité.",
                    ephemeral=True,
                )
                return
            await interaction.response.edit_message(
                content=(
                    f"⏰ Rappel #{self.reminder_id} reporté.\n"
                    f"{format_reminder_confirmation(reminder)}"
                ),
                view=None,
            )

        return callback

    async def _done_callback(self, interaction: discord.Interaction) -> None:
        reminder = complete_reminder(
            self.reminder_id, chat_id=interaction.channel_id
        )
        if reminder is None:
            await interaction.response.send_message(
                f"❌ Rappel #{self.reminder_id} introuvable ou déjà traité.",
                ephemeral=True,
            )
            return
        if reminder.get("status") == "pending" and reminder.get("completed_occurrence"):
            content = (
                f"✅ Occurrence terminée.\n"
                f"{format_reminder_confirmation(reminder)}"
            )
        else:
            content = f"✅ Rappel #{self.reminder_id} terminé."
        await interaction.response.edit_message(content=content, view=None)


async def _send_discord_reminder_impl(
    channel_id: int, reminder: dict
) -> None:
    """Envoie un rappel Discord avec ses boutons d'action."""
    bot = _get_bot()
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    await channel.send(
        format_reminder_notification(reminder),
        view=ReminderActionView(int(reminder["id"])),
    )


async def send_discord_reminder(channel_id: int, reminder: dict) -> None:
    """Envoie un rappel Discord dans la boucle du bot."""
    loop = _DISCORD_LOOP
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if loop is not None and loop.is_running() and loop is not current_loop:
        future = asyncio.run_coroutine_threadsafe(
            _send_discord_reminder_impl(channel_id, reminder), loop
        )
        await asyncio.wrap_future(future)
        return

    await _send_discord_reminder_impl(channel_id, reminder)


async def _send_discord_message_impl(channel_id: int, text: str) -> None:
    """Envoie un message dans un salon Discord précis, dans la boucle du bot."""
    bot = _get_bot()
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    await _send_discord_chunks(channel, text)


async def send_discord_message_to_channel(channel_id: int, text: str) -> None:
    """Envoie un message Discord en respectant la boucle asyncio du bot."""
    loop = _DISCORD_LOOP
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if loop is not None and loop.is_running() and loop is not current_loop:
        future = asyncio.run_coroutine_threadsafe(
            _send_discord_message_impl(channel_id, text), loop
        )
        await asyncio.wrap_future(future)
        return

    await _send_discord_message_impl(channel_id, text)


async def _broadcast_discord_message_impl(text: str) -> bool:
    """Implémentation de la diffusion, exécutée dans la boucle du bot Discord."""
    bot = _get_bot()

    for channel_id in get_saved_discord_channel_ids():
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception:
                continue
        try:
            if channel.permissions_for(channel.guild.me).send_messages:
                await _send_discord_chunks(channel, text)
                return True
        except Exception as exc:
            logger.warning(
                "Erreur diffusion Discord sur le salon %s: %s", channel_id, exc
            )

    # Repli : recherche par nom de salon.
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if (
                channel.name in ("général", "general", "bot", "assistant")
                and channel.permissions_for(guild.me).send_messages
            ):
                try:
                    await _send_discord_chunks(channel, text)
                    return True
                except Exception as exc:
                    logger.warning(
                        "Erreur diffusion Discord sur %s: %s", channel.name, exc
                    )
                break
    return False


async def broadcast_discord_message(text: str) -> bool:
    """Diffuse un message en respectant la boucle asyncio du bot Discord."""
    loop = _DISCORD_LOOP
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if loop is not None and loop.is_running() and loop is not current_loop:
        future = asyncio.run_coroutine_threadsafe(
            _broadcast_discord_message_impl(text), loop
        )
        return await asyncio.wrap_future(future)

    return await _broadcast_discord_message_impl(text)


# ── Lancement en arrière-plan ─────────────────────────────────────────────────



_discord_stop_event = threading.Event()


def start_discord_bot() -> None:
    """Lance le bot Discord dans un thread daemon séparé avec reconnexion automatique.

    Le bot s'exécute dans sa propre boucle asyncio, indépendamment de FastAPI.
    Cette fonction est non-bloquante: elle démarre le thread et retourne immédiatement.
    """
    if not DISCORD_TOKEN:
        logger.warning(
            "DISCORD_TOKEN non défini — le bot Discord ne sera pas démarré."
        )
        return

    _discord_stop_event.clear()

    def _run() -> None:
        while not _discord_stop_event.is_set():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            bot = _get_bot()
            try:
                logger.info("Démarrage du bot Discord...")
                loop.run_until_complete(bot.start(DISCORD_TOKEN))
            except Exception as exc:
                if _discord_stop_event.is_set():
                    break
                logger.error("Le bot Discord s'est arrêté avec une erreur: %s", exc)
                time.sleep(5)
            else:
                if _discord_stop_event.is_set():
                    break
                logger.warning("Connexion Discord terminée, tentative de reconnexion dans 5s...")
                time.sleep(5)
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

    thread = threading.Thread(target=_run, name="discord-bot", daemon=True)
    thread.start()
    logger.info("Thread du bot Discord lancé (daemon=True).")


async def stop_discord_bot() -> None:
    """Ferme proprement la connexion du bot Discord."""
    _discord_stop_event.set()
    bot = _get_bot()
    if bot.is_closed():
        return

    loop = _DISCORD_LOOP
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    closed_cleanly = False
    try:
        if loop is not None and loop.is_running() and loop is not current_loop:
            future = asyncio.run_coroutine_threadsafe(bot.close(), loop)
            try:
                # shield évite que l'expiration du timeout annule le future
                # concurrent alors que la boucle Discord va se fermer.
                wrapped = asyncio.wrap_future(future)
                await asyncio.wait_for(asyncio.shield(wrapped), timeout=5.0)
                closed_cleanly = True
            except asyncio.TimeoutError:
                logger.warning(
                    "Arrêt Discord : fermeture non terminée en 5s, on continue."
                )
        else:
            await asyncio.wait_for(bot.close(), timeout=5.0)
            closed_cleanly = True

        if closed_cleanly:
            logger.info("Bot Discord arrêté proprement.")
    except asyncio.TimeoutError:
        logger.warning("Arrêt Discord : délai de fermeture dépassé.")
    except Exception as exc:
        logger.warning("Erreur lors de l'arrêt du bot Discord: %s", exc)
