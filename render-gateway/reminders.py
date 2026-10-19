"""
reminders.py — Gestionnaire de rappels persistants (SQLite) et planificateur asynchrone.

Supporte :
- Stockage SQLite thread-safe avec mode WAL.
- Parsing des durées relatives (ex: "10s", "5m", "15min", "1h", "1h30", "2j").
- Parsing des heures fixes (ex: "14:30", "14h30", "8:00").
- Détection en langage naturel (ex: "Rappelle-moi dans 15 min de couper le feu").
- Récurrences simples : tous les jours, en semaine, chaque lundi, chaque mois.
- Snooze, clôture et reprise avec backoff exponentiel.
- Notifications asynchrones vers Telegram et Discord.
"""

import calendar
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


def _load_timezone() -> ZoneInfo:
    name = os.getenv("REMINDER_TIMEZONE", "Europe/Paris").strip() or "Europe/Paris"
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        logger.warning("Fuseau horaire de rappel invalide %r ; Europe/Paris utilisé.", name)
        return ZoneInfo("Europe/Paris")


# Fuseau horaire utilisé pour les rappels.
TIMEZONE = _load_timezone()

DB_PATH = Path(__file__).resolve().parent / "reminders.db"

# Backoff de livraison : 30 s, 1 min, 2 min, 4 min... plafonné à 1 h.
RETRY_BASE_SECONDS = int(os.getenv("REMINDER_RETRY_BASE_SECONDS", "30"))
RETRY_MAX_SECONDS = int(os.getenv("REMINDER_RETRY_MAX_SECONDS", "3600"))

WEEKDAYS = {
    "lundi": 0,
    "mardi": 1,
    "mercredi": 2,
    "jeudi": 3,
    "vendredi": 4,
    "samedi": 5,
    "dimanche": 6,
}


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


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Crée la table et migre les bases existantes sans perte de données."""
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
            status TEXT NOT NULL DEFAULT 'pending',
            recurrence TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT,
            next_attempt_at TEXT,
            last_error TEXT
        );
        """
    )

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(reminders);")}
    additions = {
        "recurrence": "TEXT",
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "last_attempt_at": "TEXT",
        "next_attempt_at": "TEXT",
        "last_error": "TEXT",
    }
    for name, ddl in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE reminders ADD COLUMN {name} {ddl};")
            logger.info("Migration reminders.db : colonne %s ajoutée.", name)

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminders_status_time "
        "ON reminders(status, remind_at);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminders_status_retry "
        "ON reminders(status, next_attempt_at);"
    )


def init_db() -> None:
    """Initialise la table des rappels et applique les migrations."""
    with _db() as conn:
        _ensure_schema(conn)
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


# ── Récurrences ──────────────────────────────────────────────────────────────

_RECURRENCE_PREFIXES: List[Tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"^\s*(?:en\s+semaine|les\s+jours\s+ouvr[ée]s|"
            r"du\s+lundi\s+au\s+vendredi|jours\s+ouvr[ée]s|"
            r"tous\s+les\s+jours\s+ouvr[ée]s)\b[,\s]*",
            re.IGNORECASE,
        ),
        "weekdays",
    ),
    (
        re.compile(
            r"^\s*(?:tous\s+les\s+jours|toutes\s+les\s+jours|"
            r"chaque\s+jour|quotidiennement|tous\s+les\s+matins|chaque\s+matin|"
            r"chaque\s+soir|tous\s+les\s+soirs)\b[,\s]*",
            re.IGNORECASE,
        ),
        "daily",
    ),
    (
        re.compile(
            r"^\s*(?:chaque|tous\s+les)\s+"
            r"(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)s?\b[,\s]*",
            re.IGNORECASE,
        ),
        "weekly_day",
    ),
    (
        re.compile(
            r"^\s*(?:chaque|toutes\s+les)\s+semaines?\b[,\s]*",
            re.IGNORECASE,
        ),
        "weekly",
    ),
    (
        re.compile(r"^\s*(?:chaque|tous\s+les)\s+mois\b[,\s]*", re.IGNORECASE),
        "monthly",
    ),
    (
        re.compile(
            r"^\s*(?:tous\s+les|toutes\s+les|chaque)\s+(\d+)\s+jours?\b[,\s]*",
            re.IGNORECASE,
        ),
        "every_days",
    ),
    (
        re.compile(
            r"^\s*(?:toutes\s+les|tous\s+les|chaque)\s+(\d+)\s+semaines?\b[,\s]*",
            re.IGNORECASE,
        ),
        "every_weeks",
    ),
    (
        re.compile(
            r"^\s*(?:tous\s+les|toutes\s+les|chaque)\s+(\d+)\s+mois\b[,\s]*",
            re.IGNORECASE,
        ),
        "every_months",
    ),
]


