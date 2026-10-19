"""
briefing.py — Configuration et construction du briefing matinal.

Le briefing est configurable par variables d'environnement :
- BRIEFING_ENABLED (1/0)
- BRIEFING_TIME (HH:MM)
- BRIEFING_DAYS (mon,tue,wed,thu,fri,sat,sun)
- BRIEFING_TIMEZONE (Europe/Paris)
- BRIEFING_CITY (facultatif)
- BRIEFING_SECTIONS (agenda,news,email,weather,tasks)

L'état de diffusion est persisté pour éviter les doublons après un
redémarrage et pour ne renvoyer que sur les plateformes en échec.
"""

import datetime
import json
import logging
import os
from pathlib import Path
from typing import Iterable, List, Optional, Set
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

STATE_FILE = Path(
    os.getenv(
        "BRIEFING_STATE_FILE",
        str(Path(__file__).resolve().parent / "briefing_state.json"),
    )
)

DAY_ALIASES = {
    "mon": 0,
    "monday": 0,
    "lun": 0,
    "lundi": 0,
    "0": 0,
    "tue": 1,
    "tuesday": 1,
    "mar": 1,
    "mardi": 1,
    "1": 1,
    "wed": 2,
    "wednesday": 2,
    "mer": 2,
    "mercredi": 2,
    "2": 2,
    "thu": 3,
    "thursday": 3,
    "jeu": 3,
    "jeudi": 3,
    "3": 3,
    "fri": 4,
    "friday": 4,
    "ven": 4,
    "vendredi": 4,
    "4": 4,
    "sat": 5,
    "saturday": 5,
    "sam": 5,
    "samedi": 5,
    "5": 5,
    "sun": 6,
    "sunday": 6,
    "dim": 6,
    "dimanche": 6,
    "6": 6,
}

VALID_SECTIONS = {"agenda", "news", "email", "weather", "tasks"}


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off", "non", "disabled"}


def _load_timezone() -> ZoneInfo:
    name = os.getenv("BRIEFING_TIMEZONE", "Europe/Paris").strip() or "Europe/Paris"
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        logger.warning("Fuseau de briefing invalide %r ; Europe/Paris utilisé.", name)
        return ZoneInfo("Europe/Paris")


def parse_days(raw: str) -> Set[int]:
    """Parse une liste de jours (noms anglais/français ou 0-6)."""
    days: Set[int] = set()
    for token in raw.replace(";", ",").split(","):
        token = token.strip().lower()
        if not token:
            continue
        if token in DAY_ALIASES:
            days.add(DAY_ALIASES[token])
    return days


def parse_time(raw: str) -> tuple[int, int]:
    """Parse une heure HH:MM, avec repli à 08:00."""
    try:
        hour_str, minute_str = raw.strip().split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (ValueError, AttributeError):
        pass
    logger.warning("BRIEFING_TIME invalide (%r) ; 08:00 utilisé.", raw)
    return 8, 0


def parse_sections(raw: str) -> Set[str]:
    """Parse les sections activées, en ignorant les inconnues."""
    sections = {
        token.strip().lower()
        for token in raw.replace(";", ",").split(",")
        if token.strip()
    }
    unknown = sections - VALID_SECTIONS
    if unknown:
        logger.warning("Sections de briefing inconnues ignorées : %s", ", ".join(sorted(unknown)))
    return sections & VALID_SECTIONS


BRIEFING_ENABLED = _env_bool("BRIEFING_ENABLED", True)
BRIEFING_HOUR, BRIEFING_MINUTE = parse_time(
    os.getenv("BRIEFING_TIME", "08:00")
)
BRIEFING_DAYS = parse_days(
    os.getenv("BRIEFING_DAYS", "mon,tue,wed,thu,fri,sat,sun")
)
BRIEFING_TIMEZONE = _load_timezone()
BRIEFING_CITY = os.getenv("BRIEFING_CITY", "").strip()
BRIEFING_SECTIONS = parse_sections(
    os.getenv("BRIEFING_SECTIONS", "agenda,news,email,weather,tasks")
)
BRIEFING_WINDOW_MINUTES = int(os.getenv("BRIEFING_WINDOW_MINUTES", "5"))
BRIEFING_PLATFORMS = ["telegram", "discord"]


def schedule_description() -> str:
    """Description lisible pour les logs."""
    day_names = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
    days = ",".join(day_names[day] for day in sorted(BRIEFING_DAYS)) or "aucun"
    return (
        f"{BRIEFING_HOUR:02d}:{BRIEFING_MINUTE:02d} "
        f"({days}, {BRIEFING_TIMEZONE.key})"
    )


