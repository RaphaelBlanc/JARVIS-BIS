"""Recurring tasks and executable natural language cron scheduler for JARVIS.

Allows user to set automated periodic actions and routines in natural language:
- "Tous les lundis à 8h briefing"
- "Chaque jour à 22h statut du PC"
- "Tous les dimanches à 20h bilan de mes projets"
- "Chaque 1er du mois à 9h bilan budget"

Upgrades:
- Catch-up on wake: If the laptop was asleep at the exact scheduled minute and wakes up later the same day,
  the task is automatically triggered upon wake-up instead of being skipped!
- Executable actions: Distinguishes native fast commands (pc, git, budget, proj, stats, radar, inbox)
  from full AI pipeline tasks.
"""

from datetime import datetime
import html
import json
import logging
from pathlib import Path
import re
import uuid

from core.config import DATA_DIR
TASKS_FILE = DATA_DIR / "recurring_tasks.json"

DAYS_MAP = {
    "lundi": 0, "lun": 0,
    "mardi": 1, "mar": 1,
    "mercredi": 2, "mer": 2,
    "jeudi": 3, "jeu": 3,
    "vendredi": 4, "ven": 4,
    "samedi": 5, "sam": 5,
    "dimanche": 6, "dim": 6,
}


def load_recurring_tasks() -> list[dict]:
    if not TASKS_FILE.exists():
        return []
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"Failed to read recurring tasks: {e}")
        return []


