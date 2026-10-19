"""
core/tools/definitions.py — Définitions et enregistrement de tous les modules de l'assistant.

Chaque outil est modulaire (comme un plugin MCP) avec sa description, ses paramètres,
et la possibilité d'être activé ou désactivé individuellement via .env ou l'installateur.
"""

from typing import List
from core.tools.base import BaseTool, ToolParam, ToolRegistry


class SystemPcTool(BaseTool):
    id = "system_pc"
    name = "Contrôle PC Linux & Wayland"
    description = "Contrôle du bureau (screenshots grim, verrouillage, webcam, son, fenêtres Hyprland, presse-papier, git scan)."
    icon = "💻"
    requires_local_hardware = True
    params = [
        ToolParam("WEBCAM_DEVICE", "Périphérique webcam", default="/dev/video0"),
    ]


class MoodleTool(BaseTool):
    id = "moodle"
    name = "Moodle EPITA"
    description = "Synchronisation automatique des devoirs, TPs et échéances académiques."
    icon = "🎓"
    requires_local_hardware = False
    params = [
        ToolParam("MOODLE_URL", "URL du serveur Moodle", default="https://moodle.epita.fr"),
        ToolParam("MOODLE_API_KEY", "Token WebService Moodle", secret=True),
    ]


class ExpenseTool(BaseTool):
    id = "expenses"
    name = "Finances & Budget Mensuel"
    description = "Suivi des dépenses, budget mensuel, analyse OCR de tickets de caisse via Gemini et intégration Apple Pay."
    icon = "💰"
    requires_local_hardware = False
    params = [
        ToolParam("MONTHLY_BUDGET", "Plafond de budget mensuel (€)", default="500", param_type="float"),
        ToolParam("JARVIS_WEBHOOK_SECRET", "Secret Webhook Apple Pay / Raccourcis", secret=True),
        ToolParam("GEMINI_API_KEY", "Clé Google Gemini pour OCR des tickets", secret=True),
    ]


class ShoppingTool(BaseTool):
    id = "shopping"
    name = "Liste de Courses Interactive"
    description = "Liste de courses dynamique avec boutons interactifs pour cocher/décocher en direct sur Telegram et Discord."
    icon = "🛒"
    requires_local_hardware = False
    params = []


class ObsidianTool(BaseTool):
    id = "obsidian"
    name = "Second Cerveau Obsidian"
    description = "Connexion directe à votre coffre Obsidian Markdown (/kb pour chercher, /note pour capturer classé par catégorie)."
    icon = "🧠"
    requires_local_hardware = False
    params = [
        ToolParam("OBSIDIAN_VAULT_PATH", "Chemin absolu vers le coffre Obsidian", default="~/Documents/Obsidian/Vault", param_type="path"),
    ]


class FocusTool(BaseTool):
    id = "focus"
    name = "Mode Focus & Deep Work"
    description = "Minuteur Pomodoro, statistiques de productivité et synchronisation Ne Pas Déranger (DND)."
    icon = "🎯"
    requires_local_hardware = False
    params = []


class IdeaTool(BaseTool):
    id = "ideas"
    name = "Incubateur d'Idées & Projets"
    description = "Brainstorming guidé, génération de stack technique, plan MVP en 3 étapes et scoring de viabilité."
    icon = "💡"
    requires_local_hardware = False
    params = []


class ProjectTool(BaseTool):
    id = "projects"
    name = "Cockpit Projets & Dev Agent"
    description = "Supervision Git de projets, scaffolding de nouveaux dépôts (/newproj), tests et agent de code autonome (/dev)."
    icon = "📁"
    requires_local_hardware = False
    params = []


class UniversalInboxTool(BaseTool):
    id = "inbox"
    name = "Boîte de Réception Universelle & Radar Tech"
    description = "Capture de liens avec enrichissement GitHub/IA, veille technologique et cyber (/radar)."
    icon = "🌐"
    requires_local_hardware = False
    params = []


class TravelTool(BaseTool):
    id = "travel"
    name = "Conseiller Trajets & Départs"
    description = "Calcul d'heure de départ intelligente avec marge de sécurité pour vos rendez-vous et cours."
    icon = "🗺️"
    requires_local_hardware = False
    params = []


class SecurityPinTool(BaseTool):
    id = "security_pin"
    name = "Sécurité par Code PIN"
    description = "Protection par code PIN à 4 chiffres (pavé virtuel 4x3) pour les commandes sensibles (/run, /kill, etc.)."
    icon = "🔒"
    requires_local_hardware = False
    params = []


class CronTool(BaseTool):
    id = "cron"
    name = "Routines & Tâches Récurrentes"
    description = "Planification en langage naturel de rappels et routines périodiques (quotidiens, hebdomadaires, mensuels)."
    icon = "⏰"
    requires_local_hardware = False
    params = []


class MailSorterTool(BaseTool):
    id = "mail_sorter"
    name = "Tri Intelligent des E-mails"
    description = "Classification en 4 pôles (Urgent, Pédagogie/Travail, À Répondre, Newsletters) sans surcoût token."
    icon = "📧"
    requires_local_hardware = False
    params = []


class GoogleWorkspaceTool(BaseTool):
    id = "google_workspace"
    name = "Google Agenda & Google Tasks"
    description = "Consultation et création d'événements d'agenda et synchronisation de listes de tâches."
    icon = "📅"
    requires_local_hardware = False
    params = []


class WeatherTool(BaseTool):
    id = "weather"
    name = "Météo Locale"
    description = "Intégration météo locale en direct dans le briefing matinal et à la demande."
    icon = "🌤️"
    requires_local_hardware = False
    params = [
        ToolParam("WEATHER_LOCATION", "Ville par défaut pour la météo", default="Paris"),
    ]


# Enregistrement initial
ALL_TOOL_CLASSES = [
    SystemPcTool,
    MoodleTool,
    ExpenseTool,
    ShoppingTool,
    ObsidianTool,
    FocusTool,
    IdeaTool,
    ProjectTool,
    UniversalInboxTool,
    TravelTool,
    SecurityPinTool,
    CronTool,
    MailSorterTool,
    GoogleWorkspaceTool,
    WeatherTool,
]


def register_all_tools() -> None:
    """Instancie et enregistre l'ensemble des modules dans le ToolRegistry."""
    for tool_cls in ALL_TOOL_CLASSES:
        ToolRegistry.register(tool_cls())


# Enregistrement automatique au chargement du module
register_all_tools()
