"""
reminders.py — Gestionnaire de rappels persistants (SQLite) et planificateur asynchrone.

Supporte :
- Stockage SQLite thread-safe avec mode WAL.
- Parsing des durées relatives (ex: "10s", "5m", "15min", "1h", "1h30", "2j").
- Parsing des heures fixes (ex: "14:30", "14h30", "8:00").
- Détection en langage naturel (ex: "Rappelle-moi dans 15 min de couper le feu").
- Notifications asynchrones vers Telegram et Discord.
"""

import datetime
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Fuseau horaire par défaut pour la France
TIMEZONE = ZoneInfo("Europe/Paris")

DB_PATH = Path(__file__).resolve().parent / "reminders.db"


def _get_db() -> sqlite3.Connection:
    """Ouvre une connexion SQLite avec row_factory dict-like et WAL."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """Connexion SQLite transactionnelle, toujours fermée en fin de bloc."""
    conn = _get_db()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Initialise la table des rappels si elle n'existe pas."""
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                target_id TEXT,
                target_name TEXT,
                message TEXT NOT NULL,
                remind_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reminders_status_time ON reminders(status, remind_at);"
        )
    logger.info("Base de données des rappels initialisée à %s", DB_PATH)


def parse_delay_or_time(
    time_str: str,
    now: Optional[datetime.datetime] = None,
    force_relative: bool = False,
) -> Tuple[Optional[datetime.datetime], Optional[str]]:
    """Parse une durée relative ou une heure précise.

    Exemples supportés :
        - Relatif : "30s", "5m", "15min", "45 minutes", "1h", "1h30", "2h15", "1j", "2 jours"
        - Heure fixe : "14:30", "14h30", "08:15", "9h", "20:00"

    ``force_relative`` permet de lever l'ambiguïté de "1h30" : avec
    ``force_relative=True`` c'est une durée (dans 1h30), sinon c'est 01:30.

    Retourne :
        (datetime_cible, None) en cas de succès,
        (None, message_erreur) si non reconnu.
    """
    if now is None:
        now = datetime.datetime.now(TIMEZONE)
    else:
        if now.tzinfo is None:
            now = now.replace(tzinfo=TIMEZONE)

    raw = time_str.strip().lower()
    relative_marker = raw.startswith("dans ")
    fixed_marker = raw.startswith(("à ", "a "))
    s = re.sub(r"^(?:dans|[àa])\s+", "", raw).strip()
    if relative_marker:
        force_relative = True
    elif fixed_marker:
        force_relative = False

    def _parse_fixed() -> Optional[datetime.datetime]:
        pattern = r"^(\d{1,2})(?:[:h](\d{1,2})?|h)?$"
        m = re.match(pattern, s)
        if not m:
            return None
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) is not None else 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        return target

    def _parse_composite_relative() -> Optional[datetime.datetime]:
        m = re.match(
            r"^(\d+)\s*h(?:eures?)?\s*(\d+)\s*(?:m(?:in(?:utes?)?)?)?$",
            s,
        )
        if not m:
            return None
        return now + datetime.timedelta(
            hours=int(m.group(1)), minutes=int(m.group(2))
        )

    def _parse_simple_relative() -> Optional[datetime.datetime]:
        m = re.match(
            r"^(\d+)\s*(s(?:ec(?:onde)?s?)?|m(?:in(?:ute)?s?)?|h(?:eure?s?)?|j(?:ours?)?|d(?:ays?)?)$",
            s,
        )
        if not m:
            return None
        val = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("s"):
            delta = datetime.timedelta(seconds=val)
        elif unit.startswith("m"):
            delta = datetime.timedelta(minutes=val)
        elif unit.startswith("h"):
            delta = datetime.timedelta(hours=val)
        elif unit.startswith("j") or unit.startswith("d"):
            delta = datetime.timedelta(days=val)
        else:
            return None
        if delta.total_seconds() <= 0:
            return None
        return now + delta

    parsers = (
        (_parse_composite_relative, _parse_simple_relative, _parse_fixed)
        if force_relative
        else (_parse_fixed, _parse_composite_relative, _parse_simple_relative)
    )
    for parser in parsers:
        target = parser()
        if target is not None:
            return target, None

    return None, f"Format de durée non reconnu : '{time_str}'. Exemples: '15m', '1h30', '14:30', '2h'."