def strip_recurrence_prefix(text: str) -> Tuple[Optional[str], str]:
    """Extrait une éventuelle récurrence au début du texte.

    Retourne (recurrence, texte_sans_recurrence).
    """
    for pattern, kind in _RECURRENCE_PREFIXES:
        match = pattern.match(text)
        if not match:
            continue
        if kind == "weekly_day":
            day = WEEKDAYS[match.group(1).lower()]
            return f"weekly:{day}", text[match.end():].strip()
        if kind == "every_days":
            return f"every:days:{int(match.group(1))}", text[match.end():].strip()
        if kind == "every_weeks":
            return f"every:weeks:{int(match.group(1))}", text[match.end():].strip()
        if kind == "every_months":
            return f"every:months:{int(match.group(1))}", text[match.end():].strip()
        return kind, text[match.end():].strip()
    return None, text.strip()


def recurrence_label(recurrence: Optional[str]) -> str:
    """Retourne un libellé humain pour une règle de récurrence."""
    if not recurrence:
        return ""
    if recurrence == "daily":
        return "Tous les jours"
    if recurrence == "weekdays":
        return "Du lundi au vendredi"
    if recurrence == "weekly":
        return "Toutes les semaines"
    if recurrence == "monthly":
        return "Tous les mois"
    if recurrence.startswith("weekly:"):
        try:
            day = int(recurrence.split(":", 1)[1])
            names = [
                "lundi",
                "mardi",
                "mercredi",
                "jeudi",
                "vendredi",
                "samedi",
                "dimanche",
            ]
            return f"Chaque {names[day]}"
        except (ValueError, IndexError):
            return "Chaque semaine"
    if recurrence.startswith("every:days:"):
        return f"Tous les {recurrence.rsplit(':', 1)[1]} jours"
    if recurrence.startswith("every:weeks:"):
        return f"Toutes les {recurrence.rsplit(':', 1)[1]} semaines"
    if recurrence.startswith("every:months:"):
        return f"Tous les {recurrence.rsplit(':', 1)[1]} mois"
    return recurrence


