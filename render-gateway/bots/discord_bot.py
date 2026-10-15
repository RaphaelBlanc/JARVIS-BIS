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
        """Enregistre l'ensemble des commandes slash avec autocomplétion."""
        for cmd in [
            briefing_slash,
            ask_slash,
            resume_slash,
            delete_slash,
            agenda_slash,
            tasks_slash,
            meteo_slash,
            search_slash,
            summarize_slash,
            status_slash,
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

        logger.info("Commandes slash enregistrées dans le command tree.")

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


BRIEFING_PROMPT = (
    "Effectue mon briefing matinal complet, motivant et structuré :\n"
    "1. 📅 EMPLOI DU TEMPS DU JOUR (Heure / Cours / Salles) :\n"
    "Consulte mon Google Agenda pour aujourd'hui (en vérifiant bien tous mes calendriers, y compris mon calendrier de cours EPITA/PREPA). "
    "Présente mon planning de la journée sous forme de liste claire avec les horaires, l'intitulé du cours et la salle exacte :\n"
    "• Heure début - Heure fin : [Matière / Intitulé] (📍 Salle [numéro])\n"
    "S'il n'y a aucun cours aujourd'hui, mentionne-le clairement.\n\n"
    "2. 📈 ACTUALITÉS ÉCONOMIQUES DE LA VEILLE :\n"
    "Recherche les actualités économiques et financières marquantes de la veille (marchés boursiers, macroéconomie, tech & business) et résume les 2 ou 3 points essentiels.\n\n"
    "3. ✉️ E-MAILS IMPORTANTS :\n"
    "Consulte mes e-mails récents reçus depuis hier via Gmail. "
    "IMPORTANT : Filtre et ignore TOTALEMENT toutes les publicités, spams, newsletters commerciales et promotions. "
    "Ne retiens que les messages importants (personnels, professionnels, école/EPITA, cours/examens, démarches). "
    "Pour chaque e-mail important retenu, résume en 1 ou 2 phrases l'essentiel et l'action requise.\n\n"
    "4. 🌤️ MÉTÉO DU JOUR :\n"
    "Consulte la météo du jour pour ma ville (température, ciel, ressenti, pluie éventuelle).\n\n"
    "5. 📋 TÂCHES EN COURS :\n"
    "Consulte mes tâches Google Tasks en cours non terminées si disponibles.\n\n"
    "Présente le résultat avec un ton motivant, structuré et dynamique avec des émojis."
)


@app_commands.command(name="briefing", description="Génère votre briefing matinal (e-mails triés, agenda, météo)")
async def briefing_slash(interaction: discord.Interaction) -> None:
    """Handler de la commande slash /briefing."""
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return

    logger.info("Commande /briefing de %d", interaction.user.id)
    await interaction.response.send_message("⏳ Préparation de votre briefing matinal (tri des e-mails, agenda, météo)...")
    result = await forward_to_runner(BRIEFING_PROMPT, continue_session=False, timeout=120)

    if result.get("success"):
        response_text = result.get("output", "").strip() or "✅ Briefing terminé."
    else:
        error = result.get("error", "Erreur inconnue")
        response_text = f"❌ Erreur : {error}"

    # Si la réponse dépasse 2000 caractères, on la tronque proprement pour Discord
    if len(response_text) > 2000:
        response_text = response_text[:1990] + "..."

    await interaction.edit_original_response(content=response_text)


@app_commands.command(name="ask", description="Pose une question à l'assistant personnel")
@app_commands.describe(question="Votre question ou tâche pour l'assistant")
async def ask_slash(interaction: discord.Interaction, question: str) -> None:
    """Handler de la commande slash /ask."""
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return


    logger.info("Commande /ask de %d: '%s'", interaction.user.id, question[:80])
    await interaction.response.send_message("⏳ Traitement en cours...")
    result = await forward_to_runner(question, continue_session=True)

    if result.get("success"):
        response_text = result.get("output", "").strip() or "✅ Tâche terminée (pas de sortie)."
    else:
        error = result.get("error", "Erreur inconnue")
        response_text = f"❌ Erreur : {error}"

    await interaction.edit_original_response(content=response_text)


async def send_discord_long_response(interaction: discord.Interaction, text: str) -> None:
    """Envoie une réponse découpée si elle dépasse 2000 caractères."""
    if not text:
        text = "✅ Tâche terminée (pas de sortie)."
    if len(text) <= 2000:
        await interaction.followup.send(text)
        return
    parts = [text[i:i + 1950] for i in range(0, len(text), 1950)]
    for p in parts:
        await interaction.followup.send(p)


async def clear_channel_messages(channel: Optional[discord.abc.Messageable], limit: int = 100) -> None:
    """Efface les messages précédents dans le salon."""
    if not channel:
        return
    try:
        if hasattr(channel, "purge"):
            await channel.purge(limit=limit)
            return
    except discord.Forbidden:
        logger.warning("Permissions insuffisantes pour purger le salon, tentative message par message.")
    except Exception as exc:
        logger.warning("Erreur lors du purge du salon : %s", exc)

    # Repli : suppression des messages accessibles
    try:
        async for msg in channel.history(limit=limit):
            try:
                await msg.delete()
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

    await interaction.response.defer(ephemeral=True)

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

    await interaction.response.defer(ephemeral=True)

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
    await interaction.response.defer()
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

    await interaction.response.defer()
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
    await interaction.response.defer()
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
    await interaction.response.defer()
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
    await interaction.response.defer()
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
    await interaction.response.defer()
    health = await fetch_health()
    session = await fetch_session()

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


@app_commands.command(name="compact", description="Compacter la mémoire de la conversation")
async def compact_slash(interaction: discord.Interaction) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    await interaction.response.defer()
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
    await interaction.response.defer()
    res = await update_session_remote(model=model.value)
    if "error" in res:
        await interaction.followup.send(f"❌ Erreur : {res['error']}")
    else:
        await interaction.followup.send(f"✅ Modèle changé sur : `{model.value}` ({model.name})")


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
    await interaction.response.defer()
    await update_session_remote(effort=level.value)
    await interaction.followup.send(f"✅ Niveau d'effort réglé sur : `{level.value}`")


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
    await interaction.response.defer()
    await update_session_remote(mode=mode.value)
    await interaction.followup.send(f"✅ Mode d'exécution réglé sur : `{mode.value}`")


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
    await interaction.response.defer()
    await update_session_remote(permission_mode=policy.value)
    await interaction.followup.send(f"✅ Politique de permissions : `{policy.value}`")


@app_commands.command(name="new", description="Démarre une nouvelle conversation vierge (vide le salon)")
async def new_slash(interaction: discord.Interaction) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await reset_session_remote()
    if interaction.channel:
        await clear_channel_messages(interaction.channel)
    await interaction.followup.send("✨ **Nouvelle conversation prête.** Le salon a été vidé.", ephemeral=True)


@app_commands.command(name="models", description="Liste les modèles Antigravity disponibles")
async def models_slash(interaction: discord.Interaction) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    await interaction.response.defer()
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
    await interaction.followup.send("\n".join(lines))


@app_commands.command(name="session", description="Affiche les réglages de la session active")
async def session_slash(interaction: discord.Interaction) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    await interaction.response.defer()
    s = await fetch_session()
    text = (
        "⚙️ **Configuration de la session :**\n"
        f"• **Modèle :** `{s.get('model', 'défaut')}`\n"
        f"• **Effort :** `{s.get('effort') or 'par défaut'}`\n"
        f"• **Mode :** `{s.get('mode') or 'auto'}`\n"
        f"• **Permissions :** `{s.get('permission_mode', 'always-proceed')}`\n"
        f"• **Conversation active :** `{'Oui' if s.get('has_active_session') else 'Non (vierge)'}`"
    )
    await interaction.followup.send(text)


@app_commands.command(name="usage", description="Consulte vos quotas et jetons consommés")
async def usage_slash(interaction: discord.Interaction) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    await interaction.response.defer()
    res = await forward_to_runner("/usage", continue_session=False)
    await interaction.followup.send(res.get("output", "Aucune information reçue."))


@app_commands.command(name="skills", description="Affiche les compétences actives")
async def skills_slash(interaction: discord.Interaction) -> None:
    if not _is_allowed(interaction.user.id):
        await interaction.response.send_message("⛔ Accès non autorisé.", ephemeral=True)
        return
    await interaction.response.defer()
    res = await forward_to_runner("/skills", continue_session=False)
    await interaction.followup.send(res.get("output", "Aucune compétence trouvée."))


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

    @bot.command(name="briefing", aliases=["brief"], help="Génère votre briefing matinal (mails triés, agenda, météo)")
    async def briefing_cmd(ctx: commands.Context) -> None:
        if not _is_allowed(ctx.author.id):
            return
        waiting = await ctx.send("⏳ Préparation de votre briefing matinal (tri des e-mails, agenda, météo)...")
        res = await forward_to_runner(BRIEFING_PROMPT, continue_session=False, timeout=120)
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
        health = await fetch_health()
        session = await fetch_session()

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

    @bot.command(name="compact", help="Compacter la mémoire de contexte")
    async def compact_cmd(ctx: commands.Context) -> None:
        if not _is_allowed(ctx.author.id):
            return
        waiting = await ctx.send("🧹 Compactage de la mémoire...")
        res = await forward_to_runner("/compact", continue_session=True, timeout=60)
        out = res.get("output", "").strip() or "✅ Mémoire compactée."
        await waiting.edit(content=out)


async def broadcast_discord_message(text: str) -> None:
    """Diffuse un message (ex: briefing automatique) sur le salon principal de chaque serveur."""
    bot = _get_bot()
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name in ("général", "general", "bot", "assistant") and channel.permissions_for(guild.me).send_messages:
                try:
                    if len(text) > 1950:
                        parts = [text[i:i + 1900] for i in range(0, len(text), 1900)]
                        for p in parts:
                            await channel.send(p)
                    else:
                        await channel.send(text)
                except Exception as exc:
                    logger.warning("Erreur diffusion Discord sur %s: %s", channel.name, exc)
                break


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