_REMINDER_TRIGGER_RE = re.compile(
    r"^\s*(?:"
    r"je\s+(?:veux|voudrais|souhaite|aimerais)\s+un\s+rappel"
    r"|(?:peux[- ]tu|pourrais[- ]tu|tu\s+peux)\s+me\s+rappeler"
    r"|merci\s+de\s+me\s+rappeler"
    r"|(?:fais|faites)[- ]moi\s+penser"
    r"|rappel(?:le)?[- ]moi"
    r"|(?:mets?|met|programme|cr[ée]e?|cr[ée]er|ajoute)\s+(?:un\s+)?rappel"
    r"|rappel"
    r")\b\s*",
    re.IGNORECASE,
)

_RELATIVE_TIME_RE = (
    r"\d+\s*(?:s(?:ec(?:ondes?)?)?|m(?:in(?:utes?)?)?|h(?:eures?)?|j(?:ours?)?)"
    r"(?:\s*\d+\s*(?:m(?:in(?:utes?)?)?)?)?"
)
_FIXED_TIME_RE = r"\d{1,2}\s*(?:[:h]\s*\d{0,2})"
_TIME_MARKER_RE = re.compile(
    rf"(?:^|\s)(?:dans\s+(?P<relative>{_RELATIVE_TIME_RE})"
    rf"|(?:à|a)\s+(?P<fixed>{_FIXED_TIME_RE}|{_RELATIVE_TIME_RE}))",
    re.IGNORECASE,
)

_LEADING_CONNECTOR_RE = re.compile(
    r"^(?:de\s+|d['’]\s*|pour\s+|que\s+|"
    r"(?:me|le)\s+dire\s+(?:de\s+|que\s+)?|"
    r"me\s+disant\s+(?:de\s+|que\s+)?|"
    r"me\s+rappeler\s+(?:de\s+|que\s+)?|à\s+)",
    re.IGNORECASE,
)


def _detect_natural_reminder_parts(
    text: str,
) -> Optional[Tuple[str, str, bool]]:
    """Retourne (temps, message, force_relative) pour une formulation naturelle."""
    clean = text.strip()
    trigger = _REMINDER_TRIGGER_RE.match(clean)
    if not trigger:
        return None

    body = clean[trigger.end():].strip()
    if not body:
        return None

    time_match = _TIME_MARKER_RE.search(body)
    if not time_match:
        return None

    is_relative = time_match.group("relative") is not None
    time_part = (time_match.group("relative") or time_match.group("fixed") or "").strip()
    if not time_part:
        return None

    # Le temps doit être réellement interprétable (évite les faux positifs).
    reminder_at, _ = parse_delay_or_time(time_part, force_relative=is_relative)
    if reminder_at is None:
        return None

    before = body[:time_match.start()].strip()
    after = body[time_match.end():].strip()
    message = f"{before} {after}".strip()

    # Retire les connecteurs naturels : "de", "d'", "pour", "me dire que", etc.
    # On boucle car ils peuvent s'enchaîner : « pour le dire que... ».
    previous = None
    while message and message != previous:
        previous = message
        message = _LEADING_CONNECTOR_RE.sub("", message).strip()
    message = re.sub(r"^[:\-–—\s]+", "", message)
    message = re.sub(r"[.,;!?\s]+$", "", message)
    message = message.strip(" \"'«»")

    if not message:
        return None

    return time_part, message, is_relative