def save_recurring_tasks(tasks: list[dict]) -> bool:
    try:
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(tasks, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logging.error(f"Failed to save recurring tasks: {e}")
        return False


def classify_cron_action(text: str) -> str:
    """Determines what executable action a recurring task should run."""
    t = text.lower().strip()
    if any(k in t for k in ("statut du pc", "statut pc", "bilan pc", "état du pc", "diagnostic pc", "/pc", "santé pc")):
        return "cmd:pc"
    if any(k in t for k in ("git", "dépôts", "depots", "commits")):
        return "cmd:git"
    if any(k in t for k in ("budget", "dépenses", "depenses", "finances")):
        return "cmd:budget"
    if any(k in t for k in ("projet", "projets", "roadmap", "reconforge")):
        return "cmd:proj"
    if any(k in t for k in ("veille", "radar", "actus cyber", "actus tech")):
        return "cmd:radar"
    if any(k in t for k in ("stats", "productivité", "temps de travail", "focus")):
        return "cmd:stats"
    if any(k in t for k in ("inbox", "liens à lire")):
        return "cmd:inbox"
    if any(k in t for k in ("briefing", "planning", "agenda", "tâches", "mails", "préparer ma semaine", "bilan")):
        return "ai_pipeline"
    if t.startswith("ai:") or t.startswith("agent:"):
        return "ai_pipeline"
    return "notify_or_ai"


def parse_recurring_task_input(text: str) -> dict | None:
    """Parses natural French expressions for recurring schedules."""
    clean = text.strip()
    clean = re.sub(r"^/cron(?:@[a-zA-Z0-9_]+)?\s*", "", clean, flags=re.IGNORECASE)

    # Pattern 1: Weekly (tous les <jour> à <HH>[:<MM>])
    m_week = re.match(
        r"^(?:tous\s+les|chaque)\s+(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|lun|mar|mer|jeu|ven|sam|dim)s?\s+(?:[àa]\s+)?(\d{1,2})(?:h(\d{1,2})?|:(\d{1,2}))?\s*(?:min)?\s+(.+)$",
        clean,
        re.IGNORECASE,
    )
    if m_week:
        day_str = m_week.group(1).lower()
        day_idx = DAYS_MAP.get(day_str, 0)
        hour = int(m_week.group(2))
        minute = int(m_week.group(3) or m_week.group(4) or 0)
        action_text = m_week.group(5).strip()
        if 0 <= hour <= 23 and 0 <= minute <= 59 and action_text:
            return {
                "id": uuid.uuid4().hex[:8],
                "type": "weekly",
                "day_of_week": day_idx,
                "day_name": day_str.capitalize(),
                "hour": hour,
                "minute": minute,
                "text": action_text,
                "action_mode": classify_cron_action(action_text),
                "last_run": "",
                "created_at": datetime.now().isoformat(),
            }

    # Pattern 2: Daily (tous les jours / chaque jour / chaque matin / chaque soir à <HH>[:<MM>])
    m_daily = re.match(
        r"^(?:tous\s+les\s+jours|chaque\s+(?:jour|matin|soir))\s+(?:[àa]\s+)?(\d{1,2})(?:h(\d{1,2})?|:(\d{1,2}))?\s*(?:min)?\s+(.+)$",
        clean,
        re.IGNORECASE,
    )
    if m_daily:
        hour = int(m_daily.group(1))
        minute = int(m_daily.group(2) or m_daily.group(3) or 0)
        action_text = m_daily.group(4).strip()
        if 0 <= hour <= 23 and 0 <= minute <= 59 and action_text:
            return {
                "id": uuid.uuid4().hex[:8],
                "type": "daily",
                "hour": hour,
                "minute": minute,
                "text": action_text,
                "action_mode": classify_cron_action(action_text),
                "last_run": "",
                "created_at": datetime.now().isoformat(),
            }

    # Pattern 3: Monthly (chaque <1|1er|15> du mois à <HH>[:<MM>])
    m_month = re.match(
        r"^chaque\s+(\d{1,2})(?:er)?\s+du\s+mois\s+(?:[àa]\s+)?(\d{1,2})(?:h(\d{1,2})?|:(\d{1,2}))?\s*(?:min)?\s+(.+)$",
        clean,
        re.IGNORECASE,
    )
    if m_month:
        day_of_month = int(m_month.group(1))
        hour = int(m_month.group(2))
        minute = int(m_month.group(3) or m_month.group(4) or 0)
        action_text = m_month.group(5).strip()
        if 1 <= day_of_month <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59 and action_text:
            return {
                "id": uuid.uuid4().hex[:8],
                "type": "monthly",
                "day_of_month": day_of_month,
                "hour": hour,
                "minute": minute,
                "text": action_text,
                "action_mode": classify_cron_action(action_text),
                "last_run": "",
                "created_at": datetime.now().isoformat(),
            }

    return None


def add_recurring_task(task_dict: dict) -> dict:
    tasks = load_recurring_tasks()
    tasks.append(task_dict)
    save_recurring_tasks(tasks)
    return task_dict


def delete_recurring_task(task_id: str) -> bool:
    tasks = load_recurring_tasks()
    orig = len(tasks)
    tasks = [t for t in tasks if t.get("id") != task_id]
    if len(tasks) < orig:
        save_recurring_tasks(tasks)
        return True
    return False


def check_due_recurring_tasks(now: datetime) -> list[dict]:
    """Evaluates tasks due for execution at or after their scheduled time today (with wake catch-up).

    Note: Does NOT mark the task as completed on disk until `mark_recurring_task_executed(task_id, now)`
    is called after successful network delivery!
    """
    tasks = load_recurring_tasks()
    due = []
    today_prefix = now.strftime("%Y-%m-%d")
    now_mins = now.hour * 60 + now.minute

    for t in tasks:
        last_run = str(t.get("last_run", ""))
        # Already executed today
        if last_run.startswith(today_prefix):
            continue

        sched_mins = int(t.get("hour", 0)) * 60 + int(t.get("minute", 0))
        # Trigger if current time has reached or passed the scheduled minute today
        if now_mins < sched_mins:
            continue

        is_due = False
        t_type = t.get("type")
        if t_type == "daily":
            is_due = True
        elif t_type == "weekly" and t.get("day_of_week") == now.weekday():
            is_due = True
        elif t_type == "monthly" and t.get("day_of_month") == now.day:
            is_due = True

        if is_due:
            if "action_mode" not in t:
                t["action_mode"] = classify_cron_action(t.get("text", ""))
            due.append(t)

    return due


def mark_recurring_task_executed(task_id: str, now: datetime | None = None) -> None:
    """Marks a recurring task as executed for today after successful delivery."""
    dt = now or datetime.now()
    minute_key = dt.strftime("%Y-%m-%d %H:%M")
    tasks = load_recurring_tasks()
    for t in tasks:
        if t.get("id") == task_id:
            t["last_run"] = minute_key
    save_recurring_tasks(tasks)


def format_recurring_tasks_summary() -> str:
    tasks = load_recurring_tasks()
    if not tasks:
        return (
            "⏰ <b>Routines & Automatisations Récurrentes</b>\n\n"
            "<i>Aucune routine configurée.</i>\n\n"
            "💡 <b>Exemples d'automatisations exécutables :</b>\n"
            "• <code>Tous les jours à 22h statut du PC</code>\n"
            "• <code>Tous les lundis à 9h veille cyber</code>\n"
            "• <code>Tous les dimanches à 20h bilan de mes projets</code>\n"
            "• <code>Chaque 1er du mois à 9h bilan budget</code>"
        )

    lines = [f"⏰ <b>Routines Automatisées Actives ({len(tasks)})</b>\n"]
    for t in tasks:
        time_str = f"{t.get('hour', 0):02d}h{t.get('minute', 0):02d}"
        if t.get("type") == "daily":
            sched = f"Tous les jours à {time_str}"
        elif t.get("type") == "weekly":
            sched = f"Chaque {t.get('day_name')} à {time_str}"
        elif t.get("type") == "monthly":
            sched = f"Chaque {t.get('day_of_month')} du mois à {time_str}"
        else:
            sched = f"À {time_str}"

        mode = t.get("action_mode") or classify_cron_action(t.get("text", ""))
        mode_badge = "⚡ Action directe" if mode.startswith("cmd:") else "🤖 Exécution Agent"
        lines.append(
            f"• <b>{sched}</b> : {html.escape(t.get('text', ''))} "
            f"<i>({mode_badge} • ID: <code>{t.get('id')}</code>)</i>"
        )

    lines.append("\n💡 <i>Rattrapage intelligent actif : si le PC est en veille à l'heure prévue, la routine s'exécute automatiquement au réveil.</i>")
    lines.append("<i>Pour supprimer :</i> <code>/cron del &lt;ID&gt;</code>")
    return "\n".join(lines)