def should_send_now(
    now: Optional[datetime.datetime] = None,
    window_minutes: Optional[int] = None,
) -> bool:
    """Indique si le briefing doit être tenté maintenant."""
    if not BRIEFING_ENABLED or not BRIEFING_DAYS:
        return False
    if now is None:
        now = datetime.datetime.now(BRIEFING_TIMEZONE)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=BRIEFING_TIMEZONE)

    if now.weekday() not in BRIEFING_DAYS:
        return False

    window = (
        BRIEFING_WINDOW_MINUTES
        if window_minutes is None
        else max(0, window_minutes)
    )
    target = now.replace(
        hour=BRIEFING_HOUR,
        minute=BRIEFING_MINUTE,
        second=0,
        microsecond=0,
    )
    return target <= now < target + datetime.timedelta(minutes=window)


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lecture état briefing impossible : %s", exc)
        return {}


def _save_state(state: dict) -> None:
    """Écrit l'état de diffusion de façon atomique."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_path = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, STATE_FILE)
        try:
            os.chmod(STATE_FILE, 0o600)
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001
        logger.error("Écriture état briefing impossible : %s", exc)


def is_platform_sent(date_key: str, platform: str) -> bool:
    """Indique si le briefing a déjà été envoyé sur une plateforme."""
    state = _load_state()
    return bool(state.get(date_key, {}).get(platform))


def mark_platform_sent(date_key: str, platform: str) -> None:
    """Marque une plateforme comme servie pour la date donnée."""
    state = _load_state()
    day = state.setdefault(date_key, {})
    day[platform] = True

    # Conserve seulement les 14 derniers jours.
    cutoff = (
        datetime.datetime.now(BRIEFING_TIMEZONE) - datetime.timedelta(days=14)
    ).strftime("%Y-%m-%d")
    state = {key: value for key, value in state.items() if key >= cutoff}
    _save_state(state)


def pending_platforms(date_key: str) -> List[str]:
    """Retourne les plateformes qui n'ont pas encore reçu le briefing."""
    return [
        platform
        for platform in BRIEFING_PLATFORMS
        if not is_platform_sent(date_key, platform)
    ]


def build_briefing_prompt(
    sections: Optional[Iterable[str]] = None,
    city: Optional[str] = None,
) -> str:
    """Construit le prompt du briefing à partir de la configuration."""
    active = set(sections) if sections is not None else set(BRIEFING_SECTIONS)
    city_name = (BRIEFING_CITY if city is None else city).strip() or "ma ville"

    parts = [
        "Effectue mon briefing matinal rapide, motivant et structuré.",
        "Sois ultra-synthétique, direct et dynamique. Pas de bavardage, "
        "va droit au but avec des puces courtes.",
    ]

    index = 1
    if "agenda" in active:
        parts.append(
            f"{index}. 📅 EMPLOI DU TEMPS DU JOUR (Heure / Cours / Salles) :\n"
            "Consulte mon Google Agenda aujourd'hui (en incluant mon calendrier "
            "de cours EPITA/PREPA). Liste uniquement les horaires, le cours et "
            "la salle exacte : • Début - Fin : Intitulé (📍 Salle). "
            "S'il n'y a pas de cours, mentionne-le brièvement."
        )
        index += 1

    if "news" in active:
        parts.append(
            f"{index}. 📈 ACTUALITÉS ÉCONOMIQUES :\n"
            "Recherche rapidement les 2 faits économiques ou tech marquants de "
            "la veille (1 seule recherche concise) et résume-les en 2 puces "
            "percutantes."
        )
        index += 1

    if "email" in active:
        parts.append(
            f"{index}. ✉️ E-MAILS IMPORTANTS :\n"
            "Consulte mes 5 derniers e-mails récents via Gmail. Ignore totalement "
            "spams, pubs et newsletters. Ne retiens que les messages clés "
            "(EPITA, examens, personnel) et résume chaque message en 1 ligne."
        )
        index += 1

    if "weather" in active:
        parts.append(
            f"{index}. 🌤️ MÉTÉO DU JOUR :\n"
            f"Météo du jour pour {city_name} (température, ciel, pluie)."
        )
        index += 1

    if "tasks" in active:
        parts.append(
            f"{index}. 📋 TÂCHES EN COURS :\n"
            "Consulte mes tâches Google Tasks non terminées si disponibles."
        )
        index += 1

    if not active:
        parts.append("Aucune section n'est activée : rappelle-le brièvement.")

    return "\n\n".join(parts)
