"""Deep Work / Pomodoro Focus Manager & Productivity Analytics for JARVIS.

Provides:
- Active Focus / Pomodoro timer with project tagging (/focus 45m ReconForge)
- Native Hyprland desktop notifications (notify-send) & audio completion cues (spd-say)
- Do-Not-Disturb (DND) flag during focus sessions to suppress non-urgent alerts
- Persistent session logging in data/focus_sessions.json
- Executive productivity statistics (/stats) across projects, focus hours, ideas, and inbox
"""

import asyncio
from datetime import datetime, timedelta
import html
import json
import logging
from pathlib import Path
import re
import shutil
import time
import uuid

from core.config import DATA_DIR
FOCUS_FILE = DATA_DIR / "focus_sessions.json"

# Active focus state in memory
ACTIVE_FOCUS: dict = {
    "active": False,
    "id": None,
    "topic": "",
    "project": "Général",
    "duration_min": 0,
    "start_ts": 0.0,
    "end_ts": 0.0,
    "chat_id": 0,
    "user_id": 0,
    "task": None,
}


def load_focus_sessions() -> list[dict]:
    if not FOCUS_FILE.exists():
        return []
    try:
        with open(FOCUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"Failed to read focus sessions: {e}")
        return []


def save_focus_sessions(sessions: list[dict]) -> bool:
    try:
        with open(FOCUS_FILE, "w", encoding="utf-8") as f:
            json.dump(sessions, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logging.error(f"Failed to save focus sessions: {e}")
        return False


def is_focus_dnd_active() -> bool:
    """Returns True if a Deep Work session is currently active."""
    if not ACTIVE_FOCUS.get("active"):
        return False
    if time.time() >= ACTIVE_FOCUS.get("end_ts", 0.0):
        return False
    return True


async def send_desktop_notification(title: str, body: str, urgency: str = "normal") -> None:
    """Sends a native Wayland/Hyprland desktop notification via notify-send."""
    notify_bin = shutil.which("notify-send")
    if not notify_bin:
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            notify_bin, "-u", urgency, "-a", "JARVIS", title, body,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=3.0)
    except Exception:
        pass


def parse_focus_input(text: str) -> dict:
    """Parses focus command or natural language request.

    Examples:
    - "45m ReconForge"
    - "25 codage parser"
    - "1h architecture JARVIS"
    - "ReconForge pendant 50 minutes"
    - "stop" / "off" / "status"
    """
    clean = re.sub(r"^/(?:focus|pomodoro|pomo)(?:@[a-zA-Z0-9_]+)?\s*", "", text.strip(), flags=re.IGNORECASE).strip()
    if not clean:
        return {"action": "status"}

    low = clean.lower()
    if low in ("stop", "off", "cancel", "annuler", "arreter", "arrêter", "fin", "done", "terminer"):
        return {"action": "stop"}
    if low in ("status", "statut", "info"):
        return {"action": "status"}

    # Pattern 1: "<duration> [m|min|h] [topic]"
    m1 = re.match(
        r"^(\d+)\s*(h(?:eures?)?|m(?:in(?:utes?)?)?)?(?:\s+(?:sur|pour|de)\s+|\s+)?(.*)$",
        clean,
        re.IGNORECASE,
    )
    if m1:
        val = int(m1.group(1))
        unit = (m1.group(2) or "m").lower()
        topic = (m1.group(3) or "").strip() or "Session Deep Work"
        mins = val * 60 if unit.startswith("h") else val
        mins = max(1, min(mins, 240))
        return {
            "action": "start",
            "duration_min": mins,
            "topic": topic,
            "project": _infer_project_from_topic(topic),
        }

    # Pattern 2: "<topic> pendant <duration> [m|min|h]"
    m2 = re.search(r"(.+?)\s+(?:pendant|durant|de|pour)\s+(\d+)\s*(h(?:eures?)?|m(?:in(?:utes?)?)?)", clean, re.IGNORECASE)
    if m2:
        topic = m2.group(1).strip()
        topic = re.sub(r"^(?:lance\s+un\s+focus|mode\s+focus|concentre-moi|travailler)\s*(?:sur)?\s*", "", topic, flags=re.IGNORECASE).strip() or "Session Deep Work"
        val = int(m2.group(2))
        unit = m2.group(3).lower()
        mins = val * 60 if unit.startswith("h") else val
        mins = max(1, min(mins, 240))
        return {
            "action": "start",
            "duration_min": mins,
            "topic": topic,
            "project": _infer_project_from_topic(topic),
        }

    # Default 25-min Pomodoro with the text as topic
    topic = re.sub(r"^(?:lance\s+un\s+focus|mode\s+focus|pomodoro)\s*(?:sur|pour)?\s*", "", clean, flags=re.IGNORECASE).strip() or "Pomodoro Focus"
    return {
        "action": "start",
        "duration_min": 25,
        "topic": topic,
        "project": _infer_project_from_topic(topic),
    }


def _infer_project_from_topic(topic: str) -> str:
    t = topic.lower()
    if "reconforge" in t or "recon" in t:
        return "ReconForge"
    if "jarvis" in t or "bot" in t:
        return "JARVIS"
    if any(k in t for k in ("cyber", "ctf", "thm", "htb", "pentest", "root-me", "caido", "burp", "sqlmap")):
        return "Cyber & CTF"
    if any(k in t for k in ("portfolio", "cv", "linkedin", "stage", "orange")):
        return "Pro & Portfolio"
    return "Général"


