"""Long-term memory module for JARVIS.

Manages persistent facts, personal preferences, habits, and user context.
Stored in data/memory.json and data/memory.md for zero-token retrieval and easy user viewing.
"""

from datetime import datetime
import json
import logging
from pathlib import Path
import re
import uuid

from core.config import DATA_DIR
MEMORY_FILE = DATA_DIR / "memory.json"
MEMORY_MD = DATA_DIR / "memory.md"


def load_memory() -> list[dict]:
    """Loads all memory entries from disk."""
    if not MEMORY_FILE.exists():
        return []
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"Failed to read memory file: {e}")
        return []


def save_memory(memories: list[dict]) -> bool:
    """Saves memory entries to JSON and updates readable Markdown backup."""
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(memories, f, indent=2, ensure_ascii=False)

        # Generate pretty Markdown
        lines = [
            "# 🧠 Mémoire Durable JARVIS",
            f"*Dernière mise à jour : {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}*\n",
        ]
        categories = {}
        for m in memories:
            cat = m.get("category", "Général")
            categories.setdefault(cat, []).append(m)

        for cat, items in sorted(categories.items()):
            lines.append(f"## {cat}")
            for it in items:
                lines.append(f"- **{it.get('key', 'Fait')}** : {it.get('value')} *(ajouté le {it.get('date', '')})*")
            lines.append("")

        with open(MEMORY_MD, "w", encoding="utf-8") as f_md:
            f_md.write("\n".join(lines))

        return True
    except Exception as e:
        logging.error(f"Failed to save memory: {e}")
        return False


def detect_memory_category(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ("epita", "cours", "tp", "prof", "école", "projet", "archi")):
        return "🎓 Études & EPITA"
    if any(k in t for k in ("loyer", "appart", "proprio", "adresse", "clé", "voisin")):
        return "🏠 Logement & Quotidien"
    if any(k in t for k in ("mange", "aime", "déteste", "boisson", "café", "allergie", "resto")):
        return "🍔 Préférences & Goûts"
    if any(k in t for k in ("pc", "linux", "arch", "hyprland", "code", "git", "serveur")):
        return "💻 Tech & Dev"
    return "📌 Informations Générales"


def add_memory_fact(raw_text: str, category: str | None = None) -> dict:
    """Adds or updates a memory fact from natural language or command."""
    clean = re.sub(r"^(?:retiens\s+que|note\s+que|rappelle-toi\s+que|m[eé]moire\s*[:=]?)\s*", "", raw_text, flags=re.IGNORECASE).strip()
    
    # Check key:value format
    if ":" in clean:
        parts = clean.split(":", 1)
        key = parts[0].strip()
        val = parts[1].strip()
    else:
        key = clean[:30] + ("..." if len(clean) > 30 else "")
        val = clean

    cat = category or detect_memory_category(clean)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    memories = load_memory()
    # Check if exact or similar key already exists
    existing = next((m for m in memories if m.get("key", "").lower() == key.lower()), None)
    if existing:
        existing["value"] = val
        existing["updated_at"] = now_str
        item = existing
    else:
        item = {
            "id": uuid.uuid4().hex[:8],
            "key": key,
            "value": val,
            "category": cat,
            "date": now_str,
        }
        memories.append(item)

    save_memory(memories)
    return item


def search_memory(query: str) -> list[dict]:
    """Finds memories matching query words (0 token)."""
    q_words = [w.lower() for w in query.strip().split() if len(w) > 1]
    memories = load_memory()
    if not q_words:
        return memories

    results = []
    for m in memories:
        target = f"{m.get('key', '')} {m.get('value', '')} {m.get('category', '')}".lower()
        if any(w in target for w in q_words):
            results.append(m)
    return results


def delete_memory_fact(fact_id_or_key: str) -> bool:
    memories = load_memory()
    orig = len(memories)
    memories = [m for m in memories if m.get("id") != fact_id_or_key and m.get("key", "").lower() != fact_id_or_key.lower()]
    if len(memories) < orig:
        save_memory(memories)
        return True
    return False


def get_memory_summary_text() -> str:
    memories = load_memory()
    if not memories:
        return (
            "🧠 <b>Mémoire durable de JARVIS</b>\n\n"
            "<i>Aucun fait enregistré pour l'instant.</i>\n\n"
            "💡 <b>Pour m'apprendre quelque chose :</b>\n"
            "• <code>Retiens que j'ai cours d'Archi le mardi matin</code>\n"
            "• <code>Note que mon propriétaire s'appelle M. Durand</code>\n"
            "• <code>/memory add café : expresso sans sucre</code>"
        )

    lines = [f"🧠 <b>Mémoire durable ({len(memories)} faits retenus)</b>\n"]
    cats = {}
    for m in memories:
        cats.setdefault(m.get("category", "Général"), []).append(m)

    for cat, items in sorted(cats.items()):
        lines.append(f"<b>{cat} :</b>")
        for it in items:
            lines.append(f"• <b>{html_escape(it.get('key'))}</b> : {html_escape(it.get('value'))} <i>[ID: {it.get('id')}]</i>")
        lines.append("")

    lines.append("💡 <i>Pour supprimer un fait :</i> <code>/memory del &lt;ID&gt;</code>")
    return "\n".join(lines)


def get_memory_context_prompt(max_facts: int = 15) -> str:
    """Returns compact context to inject into AI system prompt when necessary."""
    memories = load_memory()
    if not memories:
        return ""
    facts = [f"- {m.get('key')}: {m.get('value')}" for m in memories[-max_facts:]]
    return "[Faits mémorisés sur l'utilisateur]:\n" + "\n".join(facts) + "\n"


def html_escape(text: str) -> str:
    import html
    return html.escape(str(text or ""))
