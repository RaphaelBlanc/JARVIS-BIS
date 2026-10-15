"""
bots/discord_bot.py — Bot Discord avec commandes slash et préfixées.

Lance un bot Discord en arrière-plan dans un thread daemon,
qui transmet les questions au home-runner via `gateway.forward_to_runner()`.
"""

import asyncio
import logging
import os
import threading
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from gateway import forward_to_runner

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


# ── Helpers ───────────────────────────────────────────────────────────────────


def _is_allowed(user_id: int) -> bool:
    """Vérifie si un utilisateur est autorisé à utiliser le bot."""
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


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

    async def setup_hook(self) -> None:
        """Enregistre les commandes slash (globalement ou par guild)."""
        self.tree.add_command(ask_slash)

        if self._guild_obj:
            # Synchronisation immédiate sur le serveur de dev
            self.tree.copy_global_to(guild=self._guild_obj)
            await self.tree.sync(guild=self._guild_obj)
            logger.info("Commandes slash synchronisées pour la guild %d.", DISCORD_GUILD_ID)
        else:
            await self.tree.sync()
            logger.info("Commandes slash synchronisées globalement.")

    async def on_ready(self) -> None:
        logger.info("Bot Discord connecté en tant que %s (ID: %d).", self.user, self.user.id)

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


# ── Commande slash /ask ───────────────────────────────────────────────────────


@app_commands.command(name="ask", description="Pose une question à l'assistant personnel")
@app_commands.describe(question="Votre question ou tâche pour l'assistant")
async def ask_slash(interaction: discord.Interaction, question: str) -> None:
    """Handler de la commande slash /ask."""
    if not _is_allowed(interaction.user.id):
        logger.warning("Accès refusé pour l'utilisateur Discord %d.", interaction.user.id)
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return

    logger.info("Commande /ask de %d: '%s'", interaction.user.id, question[:80])

    await interaction.response.send_message("⏳ Traitement en cours...")
    result = await forward_to_runner(question)

    if result.get("success"):
        response_text = result.get("output", "").strip() or "✅ Tâche terminée (pas de sortie)."
    else:
        error = result.get("error", "Erreur inconnue")
        response_text = f"❌ Erreur : {error}"
        logger.error("Erreur runner (slash /ask): %s", error)

    await interaction.edit_original_response(content=response_text)


# ── Commandes préfixées ───────────────────────────────────────────────────────


def _register_prefix_commands(bot: AssistantBot) -> None:
    """Enregistre les commandes préfixées (!) sur l'instance bot."""

    @bot.command(name="ask", help="Pose une question à l'assistant. Ex: !ask Quelle heure est-il ?")
    async def ask_prefix(ctx: commands.Context, *, question: str) -> None:
        """Handler de la commande préfixée !ask."""
        if not _is_allowed(ctx.author.id):
            logger.warning("Accès refusé pour l'utilisateur Discord %d.", ctx.author.id)
            await ctx.send("⛔ Accès non autorisé.")
            return

        logger.info("Commande !ask de %d: '%s'", ctx.author.id, question[:80])

        waiting_msg = await ctx.send("⏳ Traitement en cours...")
        result = await forward_to_runner(question)

        if result.get("success"):
            response_text = result.get("output", "").strip() or "✅ Tâche terminée (pas de sortie)."
        else:
            error = result.get("error", "Erreur inconnue")
            response_text = f"❌ Erreur : {error}"
            logger.error("Erreur runner (!ask): %s", error)

        await waiting_msg.edit(content=response_text)


# ── Lancement en arrière-plan ─────────────────────────────────────────────────


def start_discord_bot() -> None:
    """Lance le bot Discord dans un thread daemon séparé.

    Le bot s'exécute dans sa propre boucle asyncio, indépendamment de FastAPI.
    Cette fonction est non-bloquante: elle démarre le thread et retourne immédiatement.
    """
    if not DISCORD_TOKEN:
        logger.warning(
            "DISCORD_TOKEN non défini — le bot Discord ne sera pas démarré."
        )
        return

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        bot = _get_bot()
        try:
            logger.info("Démarrage du bot Discord...")
            loop.run_until_complete(bot.start(DISCORD_TOKEN))
        except Exception as exc:
            logger.error("Le bot Discord s'est arrêté avec une erreur: %s", exc, exc_info=True)
        finally:
            loop.close()

    thread = threading.Thread(target=_run, name="discord-bot", daemon=True)
    thread.start()
    logger.info("Thread du bot Discord lancé (daemon=True).")


async def stop_discord_bot() -> None:
    """Ferme proprement la connexion du bot Discord."""
    bot = _get_bot()
    if not bot.is_closed():
        await bot.close()
        logger.info("Bot Discord arrêté proprement.")
