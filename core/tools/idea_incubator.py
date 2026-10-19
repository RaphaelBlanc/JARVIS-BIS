"""Idea Incubator & Structured Brainstorming Engine for JARVIS.

Transforms raw user thoughts (text or voice) into structured engineering specs:
- Pitch & Problem solved
- Suggested Tech Stack & Architecture
- 3-Step Weekend MVP Plan
- Synergies with existing projects (ReconForge, JARVIS, Cyber tools)
- Impact / Feasibility score
Persists in data/ideas.json and mirrors to Obsidian (showcase-vault/Ideas/).
"""

import asyncio
from datetime import datetime
import html
import json
import logging
from pathlib import Path
import re
import uuid

from core.tools.obsidian_bridge import create_or_append_vault_note

from core.config import DATA_DIR
IDEAS_FILE = DATA_DIR / "ideas.json"


def load_ideas() -> list[dict]:
    if not IDEAS_FILE.exists():
        return []
    try:
        with open(IDEAS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"Failed to load ideas: {e}")
        return []


def save_ideas(ideas: list[dict]) -> bool:
    try:
        with open(IDEAS_FILE, "w", encoding="utf-8") as f:
            json.dump(ideas, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logging.error(f"Failed to save ideas: {e}")
        return False


def get_idea_by_id(idea_id: str) -> dict | None:
    ideas = load_ideas()
    return next((i for i in ideas if i.get("id") == idea_id), None)


def delete_idea(idea_id: str) -> bool:
    ideas = load_ideas()
    orig = len(ideas)
    ideas = [i for i in ideas if i.get("id") != idea_id]
    if len(ideas) < orig:
        save_ideas(ideas)
        return True
    return False


async def incubate_idea(raw_text: str, genai_client=None) -> dict:
    """Transforms a raw idea into a structured MVP specification and saves it."""
    clean = re.sub(
        r"^/(?:idea|idee|incubate)(?:@[a-zA-Z0-9_]+)?\s*",
        "",
        raw_text.strip(),
        flags=re.IGNORECASE,
    ).strip()
    clean = re.sub(
        r"^(?:j'ai\s+une\s+id[eé]e(?:\s+de\s+projet|\s+d'outil)?|id[eé]e\s+de\s+projet|id[eé]e\s+d'outil|nouvelle\s+id[eé]e|note\s+cette\s+id[eé]e)\s*[:=]?\s*",
        "",
        clean,
        flags=re.IGNORECASE,
    ).strip()

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    idea_id = uuid.uuid4().hex[:8]

    # Default deterministic structure if offline or no Gemini client
    words = re.sub(r"[^\w\séèêëàâôûîïç\-]", "", clean).split()[:5]
    default_title = " ".join(words).capitalize() if words else "Nouveau Projet"
    slug = re.sub(r"[^a-z0-9]+", "-", default_title.lower()).strip("-")[:24] or "new-tool"

    spec = {
        "title": default_title,
        "slug": slug,
        "pitch": clean,
        "stack": "Python 3.12+ (asyncio, httpx, rich / typer)",
        "mvp_steps": [
            "1. Architecture & CLI core avec parsing des arguments",
            "2. Implémentation du moteur principal et tests unitaires pytest",
            "3. Export JSON/Markdown et packaging pyproject.toml",
        ],
        "synergy": "Intégrable dans ton écosystème Cyber / Dev (ReconForge ou JARVIS)",
        "score": "8.5/10",
        "template": "python-cli",
    }

    if genai_client:
        try:
            prompt = (
                "Tu es un architecte logiciel senior et incubateur de projets Cyber / Dev / IA.\n"
                f"L'utilisateur propose cette idée brute : « {clean} »\n\n"
                "Analyse cette idée et renvoie UNIQUEMENT un objet JSON valide (sans markdown autour) avec ces clés exactes :\n"
                "{\n"
                '  "title": "Nom court et percutant du projet/outil (2-4 mots)",\n'
                '  "slug": "nom-kebab-case-court",\n'
                '  "pitch": "Proposition de valeur claire en 1-2 phrases",\n'
                '  "stack": "Stack technique idéale concise (ex: Python asyncio + httpx + rich, ou FastAPI, ou Rust)",\n'
                '  "mvp_steps": ["Étape 1 concrète", "Étape 2 concrète", "Étape 3 concrète"],\n'
                '  "synergy": "Comment cette idée peut s\'interfacer avec ReconForge (outil de recon cyber), JARVIS (assistant Telegram) ou Obsidian",\n'
                '  "score": "Note de faisabilité/impact sur 10 (ex: 9/10 — Fort impact)",\n'
                '  "template": "python-cli" | "fastapi" | "cyber-tool" | "web"\n'
                "}"
            )
            resp = await asyncio.to_thread(
                genai_client.models.generate_content,
                model="gemini-3.5-flash-lite",
                contents=prompt,
            )
            raw_out = (resp.text or "").strip()
            m = re.search(r"\{.*\}", raw_out, flags=re.DOTALL)
            if m:
                parsed = json.loads(m.group(0))
                spec.update({k: v for k, v in parsed.items() if v})
        except Exception as e:
            logging.warning(f"Gemini idea incubation fallback: {e}")

    item = {
        "id": idea_id,
        "raw": clean,
        "title": spec["title"],
        "slug": re.sub(r"[^a-z0-9\-]", "", str(spec.get("slug", slug)).lower()) or slug,
        "pitch": spec["pitch"],
        "stack": spec["stack"],
        "mvp_steps": spec["mvp_steps"] if isinstance(spec.get("mvp_steps"), list) else [str(spec.get("mvp_steps"))],
        "synergy": spec["synergy"],
        "score": spec["score"],
        "template": spec.get("template", "python-cli"),
        "status": "incubated",
        "created_at": now_str,
    }

    ideas = load_ideas()
    ideas.append(item)
    save_ideas(ideas)

    # Mirror directly into Obsidian showcase-vault/Ideas/
    try:
        steps_md = "\n".join(f"- [ ] {s}" for s in item["mvp_steps"])
        obs_body = (
            f"## 🎯 Pitch & Problème\n{item['pitch']}\n\n"
            f"## 🛠️ Stack & Architecture\n`{item['stack']}`\n\n"
            f"## ⚡ Plan MVP (3 Étapes)\n{steps_md}\n\n"
            f"## 🔗 Synergies\n{item['synergy']}\n\n"
            f"**Score Impact/Faisabilité :** {item['score']} | **Slug :** `{item['slug']}`"
        )
        create_or_append_vault_note(
            f"{item['title']} : {obs_body}",
            explicit_folder="Ideas",
            tags=["idea", "mvp", item["template"]],
        )
    except Exception as e:
        logging.warning(f"Could not mirror idea to Obsidian: {e}")

    return item


def format_idea_card(item: dict) -> str:
    """Formats a single incubated idea into rich Telegram HTML."""
    steps_html = "\n".join(f"  • {html.escape(str(s))}" for s in item.get("mvp_steps", []))
    return (
        f"💡 <b>Idée Incubée : {html.escape(item.get('title', 'Projet'))}</b> <code>[{item.get('id')}]</code>\n\n"
        f"🎯 <b>Pitch :</b>\n<i>{html.escape(item.get('pitch', ''))}</i>\n\n"
        f"🛠️ <b>Stack recommandée :</b> <code>{html.escape(item.get('stack', ''))}</code>\n\n"
        f"⚡ <b>Plan d'attaque MVP :</b>\n{steps_html}\n\n"
        f"🔗 <b>Synergie :</b> {html.escape(item.get('synergy', ''))}\n"
        f"📊 <b>Potentiel :</b> <b>{html.escape(str(item.get('score', '8/10')))}</b>\n"
        f"🧠 <i>Synchronisé dans Obsidian (<code>Ideas/{html.escape(item.get('title', ''))}.md</code>)</i>"
    )


def format_ideas_list() -> dict:
    """Formats the list of all incubated ideas."""
    ideas = load_ideas()
    if not ideas:
        return {
            "ideas": [],
            "message": (
                "💡 <b>Incubateur d'Idées & Projets JARVIS</b>\n\n"
                "<i>Aucune idée enregistrée pour le moment.</i>\n\n"
                "<b>Pour incuber une idée :</b>\n"
                "• <code>/idea Un scanner DNS asynchrone qui exporte en graphe Obsidian</code>\n"
                "• Ou en vocal / texte : <i>« J'ai une idée d'outil cyber qui... »</i>"
            ),
        }

    lines = [f"💡 <b>Backlog d'Innovation ({len(ideas)} idées incubées)</b>\n"]
    for idx, it in enumerate(reversed(ideas[-10:]), 1):
        status_icon = "🚀" if it.get("status") == "launched" else "💡"
        lines.append(
            f"{idx}. {status_icon} <b>{html.escape(it.get('title', 'Idée'))}</b> "
            f"(<code>{html.escape(it.get('slug', ''))}</code>) — <b>{html.escape(str(it.get('score', '')))}</b>\n"
            f"   └ <i>{html.escape(it.get('pitch', '')[:95])}...</i> <code>[ID: {it.get('id')}]</code>"
        )

    lines.append("\n👇 <i>Cliquez sur un bouton pour convertir une idée en vrai dépôt de code :</i>")
    return {
        "ideas": list(reversed(ideas[-8:])),
        "message": "\n".join(lines),
    }
