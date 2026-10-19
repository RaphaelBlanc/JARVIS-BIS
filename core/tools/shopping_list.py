"""Shopping and grocery list management for JARVIS.

Handles recording, tracking, and interactive inline toggling of grocery items.
Persisted locally in data/courses.json.
"""

from datetime import datetime
import json
import logging
from pathlib import Path
import re
import uuid

from core.config import DATA_DIR
COURSES_FILE = DATA_DIR / "courses.json"


def load_courses() -> list[dict]:
    """Loads shopping list items from disk."""
    if not COURSES_FILE.exists():
        return []
    try:
        with open(COURSES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"Failed to read {COURSES_FILE.name}: {e}")
        return []


def save_courses(items: list[dict]) -> bool:
    """Saves shopping list items to disk."""
    try:
        with open(COURSES_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logging.error(f"Failed to write courses file: {e}")
        return False


def clean_item_text(text: str) -> str:
    """Cleans up item text removing bullet markers, quotes, and excessive whitespace."""
    t = text.strip()
    t = re.sub(r"^[-*•+–—]\s*", "", t)
    t = re.sub(r"^\d+[\.\)]\s*", "", t)
    t = re.sub(r"^\[[\s✓xX]?\]\s*", "", t)
    return t.strip()


def add_course_items(raw_text: str) -> list[dict]:
    """Parses raw text and adds items to the shopping list.

    Supports:
    - Comma-separated: "lait, 6 oeufs, café, pain de mie"
    - Newline-separated lists
    - Space-separated if no commas/newlines
    """
    clean = raw_text.strip()
    # Strip command prefix if passed directly
    clean = re.sub(r"^/courses(?:@[a-zA-Z0-9_]+)?\s*", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"^(?:ajouter|\+)\s*", "", clean, flags=re.IGNORECASE)

    raw_items: list[str] = []
    if "\n" in clean:
        raw_items = clean.split("\n")
    elif "," in clean:
        raw_items = clean.split(",")
    else:
        # Check if words look like separate items or a single multi-word item
        # If text has common conjunctions or short items
        raw_items = [clean]

    courses = load_courses()
    added: list[dict] = []
    now_iso = datetime.now().isoformat()

    for item_str in raw_items:
        txt = clean_item_text(item_str)
        if not txt:
            continue
        # Capitalize first letter
        txt_formatted = txt[0].upper() + txt[1:] if len(txt) > 1 else txt.upper()
        
        # Avoid duplicate pending items with same name
        existing = next((c for c in courses if not c.get("done") and c.get("text", "").lower() == txt_formatted.lower()), None)
        if existing:
            continue

        item_obj = {
            "id": uuid.uuid4().hex[:8],
            "text": txt_formatted,
            "done": False,
            "created_at": now_iso,
        }
        courses.append(item_obj)
        added.append(item_obj)

    if added:
        save_courses(courses)

    return added


def toggle_course_item(item_id: str) -> tuple[bool, dict | None]:
    """Toggles item status between completed and pending."""
    courses = load_courses()
    for item in courses:
        if item.get("id") == item_id:
            item["done"] = not item.get("done", False)
            item["updated_at"] = datetime.now().isoformat()
            save_courses(courses)
            return True, item
    return False, None


def delete_course_item(item_id: str) -> bool:
    """Deletes a specific item by ID."""
    courses = load_courses()
    orig_len = len(courses)
    courses = [c for c in courses if c.get("id") != item_id]
    if len(courses) < orig_len:
        save_courses(courses)
        return True
    return False


def clear_done_courses() -> int:
    """Removes all completed (done=True) items."""
    courses = load_courses()
    pending = [c for c in courses if not c.get("done", False)]
    removed_count = len(courses) - len(pending)
    if removed_count > 0:
        save_courses(pending)
    return removed_count


def clear_all_courses() -> int:
    """Clears all items in the shopping list."""
    courses = load_courses()
    count = len(courses)
    save_courses([])
    return count


def format_courses_status() -> dict:
    """Formats the current shopping list and returns text summary + item lists."""
    courses = load_courses()
    pending = [c for c in courses if not c.get("done", False)]
    done = [c for c in courses if c.get("done", False)]

    lines = [
        "🛒 <b>Liste de courses JARVIS</b>",
        f"• <b>Articles à acheter :</b> {len(pending)}",
    ]

    if not courses:
        lines.append("\n<i>La liste de courses est actuellement vide !</i>")
        lines.append("💡 <i>Ajoutez des articles avec :</i> <code>/courses lait, oeufs, café</code>")
    else:
        if pending:
            lines.append("\n<b>À acheter :</b>")
            for item in pending:
                lines.append(f"• ▫️ {item.get('text')}")

        if done:
            lines.append(f"\n<b>Déjà achetés ({len(done)}) :</b>")
            for item in done:
                lines.append(f"• <s>{item.get('text')}</s>")

    return {
        "total": len(courses),
        "pending": pending,
        "done": done,
        "text": "\n".join(lines),
    }