def detect_natural_reminder(text: str) -> Optional[Tuple[str, str]]:
    """Détecte une formulation naturelle de rappel dans un texte.

    Exemples :
        - "Rappelle-moi dans 20 minutes de sortir le pain" -> ("20 minutes", "sortir le pain")
        - "Rappel moi les pattes dans 7m" -> ("7m", "les pattes")
        - "Je veux un rappel dans 3 minutes pour me dire que je vais me coucher"
          -> ("3 minutes", "je vais me coucher")
        - "Rappelle-moi de sortir le pain dans 20 minutes" -> ("20 minutes", "sortir le pain")
        - "Mets un rappel à 14h30 d'appeler le médecin" -> ("14h30", "appeler le médecin")
        - "Rappel dans 30m pour couper le gaz" -> ("30m", "couper le gaz")

    Retourne :
        (time_part, message_part) ou None.
    """
    parts = _detect_natural_reminder_parts(text)
    if not parts:
        return None
    time_part, message, _ = parts
    return time_part, message


def detect_natural_reminder_datetime(
    text: str,
    now: Optional[datetime.datetime] = None,
) -> Optional[Tuple[datetime.datetime, str]]:
    """Détecte un rappel naturel et retourne directement la date cible."""
    parts = _detect_natural_reminder_parts(text)
    if not parts:
        return None
    time_part, message, is_relative = parts
    target, _ = parse_delay_or_time(
        time_part, now=now, force_relative=is_relative
    )
    if target is None:
        return None
    return target, message


def _looks_like_relative_duration(candidate: str) -> bool:
    """Heuristique pour « 1h30 »/« 9h » (durée) vs « 14h30 » (heure fixe)."""
    m = re.match(
        r"^(\d{1,2})\s*h(?:\s*(\d{1,2}))?$",
        candidate.strip(),
        re.IGNORECASE,
    )
    return bool(m and int(m.group(1)) <= 12)


def split_time_and_message(raw: str) -> Tuple[Optional[datetime.datetime], str, Optional[str]]:
    """Sépare la durée et le message d'une commande /rappel.

    Essaie les préfixes les plus longs d'abord pour que « 15 minutes » soit
    interprété comme une durée relative et non comme « 15 » (15h00).

    Retourne (datetime_cible, message, erreur).
    """
    words = raw.strip().split()
    if len(words) < 2:
        return None, "", "Indiquez une durée et un message. Ex: /rappel 15m Sortir le pain"

    for count in range(min(4, len(words) - 1), 0, -1):
        candidate = " ".join(words[:count])
        force_relative = candidate.lower().startswith("dans ") or _looks_like_relative_duration(candidate)
        target, _ = parse_delay_or_time(candidate, force_relative=force_relative)
        if target is not None:
            message = " ".join(words[count:]).strip()
            if message:
                return target, message, None

    return None, "", (
        "Durée non reconnue. Exemples : 30s, 15m, 1h30, 14:30, 2h."
    )


def format_reminder_confirmation(reminder: Dict[str, Any]) -> str:
    """Formate la confirmation d'un rappel créé."""
    target = datetime.datetime.fromisoformat(reminder["remind_at"])
    if target.tzinfo is None:
        target = target.replace(tzinfo=TIMEZONE)
    return (
        f"⏰ Rappel programmé pour le {target.strftime('%d/%m à %H:%M')}.\n"
        f"📝 {reminder['message']}"
    )



