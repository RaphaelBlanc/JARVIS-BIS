"""
memory.py — Mémoire durable et profil utilisateur pour l'assistant.

Le stockage est un fichier JSON local, écrit de façon atomique. Il contient :
- ``profile`` : préférences durables (ville, fuseau, heure de réveil, etc.) ;
- ``facts`` : souvenirs libres ajoutés via /souviens.

Cette mémoire est injectée dans les prompts envoyés à agy afin de conserver
les préférences au-delà des conversations et des redémarrages.
"""

import datetime
import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MEMORY_PATH = Path(
    os.getenv(
        "MEMORY_FILE",
        str(Path(__file__).resolve().parent / "memory.json"),
    )
)
MAX_FACTS = int(os.getenv("MEMORY_MAX_FACTS", "200"))
MAX_FACT_LENGTH = int(os.getenv("MEMORY_MAX_FACT_LENGTH", "2000"))
MAX_PROFILE_VALUE_LENGTH = int(os.getenv("MEMORY_MAX_PROFILE_VALUE_LENGTH", "1000"))
MEMORY_PROMPT_MAX_CHARS = int(os.getenv("MEMORY_PROMPT_MAX_CHARS", "4000"))

_LOCK = threading.RLock()


def _empty_memory() -> Dict[str, Any]:
    return {"profile": {}, "facts": []}


def _normalize(data: Any) -> Dict[str, Any]:
    """Garantit une structure valide, même si le fichier est ancien/corrompu."""
    if not isinstance(data, dict):
        return _empty_memory()
    profile = data.get("profile")
    facts = data.get("facts")
    if not isinstance(profile, dict):
        profile = {}
    if not isinstance(facts, list):
        facts = []
    clean_facts = []
    for fact in facts:
        if not isinstance(fact, dict) or not fact.get("text"):
            continue
        clean_facts.append(
            {
                "id": str(fact.get("id") or uuid.uuid4().hex[:8]),
                "text": str(fact["text"])[:MAX_FACT_LENGTH],
                "tags": [str(tag)[:40] for tag in fact.get("tags", []) if tag],
                "created_at": str(
                    fact.get("created_at")
                    or datetime.datetime.now(datetime.timezone.utc).isoformat()
                ),
            }
        )
    return {
        "profile": {str(k): str(v) for k, v in profile.items()},
        "facts": clean_facts,
    }


def load_memory() -> Dict[str, Any]:
    """Charge la mémoire depuis le disque (thread-safe)."""
    with _LOCK:
        if not MEMORY_PATH.exists():
            return _empty_memory()
        try:
            with open(MEMORY_PATH, "r", encoding="utf-8") as handle:
                return _normalize(json.load(handle))
        except Exception as exc:  # noqa: BLE001
            logger.error("Lecture mémoire impossible (%s) : %s", MEMORY_PATH, exc)
            return _empty_memory()


def _save_memory(data: Dict[str, Any]) -> None:
    """Écrit la mémoire de façon atomique (thread-safe)."""
    with _LOCK:
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = MEMORY_PATH.with_suffix(MEMORY_PATH.suffix + ".tmp")
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(_normalize(data), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, MEMORY_PATH)
        try:
            os.chmod(MEMORY_PATH, 0o600)
        except OSError:
            pass


def get_memory() -> Dict[str, Any]:
    """Retourne une copie de la mémoire complète."""
    data = load_memory()
    return {"profile": dict(data["profile"]), "facts": [dict(f) for f in data["facts"]]}


def add_fact(text: str, tags: Optional[List[str]] = None) -> Dict[str, Any]:
    """Ajoute un souvenir durable."""
    clean = " ".join(str(text).strip().split())
    if not clean:
        raise ValueError("Le souvenir est vide.")
    if len(clean) > MAX_FACT_LENGTH:
        raise ValueError(f"Le souvenir dépasse {MAX_FACT_LENGTH} caractères.")

    with _LOCK:
        data = load_memory()
        if len(data["facts"]) >= MAX_FACTS:
            raise ValueError(f"La mémoire est pleine ({MAX_FACTS} souvenirs maximum).")
        fact = {
            "id": uuid.uuid4().hex[:8],
            "text": clean,
            "tags": [str(tag).strip()[:40] for tag in (tags or []) if str(tag).strip()],
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        data["facts"].append(fact)
        _save_memory(data)
    return fact


def delete_fact(fact_id: str) -> bool:
    """Supprime un souvenir par son identifiant."""
    with _LOCK:
        data = load_memory()
        before = len(data["facts"])
        data["facts"] = [
            fact for fact in data["facts"] if fact.get("id") != str(fact_id)
        ]
        if len(data["facts"]) == before:
            return False
        _save_memory(data)
        return True


def clear_facts() -> int:
    """Supprime tous les souvenirs et retourne le nombre effacé."""
    with _LOCK:
        data = load_memory()
        count = len(data["facts"])
        data["facts"] = []
        _save_memory(data)
        return count


def set_profile(key: str, value: str) -> None:
    """Définit une préférence durable."""
    clean_key = str(key).strip()[:64]
    clean_value = " ".join(str(value).strip().split())
    if not clean_key:
        raise ValueError("La clé de profil est vide.")
    if not clean_value:
        raise ValueError("La valeur de profil est vide.")
    if len(clean_value) > MAX_PROFILE_VALUE_LENGTH:
        raise ValueError(
            f"La valeur dépasse {MAX_PROFILE_VALUE_LENGTH} caractères."
        )
    with _LOCK:
        data = load_memory()
        data["profile"][clean_key] = clean_value
        _save_memory(data)


def delete_profile(key: str) -> bool:
    """Supprime une préférence durable."""
    with _LOCK:
        data = load_memory()
        if str(key) not in data["profile"]:
            return False
        del data["profile"][str(key)]
        _save_memory(data)
        return True


def render_memory_prompt(
    max_chars: Optional[int] = None,
) -> str:
    """Construit le bloc de contexte injecté dans les prompts d'agy."""
    data = load_memory()
    if not data["profile"] and not data["facts"]:
        return ""

    limit = MEMORY_PROMPT_MAX_CHARS if max_chars is None else max_chars
    lines = [
        "[Contexte utilisateur durable — informations fournies par l'utilisateur]",
    ]
    if data["profile"]:
        lines.append("Profil :")
        for key, value in sorted(data["profile"].items()):
            lines.append(f"- {key} : {value}")
    if data["facts"]:
        lines.append("Souvenirs :")
        for fact in data["facts"]:
            lines.append(f"- {fact['text']}")
    lines.append("[Fin du contexte utilisateur durable]")

    text = "\n".join(lines)
    if limit > 0 and len(text) > limit:
        text = text[: max(0, limit - 30)].rstrip() + "\n[…mémoire tronquée]"
    return text