def _add_months(value: datetime.datetime, months: int) -> datetime.datetime:
    """Ajoute des mois en conservant l'heure locale et en bornant le jour."""
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def compute_next_occurrence(
    current: datetime.datetime, recurrence: Optional[str]
) -> Optional[datetime.datetime]:
    """Calcule l'occurrence suivante d'une récurrence."""
    if not recurrence:
        return None
    if current.tzinfo is None:
        current = current.replace(tzinfo=TIMEZONE)

    if recurrence == "daily":
        return current + datetime.timedelta(days=1)

    if recurrence == "weekdays":
        candidate = current
        for _ in range(7):
            candidate += datetime.timedelta(days=1)
            if candidate.weekday() < 5:
                return candidate
        return None

    if recurrence == "weekly":
        return current + datetime.timedelta(days=7)

    if recurrence.startswith("weekly:"):
        try:
            target_day = int(recurrence.split(":", 1)[1])
        except ValueError:
            return None
        days_ahead = (target_day - current.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return current + datetime.timedelta(days=days_ahead)

    if recurrence == "monthly":
        return _add_months(current, 1)

    if recurrence.startswith("every:days:"):
        try:
            days = int(recurrence.rsplit(":", 1)[1])
        except ValueError:
            return None
        return current + datetime.timedelta(days=max(1, days))

    if recurrence.startswith("every:weeks:"):
        try:
            weeks = int(recurrence.rsplit(":", 1)[1])
        except ValueError:
            return None
        return current + datetime.timedelta(weeks=max(1, weeks))

    if recurrence.startswith("every:months:"):
        try:
            months = int(recurrence.rsplit(":", 1)[1])
        except ValueError:
            return None
        return _add_months(current, max(1, months))

    return None


def align_first_occurrence(
    target: datetime.datetime,
    recurrence: Optional[str],
    now: Optional[datetime.datetime] = None,
) -> datetime.datetime:
    """Aligne la première occurrence sur la règle de récurrence.

    Exemple : « chaque lundi à 9h » un lundi à 10h doit viser lundi prochain,
    et non mardi matin (résultat brut du parseur d'heure fixe).
    """
    if not recurrence:
        return target
    if now is None:
        now = datetime.datetime.now(TIMEZONE)
    if target.tzinfo is None:
        target = target.replace(tzinfo=TIMEZONE)

    if recurrence.startswith("weekly:"):
        try:
            target_day = int(recurrence.split(":", 1)[1])
        except ValueError:
            return target
        days_ahead = (target_day - target.weekday()) % 7
        if days_ahead:
            target += datetime.timedelta(days=days_ahead)
        if target <= now:
            target += datetime.timedelta(days=7)
        return target

    if recurrence == "weekdays":
        while target.weekday() >= 5:
            target += datetime.timedelta(days=1)
        return target

    if recurrence == "monthly":
        # Si le parseur a reporté au lendemain parce que l'heure est passée,
        # on veut le même jour du mois suivant, pas demain.
        if target.date() != now.date():
            target = now.replace(
                day=min(
                    now.day,
                    calendar.monthrange(now.year, now.month)[1],
                ),
                hour=target.hour,
                minute=target.minute,
                second=0,
                microsecond=0,
            )
            if target <= now:
                target = _add_months(target, 1)
        return target

    return target


def next_occurrence_after(
    current: datetime.datetime,
    recurrence: Optional[str],
    now: Optional[datetime.datetime] = None,
) -> Optional[datetime.datetime]:
    """Retourne la première occurrence strictement future après ``now``.

    Permet d'éviter de rattraper en rafale les occurrences manquées si le
    gateway était arrêté plusieurs jours.
    """
    if now is None:
        now = datetime.datetime.now(TIMEZONE)
    if not recurrence:
        return None

    candidate = current
    for _ in range(1000):
        candidate = compute_next_occurrence(candidate, recurrence)
        if candidate is None:
            return None
        if candidate > now:
            return candidate
    logger.error("Impossible de calculer la prochaine occurrence de %r.", recurrence)
    return None


# ── Détection en langage naturel ─────────────────────────────────────────────

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


def clean_reminder_message(message: str) -> str:
    """Retire les connecteurs naturels et la ponctuation autour d'un message."""
    message = message.strip()
    previous = None
    while message and message != previous:
        previous = message
        message = _LEADING_CONNECTOR_RE.sub("", message).strip()
    message = re.sub(r"^[:\-–—\s]+", "", message)
    message = re.sub(r"[.,;!?\s]+$", "", message)
    return message.strip(" \"'«»")


def _detect_natural_reminder_parts(
    text: str,
) -> Optional[Tuple[str, str, bool, Optional[str]]]:
    """Retourne (temps, message, force_relative, récurrence)."""
    clean = text.strip()
    trigger = _REMINDER_TRIGGER_RE.match(clean)
    if not trigger:
        return None

    body = clean[trigger.end():].strip()
    if not body:
        return None

    recurrence, body = strip_recurrence_prefix(body)
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
    message = clean_reminder_message(f"{before} {after}".strip())

    if not message:
        return None

    return time_part, message, is_relative, recurrence


def detect_natural_reminder(text: str) -> Optional[Tuple[str, str]]:
    """Détecte une formulation naturelle de rappel dans un texte."""
    parts = _detect_natural_reminder_parts(text)
    if not parts:
        return None
    time_part, message, _, _ = parts
    return time_part, message


def detect_natural_reminder_datetime(
    text: str,
    now: Optional[datetime.datetime] = None,
) -> Optional[Tuple[datetime.datetime, str]]:
    """Détecte un rappel naturel et retourne directement la date cible."""
    full = detect_natural_reminder_full(text, now=now)
    if not full:
        return None
    target, message, _ = full
    return target, message


def detect_natural_reminder_full(
    text: str,
    now: Optional[datetime.datetime] = None,
) -> Optional[Tuple[datetime.datetime, str, Optional[str]]]:
    """Détecte un rappel naturel et retourne (date, message, récurrence)."""
    parts = _detect_natural_reminder_parts(text)
    if not parts:
        return None
    time_part, message, is_relative, recurrence = parts
    target, _ = parse_delay_or_time(
        time_part, now=now, force_relative=is_relative
    )
    if target is None:
        return None
    target = align_first_occurrence(target, recurrence, now=now)
    return target, message, recurrence


def _looks_like_relative_duration(candidate: str) -> bool:
    """Heuristique pour « 1h30 »/« 9h » (durée) vs « 14h30 » (heure fixe)."""
    m = re.match(
        r"^(\d{1,2})\s*h(?:\s*(\d{1,2}))?$",
        candidate.strip(),
        re.IGNORECASE,
    )
    return bool(m and int(m.group(1)) <= 12)


def split_time_and_message(
    raw: str,
    now: Optional[datetime.datetime] = None,
    force_absolute: bool = False,
) -> Tuple[Optional[datetime.datetime], str, Optional[str]]:
    """Sépare la durée et le message d'une commande /rappel.

    Essaie les préfixes les plus longs d'abord pour que « 15 minutes » soit
    interprété comme une durée relative et non comme « 15 » (15h00).
    ``force_absolute`` est utilisé pour les récurrences : « chaque jour 8h »
    doit signifier 08:00, pas dans 8 heures.

    Retourne (datetime_cible, message, erreur).
    """
    words = raw.strip().split()
    if len(words) < 2:
        return None, "", "Indiquez une durée et un message. Ex: /rappel 15m Sortir le pain"

    for count in range(min(4, len(words) - 1), 0, -1):
        candidate = " ".join(words[:count])
        force_relative = False
        if not force_absolute:
            force_relative = candidate.lower().startswith(
                "dans "
            ) or _looks_like_relative_duration(candidate)
        target, _ = parse_delay_or_time(
            candidate, now=now, force_relative=force_relative
        )
        if target is not None:
            message = clean_reminder_message(" ".join(words[count:]))
            if message:
                return target, message, None

    return None, "", (
        "Durée non reconnue. Exemples : 30s, 15m, 1h30, 14:30, 2h."
    )


def parse_reminder_command(
    raw: str,
    now: Optional[datetime.datetime] = None,
) -> Tuple[Optional[datetime.datetime], str, Optional[str], Optional[str]]:
    """Parse une commande /rappel complète.

    Retourne (datetime_cible, message, récurrence, erreur).
    """
    recurrence, rest = strip_recurrence_prefix(raw)
    if not rest:
        return (
            None,
            "",
            recurrence,
            "Indiquez une durée et un message. Ex: /rappel chaque jour 8h Sortir le pain",
        )
    target, message, error = split_time_and_message(
        rest, now=now, force_absolute=bool(recurrence)
    )
    if target is not None:
        target = align_first_occurrence(target, recurrence, now=now)
    return target, message, recurrence, error


# ── Formatage ────────────────────────────────────────────────────────────────

def format_reminder_confirmation(reminder: Dict[str, Any]) -> str:
    """Formate la confirmation d'un rappel créé."""
    target = datetime.datetime.fromisoformat(reminder["remind_at"])
    if target.tzinfo is None:
        target = target.replace(tzinfo=TIMEZONE)
    text = (
        f"⏰ Rappel programmé pour le {target.strftime('%d/%m à %H:%M')}.\n"
        f"📝 {reminder['message']}"
    )
    recurrence = reminder.get("recurrence")
    if recurrence:
        text += f"\n🔁 {recurrence_label(recurrence)}"
    return text


def format_reminder_notification(reminder: Dict[str, Any]) -> str:
    """Formate le message envoyé à l'échéance."""
    text = f"⏰ Rappel #{reminder['id']} : {reminder['message']}"
    recurrence = reminder.get("recurrence")
    if recurrence:
        text += f"\n🔁 {recurrence_label(recurrence)}"
    return text


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


# ── Stockage ─────────────────────────────────────────────────────────────────

_REMINDER_FIELDS = (
    "id, platform, chat_id, target_id, target_name, message, remind_at, "
    "created_at, status, recurrence, attempts, last_attempt_at, "
    "next_attempt_at, last_error"
)


def add_reminder(
    platform: str,
    chat_id: str,
    target_id: Optional[str],
    message: str,
    remind_at: datetime.datetime,
    target_name: str = "",
    recurrence: Optional[str] = None,
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
            INSERT INTO reminders
                (platform, chat_id, target_id, target_name, message,
                 remind_at, created_at, status, recurrence)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?);
            """,
            (
                platform,
                str(chat_id),
                str(target_id or ""),
                target_name,
                message,
                remind_at_iso,
                created_at_iso,
                recurrence,
            ),
        )
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
        "recurrence": recurrence,
        "attempts": 0,
        "last_attempt_at": None,
        "next_attempt_at": None,
        "last_error": None,
    }


def get_reminder(
    reminder_id: int, chat_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Récupère un rappel par son ID, éventuellement filtré par chat."""
    with _db() as conn:
        if chat_id:
            row = conn.execute(
                f"SELECT {_REMINDER_FIELDS} FROM reminders WHERE id = ? AND chat_id = ?;",
                (reminder_id, str(chat_id)),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT {_REMINDER_FIELDS} FROM reminders WHERE id = ?;",
                (reminder_id,),
            ).fetchone()
    return dict(row) if row else None


def get_due_reminders(now: Optional[datetime.datetime] = None) -> List[Dict[str, Any]]:
    """Récupère les rappels en attente dont l'échéance est atteinte.

    Les rappels en backoff (next_attempt_at futur) sont ignorés.
    """
    if now is None:
        now = datetime.datetime.now(TIMEZONE)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=TIMEZONE)

    with _db() as conn:
        cursor = conn.execute(
            f"""
            SELECT {_REMINDER_FIELDS}
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
                if target.tzinfo is None:
                    target = target.replace(tzinfo=TIMEZONE)
                next_attempt = item.get("next_attempt_at")
                if next_attempt:
                    retry_at = datetime.datetime.fromisoformat(next_attempt)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=TIMEZONE)
                    if retry_at > now:
                        continue
            except (TypeError, ValueError):
                logger.error(
                    "Date de rappel invalide pour #%s : %r / %r",
                    item.get("id"),
                    item.get("remind_at"),
                    item.get("next_attempt_at"),
                )
                continue
            if target <= now:
                due.append(item)
        due.sort(key=lambda item: datetime.datetime.fromisoformat(item["remind_at"]))
        return due


def mark_reminder_sent(reminder_id: int) -> None:
    """Marque un rappel ponctuel comme envoyé."""
    with _db() as conn:
        conn.execute(
            """
            UPDATE reminders
            SET status = 'completed', next_attempt_at = NULL
            WHERE id = ?;
            """,
            (reminder_id,),
        )


def mark_reminder_failure(
    reminder_id: int,
    error: str,
    now: Optional[datetime.datetime] = None,
) -> datetime.datetime:
    """Enregistre un échec et programme une nouvelle tentative avec backoff."""
    if now is None:
        now = datetime.datetime.now(TIMEZONE)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=TIMEZONE)

    with _db() as conn:
        row = conn.execute(
            "SELECT attempts FROM reminders WHERE id = ?;", (reminder_id,)
        ).fetchone()
        attempts = int(row["attempts"] if row else 0) + 1
        delay = min(
            RETRY_BASE_SECONDS * (2 ** max(0, attempts - 1)),
            RETRY_MAX_SECONDS,
        )
        retry_at = now + datetime.timedelta(seconds=delay)
        conn.execute(
            """
            UPDATE reminders
            SET attempts = ?,
                last_attempt_at = ?,
                next_attempt_at = ?,
                last_error = ?
            WHERE id = ?;
            """,
            (
                attempts,
                now.isoformat(),
                retry_at.isoformat(),
                error[:2000],
                reminder_id,
            ),
        )
    logger.warning(
        "Rappel #%s : échec %d, nouvelle tentative à %s.",
        reminder_id,
        attempts,
        retry_at.strftime("%H:%M:%S"),
    )
    return retry_at


def reschedule_reminder(
    reminder_id: int,
    next_at: datetime.datetime,
) -> None:
    """Programme la prochaine occurrence d'un rappel récurrent."""
    if next_at.tzinfo is None:
        next_at = next_at.replace(tzinfo=TIMEZONE)
    with _db() as conn:
        conn.execute(
            """
            UPDATE reminders
            SET remind_at = ?,
                status = 'pending',
                attempts = 0,
                last_attempt_at = NULL,
                next_attempt_at = NULL,
                last_error = NULL
            WHERE id = ?;
            """,
            (next_at.astimezone(TIMEZONE).isoformat(), reminder_id),
        )


def snooze_reminder(
    reminder_id: int,
    duration: str = "10m",
    chat_id: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Reporte un rappel actif de la durée demandée."""
    reminder = get_reminder(reminder_id, chat_id)
    if not reminder or reminder["status"] != "pending":
        return None

    if now is None:
        now = datetime.datetime.now(TIMEZONE)
    target, error = parse_delay_or_time(duration, now=now, force_relative=True)
    if target is None:
        logger.warning("Snooze invalide pour #%s : %s", reminder_id, error)
        return None

    with _db() as conn:
        conn.execute(
            """
            UPDATE reminders
            SET remind_at = ?,
                attempts = 0,
                last_attempt_at = NULL,
                next_attempt_at = NULL,
                last_error = NULL
            WHERE id = ?;
            """,
            (target.astimezone(TIMEZONE).isoformat(), reminder_id),
        )
    return get_reminder(reminder_id, chat_id)


def complete_reminder(
    reminder_id: int,
    chat_id: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Clôt un rappel.

    Pour un rappel récurrent, seule l'occurrence courante est clôturée : la
    suivante est programmée automatiquement.
    """
    reminder = get_reminder(reminder_id, chat_id)
    if not reminder or reminder["status"] != "pending":
        return None

    if now is None:
        now = datetime.datetime.now(TIMEZONE)
    recurrence = reminder.get("recurrence")
    if recurrence:
        current = datetime.datetime.fromisoformat(reminder["remind_at"])
        next_at = next_occurrence_after(current, recurrence, now)
        if next_at is not None:
            reschedule_reminder(reminder_id, next_at)
            updated = get_reminder(reminder_id, chat_id)
            if updated:
                updated["completed_occurrence"] = reminder["remind_at"]
            return updated

    with _db() as conn:
        conn.execute(
            "UPDATE reminders SET status = 'completed', next_attempt_at = NULL WHERE id = ?;",
            (reminder_id,),
        )
    return get_reminder(reminder_id, chat_id)


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
        return cursor.rowcount > 0


def list_active_reminders(chat_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Liste les rappels actifs et futurs."""
    with _db() as conn:
        if chat_id:
            cursor = conn.execute(
                f"""
                SELECT {_REMINDER_FIELDS}
                FROM reminders
                WHERE status = 'pending' AND chat_id = ?
                ORDER BY remind_at ASC;
                """,
                (str(chat_id),),
            )
        else:
            cursor = conn.execute(
                f"""
                SELECT {_REMINDER_FIELDS}
                FROM reminders
                WHERE status = 'pending'
                ORDER BY remind_at ASC;
                """
            )
        reminders = [dict(r) for r in cursor.fetchall()]

    reminders.sort(key=lambda item: datetime.datetime.fromisoformat(item["remind_at"]))
    return reminders


def cleanup_old_reminders(max_age_days: int = 90) -> int:
    """Supprime les rappels terminés/annulés anciens."""
    cutoff = (
        datetime.datetime.now(TIMEZONE) - datetime.timedelta(days=max_age_days)
    ).isoformat()
    with _db() as conn:
        cursor = conn.execute(
            """
            DELETE FROM reminders
            WHERE status IN ('completed', 'cancelled')
              AND created_at < ?;
            """,
            (cutoff,),
        )
        return cursor.rowcount
