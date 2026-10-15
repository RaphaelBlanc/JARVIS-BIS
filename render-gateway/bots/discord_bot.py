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

from gateway import (
    fetch_models,
    fetch_session,
    forward_to_runner,
    reset_session_remote,
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
                logger.info("Serveur Discord rejoint: %s (ID: %d)", guild.name, guild.id)
                channels = [
                    c.name for c in guild.text_channels
                    if c.permissions_for(guild.me).send_messages
                ]
                logger.info("Salons accessibles dans '%s': %s", guild.name, ", ".join(f"#{c}" for c in channels))

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
        result = await forward_to_runner(question, continue_session=True)

        if result.get("success"):
            response_text = result.get("output", "").strip() or "✅ Tâche terminée (pas de sortie)."
        else:
            error = result.get("error", "Erreur inconnue")
            response_text = f"❌ Erreur : {error}"
            logger.error("Erreur runner (!ask): %s", error)

        await waiting_msg.edit(content=response_text)

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

    @bot.command(name="new", help="Démarre une nouvelle conversation vierge")
    async def new_cmd(ctx: commands.Context) -> None:
        if not _is_allowed(ctx.author.id):
            return
        await reset_session_remote()
        await ctx.send("🧹 **Nouvelle conversation démarrée !** L'historique précédent a été effacé.")

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

    @bot.command(name="skills", help="Affiche les compétences actives")
    async def skills_cmd(ctx: commands.Context) -> None:
        if not _is_allowed(ctx.author.id):
            return
        waiting = await ctx.send("⏳ Récupération des compétences...")
        res = await forward_to_runner("/skills", continue_session=False)
        await waiting.edit(content=res.get("output", "Aucune compétence trouvée."))


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