def add_reminder(
    platform: str,
    chat_id: str,
    target_id: Optional[str],
    message: str,
    remind_at: datetime.datetime,
    target_name: str = "",
) -> Dict[str, Any]:
    """Enregistre un nouveau rappel dans la base SQLite."""
    now = datetime.datetime.now(TIMEZONE)
    created_at_iso = now.isoformat()
    if remind_at.tzinfo is None:
        remind_at = remind_at.replace(tzinfo=TIMEZONE)
    remind_at = remind_at.astimezone(TIMEZONE)
    remind_at_iso = remind_at.isoformat()

    with _db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO reminders (platform, chat_id, target_id, target_name, message, remind_at, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending');
            """,
            (platform, str(chat_id), str(target_id or ""), target_name, message, remind_at_iso, created_at_iso),
        )
        conn.commit()
        reminder_id = cursor.lastrowid

    return {
        "id": reminder_id,
        "platform": platform,
        "chat_id": str(chat_id),
        "target_id": str(target_id or ""),
        "target_name": target_name,
        "message": message,
        "remind_at": remind_at_iso,
        "created_at": created_at_iso,
        "status": "pending",
    }


def get_due_reminders(now: Optional[datetime.datetime] = None) -> List[Dict[str, Any]]:
    """Récupère les rappels en attente dont l'échéance est atteinte."""
    if now is None:
        now = datetime.datetime.now(TIMEZONE)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=TIMEZONE)

    with _db() as conn:
        cursor = conn.execute(
            """
            SELECT id, platform, chat_id, target_id, target_name, message, remind_at, created_at, status
            FROM reminders
            WHERE status = 'pending'
            ORDER BY remind_at ASC;
            """
        )
        due = []
        for row in cursor.fetchall():
            item = dict(row)
            try:
                target = datetime.datetime.fromisoformat(item["remind_at"])
            except (TypeError, ValueError):
                logger.error(
                    "Date de rappel invalide pour #%s : %r",
                    item.get("id"),
                    item.get("remind_at"),
                )
                continue
            if target.tzinfo is None:
                target = target.replace(tzinfo=TIMEZONE)
            if target <= now:
                due.append(item)
        due.sort(key=lambda item: datetime.datetime.fromisoformat(item["remind_at"]))
        return due


def mark_reminder_sent(reminder_id: int) -> None:
    """Marque un rappel comme envoyé."""
    with _db() as conn:
        conn.execute(
            "UPDATE reminders SET status = 'completed' WHERE id = ?;",
            (reminder_id,),
        )
        conn.commit()


def cancel_reminder(reminder_id: int, chat_id: Optional[str] = None) -> bool:
    """Annule un rappel en attente."""
    with _db() as conn:
        if chat_id:
            cursor = conn.execute(
                "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND chat_id = ? AND status = 'pending';",
                (reminder_id, str(chat_id)),
            )
        else:
            cursor = conn.execute(
                "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'pending';",
                (reminder_id,),
            )
        conn.commit()
        return cursor.rowcount > 0


def list_active_reminders(chat_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Liste les rappels actifs et futurs."""
    with _db() as conn:
        if chat_id:
            cursor = conn.execute(
                """
                SELECT id, platform, chat_id, target_id, target_name, message, remind_at, created_at, status
                FROM reminders
                WHERE status = 'pending' AND chat_id = ?
                ORDER BY remind_at ASC;
                """,
                (str(chat_id),),
            )
        else:
            cursor = conn.execute(
                """
                SELECT id, platform, chat_id, target_id, target_name, message, remind_at, created_at, status
                FROM reminders
                WHERE status = 'pending'
                ORDER BY remind_at ASC;
                """
            )
        reminders = [dict(r) for r in cursor.fetchall()]

    reminders.sort(key=lambda item: datetime.datetime.fromisoformat(item["remind_at"]))
    return reminders


def format_remaining_time(remind_at_iso: str, now: Optional[datetime.datetime] = None) -> str:
    """Calcule une description humaine du temps restant (ex: 'dans 12 minutes')."""
    if now is None:
        now = datetime.datetime.now(TIMEZONE)
    target = datetime.datetime.fromisoformat(remind_at_iso)
    if target.tzinfo is None:
        target = target.replace(tzinfo=TIMEZONE)

    diff = target - now
    total_seconds = int(diff.total_seconds())
    if total_seconds <= 0:
        return "maintenant"

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}min")
    if not parts and seconds > 0:
        parts.append(f"{seconds}s")

    return f"dans {' '.join(parts)} ({target.strftime('%H:%M')})"