def record_completed_focus(duration_min: int, topic: str, project: str) -> dict:
    """Persists a completed focus session to disk."""
    now = datetime.now()
    entry = {
        "id": uuid.uuid4().hex[:8],
        "topic": topic,
        "project": project,
        "duration_min": max(1, int(duration_min)),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "timestamp": now.timestamp(),
    }
    sessions = load_focus_sessions()
    sessions.append(entry)
    save_focus_sessions(sessions)
    return entry


def format_active_focus_status() -> str:
    """Formats the current active focus session status or instructions."""
    if not is_focus_dnd_active():
        sessions = load_focus_sessions()
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_mins = sum(s.get("duration_min", 0) for s in sessions if s.get("date") == today_str)
        return (
            "🎯 <b>Mode Deep Work & Pomodoro (Hyprland)</b>\n\n"
            "• <b>Statut actuel :</b> ⚪ Aucune session en cours\n"
            f"• <b>Temps focalisé aujourd'hui :</b> <code>{today_mins} min</code>\n\n"
            "💡 <b>Pour démarrer une session :</b>\n"
            "• <code>/focus 45m ReconForge</code>\n"
            "• <code>/focus 25m Audit Cyber</code>\n"
            "• Ou en langage naturel : <i>« Lance un focus de 50 minutes sur ReconForge »</i>"
        )

    rem_secs = max(0, int(ACTIVE_FOCUS["end_ts"] - time.time()))
    rem_mins = rem_secs // 60
    rem_s = rem_secs % 60
    tot_mins = ACTIVE_FOCUS["duration_min"]
    elapsed_pct = min(100, int(((tot_mins * 60 - rem_secs) / max(1, tot_mins * 60)) * 100))
    filled = elapsed_pct // 10
    bar = "█" * filled + "░" * (10 - filled)

    end_str = datetime.fromtimestamp(ACTIVE_FOCUS["end_ts"]).strftime("%H:%M")
    return (
        "🎯 <b>Session Deep Work Active</b> 🟢\n\n"
        f"• <b>Objectif :</b> {html.escape(ACTIVE_FOCUS['topic'])}\n"
        f"• <b>Projet :</b> <code>{html.escape(ACTIVE_FOCUS['project'])}</code>\n"
        f"• <b>Progression :</b> <code>[{bar}] {elapsed_pct}%</code>\n"
        f"• <b>Temps restant :</b> <b>{rem_mins}m {rem_s:02d}s</b> (fin à {end_str})\n"
        "• <b>Mode Ne Pas Déranger :</b> 🔕 Alertes secondaires suspendues"
    )


def get_productivity_stats() -> dict:
    """Generates a comprehensive weekly & daily productivity report across Focus, Ideas, and Inbox."""
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    week_ago_ts = (now - timedelta(days=7)).timestamp()

    sessions = load_focus_sessions()
    today_sessions = [s for s in sessions if s.get("date") == today_str]
    week_sessions = [s for s in sessions if s.get("timestamp", 0) >= week_ago_ts]

    today_mins = sum(s.get("duration_min", 0) for s in today_sessions)
    week_mins = sum(s.get("duration_min", 0) for s in week_sessions)

    proj_breakdown: dict[str, int] = {}
    for s in week_sessions:
        p = s.get("project", "Général")
        proj_breakdown[p] = proj_breakdown.get(p, 0) + s.get("duration_min", 0)

    # Check Ideas & Inbox counts
    ideas_file = DATA_DIR / "ideas.json"
    ideas_count = 0
    if ideas_file.exists():
        try:
            ideas_count = len(json.loads(ideas_file.read_text(encoding="utf-8")))
        except Exception:
            pass

    inbox_file = DATA_DIR / "inbox_links.json"
    unread_links = 0
    if inbox_file.exists():
        try:
            links = json.loads(inbox_file.read_text(encoding="utf-8"))
            unread_links = sum(1 for l in links if not l.get("read"))
        except Exception:
            pass

    h_week = week_mins // 60
    m_week = week_mins % 60

    lines = [
        "📈 <b>Tableau de Bord Productivité & Deep Work</b>\n",
        f"⏱️ <b>Aujourd'hui :</b> <code>{today_mins} min</code> ({len(today_sessions)} sessions)",
        f"🔥 <b>7 derniers jours :</b> <code>{h_week}h {m_week:02d}m</code> ({len(week_sessions)} sessions)\n",
    ]

    if proj_breakdown:
        lines.append("📂 <b>Temps investi par Projet (7j) :</b>")
        for proj, mins in sorted(proj_breakdown.items(), key=lambda x: x[1], reverse=True):
            pct = (mins / week_mins * 100) if week_mins else 0
            lines.append(f"• <b>{html.escape(proj)}</b> : <code>{mins} min</code> ({pct:.0f}%)")
        lines.append("")

    lines.extend([
        "🧠 <b>Écosystème & Innovation :</b>",
        f"• 💡 <b>Idées incubées :</b> {ideas_count}",
        f"• 📥 <b>Liens Inbox à lire :</b> {unread_links}",
    ])

    if week_sessions:
        lines.append("\n🕒 <b>Dernières sessions Focus :</b>")
        for s in sorted(week_sessions, key=lambda x: x.get("timestamp", 0), reverse=True)[:4]:
            lines.append(
                f"• <i>{s.get('date', '')[5:]}</i> — <b>{html.escape(s.get('topic', ''))}</b> "
                f"(<code>{s.get('duration_min')}m</code> • {html.escape(s.get('project', ''))})"
            )

    return {
        "today_mins": today_mins,
        "week_mins": week_mins,
        "projects": proj_breakdown,
        "message": "\n".join(lines),
    }
