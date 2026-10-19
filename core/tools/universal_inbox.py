"""Universal Inbox & Cyber/Tech Radar manager for JARVIS.

Captures, enriches, classifies and routes incoming links, ideas, notes and documents:
- Web links (GitHub repos, articles, CVEs, videos) -> Enriched with metadata/summary in data/inbox_links.json
- Direct export of captured links to Obsidian (showcase-vault/Tools or Resources)
- 1-click git clone for GitHub links into ~/Documents/Cyber/Tools
- Live Cyber & Tech Radar (/radar) fetching top GitHub security/dev tools & HackerNews stories
- Raw thoughts and quick notes -> Routed to Obsidian showcase-vault + data/notes.md
"""

import asyncio
from datetime import datetime
import html
import json
import logging
from pathlib import Path
import re
import uuid
import httpx

from core.tools.obsidian_bridge import create_or_append_vault_note

from core.config import DATA_DIR
INBOX_LINKS_FILE = DATA_DIR / "inbox_links.json"
NOTES_FILE = DATA_DIR / "notes.md"
LOCAL_INBOX_DIR = Path.home() / "Documents" / "Inbox"
LOCAL_INBOX_DIR.mkdir(parents=True, exist_ok=True)
CYBER_TOOLS_DIR = Path.home() / "Documents" / "Cyber" / "Tools"
CYBER_TOOLS_DIR.mkdir(parents=True, exist_ok=True)


def load_inbox_links() -> list[dict]:
    if not INBOX_LINKS_FILE.exists():
        return []
    try:
        with open(INBOX_LINKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"Failed to read inbox links: {e}")
        return []


def save_inbox_links(links: list[dict]) -> bool:
    try:
        with open(INBOX_LINKS_FILE, "w", encoding="utf-8") as f:
            json.dump(links, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logging.error(f"Failed to save inbox links: {e}")
        return False


def get_inbox_link_by_id(link_id: str) -> dict | None:
    links = load_inbox_links()
    return next((l for l in links if l.get("id") == link_id), None)


def extract_urls(text: str) -> list[str]:
    return re.findall(r"https?://[^\s]+", text)


def capture_link(url: str, note: str = "") -> dict:
    """Synchronous fast link capture."""
    links = load_inbox_links()
    domain = re.sub(r"^https?://(?:www\.)?([^/]+).*", r"\1", url)
    is_gh = "github.com/" in url.lower()
    item = {
        "id": uuid.uuid4().hex[:8],
        "url": url,
        "domain": domain,
        "title": domain,
        "summary": "",
        "is_github": is_gh,
        "type": "github_repo" if is_gh else "web",
        "stars": None,
        "language": None,
        "note": note.strip(),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "read": False,
    }
    links.append(item)
    save_inbox_links(links)
    return item


async def capture_and_enrich_link(url: str, note: str = "", genai_client=None) -> dict:
    """Captures a URL and asynchronously enriches it with title, GitHub stats, and summary."""
    item = capture_link(url, note)
    gh_match = re.search(r"github\.com/([a-zA-Z0-9_.\-]+)/([a-zA-Z0-9_.\-]+)", url)

    title = item["domain"]
    description = ""
    stars = None
    language = None
    topics = []

    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            if gh_match:
                owner, repo = gh_match.group(1), gh_match.group(2).rstrip("/")
                repo = re.sub(r"\.git$", "", repo)
                api_url = f"https://api.github.com/repos/{owner}/{repo}"
                resp = await client.get(api_url, headers={"Accept": "application/vnd.github+json"})
                if resp.status_code == 200:
                    data = resp.json()
                    title = data.get("full_name", f"{owner}/{repo}")
                    description = data.get("description") or ""
                    stars = data.get("stargazers_count")
                    language = data.get("language")
                    topics = data.get("topics") or []
            else:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 JARVIS-Assistant/2.0"})
                if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
                    html_snippet = resp.text[:25000]
                    m_title = re.search(r"<title[^>]*>(.*?)</title>", html_snippet, re.IGNORECASE | re.DOTALL)
                    if m_title:
                        title = html.unescape(re.sub(r"\s+", " ", m_title.group(1)).strip())[:100]
                    m_desc = re.search(
                        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
                        html_snippet,
                        re.IGNORECASE,
                    )
                    if m_desc:
                        description = html.unescape(m_desc.group(1).strip())[:280]
    except Exception as e:
        logging.debug(f"Link metadata fetch fallback: {e}")

    summary = description
    if genai_client and (description or gh_match):
        try:
            prompt = (
                f"Lien capturé : {url}\nTitre : {title}\nDescription : {description}\n"
                f"Langage : {language or 'N/A'} | Topics : {', '.join(topics)}\n"
                f"Note utilisateur : {note}\n\n"
                "En 2 phrases très concises en français, explique à quoi sert cette ressource/outil et comment elle peut être utile en Cybersécurité ou Développement."
            )
            resp = await asyncio.to_thread(
                genai_client.models.generate_content,
                model="gemini-3.5-flash-lite",
                contents=prompt,
            )
            if resp.text:
                summary = resp.text.strip()
        except Exception:
            pass

    # Update persisted link
    links = load_inbox_links()
    for l in links:
        if l.get("id") == item["id"]:
            l["title"] = title
            l["summary"] = summary
            l["stars"] = stars
            l["language"] = language
            l["topics"] = topics
            item = l
            break
    save_inbox_links(links)
    return item


def format_enriched_link_card(item: dict) -> str:
    """Formats a captured link card with enriched metadata for Telegram."""
    lines = [
        f"📥 <b>Ressource Capturée & Analysée</b> <code>[{item['id']}]</code>\n",
        f"🔗 <b><a href=\"{html.escape(item['url'])}\">{html.escape(item.get('title') or item['domain'])}</a></b>",
    ]
    if item.get("stars") is not None or item.get("language"):
        meta_parts = []
        if item.get("stars") is not None:
            meta_parts.append(f"⭐ <b>{item['stars']}</b> stars")
        if item.get("language"):
            meta_parts.append(f"💻 <code>{html.escape(str(item['language']))}</code>")
        lines.append("• " + " | ".join(meta_parts))

    if item.get("note"):
        lines.append(f"• 📝 <b>Note :</b> <i>{html.escape(item['note'])}</i>")

    if item.get("summary"):
        lines.append(f"\n💡 <b>Synthèse :</b>\n<i>{html.escape(item['summary'])}</i>")

    return "\n".join(lines)


def save_link_to_obsidian(link_id: str) -> dict:
    """Saves an enriched inbox link directly as a note in the Obsidian showcase-vault."""
    item = get_inbox_link_by_id(link_id)
    if not item:
        return {"ok": False, "message": "❌ Lien introuvable dans l'Inbox."}

    folder = "Tools" if item.get("is_github") else "Resources"
    title = re.sub(r"[^\w\s\-]", "_", item.get("title") or item.get("domain", "Ressource")).strip()[:45]
    body_lines = [
        f"- **URL :** {item['url']}",
        f"- **Capturé le :** {item.get('created_at', '')}",
    ]
    if item.get("language"):
        body_lines.append(f"- **Langage :** `{item['language']}`")
    if item.get("stars") is not None:
        body_lines.append(f"- **GitHub Stars :** ⭐ {item['stars']}")
    if item.get("note"):
        body_lines.append(f"- **Note personnelle :** {item['note']}")
    if item.get("summary"):
        body_lines.append(f"\n### Synthèse\n{item['summary']}")

    res = create_or_append_vault_note(
        f"{title} : " + "\n".join(body_lines),
        explicit_folder=folder,
        tags=["inbox", folder.lower()],
    )
    mark_link_as_read(link_id)
    return {
        "ok": True,
        "rel_path": res.get("rel", ""),
        "message": f"🧠 <b>Fiche enregistrée dans Obsidian :</b> <code>{html.escape(res['rel'])}</code>",
    }


async def clone_github_repo(link_id: str) -> dict:
    """Clones a GitHub repository from the Inbox into ~/Documents/Cyber/Tools."""
    item = get_inbox_link_by_id(link_id)
    if not item:
        return {"ok": False, "message": "❌ Lien introuvable."}
    url = item.get("url", "")
    gh_match = re.search(r"github\.com/([a-zA-Z0-9_.\-]+)/([a-zA-Z0-9_.\-]+)", url)
    if not gh_match:
        return {"ok": False, "message": "⚠️ Ce lien n'est pas un dépôt GitHub valide."}

    owner, repo = gh_match.group(1), re.sub(r"\.git$", "", gh_match.group(2).rstrip("/"))
    clone_url = f"https://github.com/{owner}/{repo}.git"
    dest = CYBER_TOOLS_DIR / repo
    if dest.exists():
        return {"ok": True, "message": f"📁 Le dépôt est déjà présent dans <code>{dest}</code>."}

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", clone_url, str(dest),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        if proc.returncode == 0 and dest.exists():
            mark_link_as_read(link_id)
            return {
                "ok": True,
                "message": f"✅ Dépôt <b>{html.escape(repo)}</b> cloné avec succès dans <code>{dest}</code> !",
            }
        return {
            "ok": False,
            "message": f"⚠️ Échec git clone : <code>{html.escape(stderr.decode('utf-8', errors='ignore')[:300])}</code>",
        }
    except Exception as e:
        return {"ok": False, "message": f"⚠️ Erreur lors du clonage : {html.escape(str(e))}"}


def capture_quick_note(text: str, tags: list[str] | None = None) -> dict:
    """Saves note directly into the real Obsidian vault AND mirrors to data/notes.md."""
    obs_res = create_or_append_vault_note(text, tags=tags or ["inbox"])
    note_id = uuid.uuid4().hex[:8]
    return {
        "ok": True,
        "id": note_id,
        "text": text.strip(),
        "obsidian_rel": obs_res.get("rel", ""),
        "rel_path": obs_res.get("rel", ""),
        "folder": obs_res.get("folder", "Inbox"),
        "date": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "message": obs_res.get("message", "📥 <b>Note enregistrée dans l'Inbox Obsidian !</b>"),
    }


def list_inbox_items() -> dict:
    links = load_inbox_links()
    unread_links = [l for l in links if not l.get("read")]

    lines = [
        "📥 <b>Boîte de Réception Universelle & Veille JARVIS</b>",
        f"• <b>Liens à traiter :</b> {len(unread_links)} non lus (sur {len(links)} capturés)",
        f"• <b>Coffre Obsidian lié :</b> <code>showcase-vault</code>",
        f"• <b>Dossier Outils Cyber :</b> <code>~/Documents/Cyber/Tools</code>\n",
    ]

    if unread_links:
        lines.append("<b>Dernières ressources en attente :</b>")
        for l in reversed(unread_links[-6:]):
            title = l.get("title") or l.get("domain", "Lien")
            star_str = f" (⭐{l['stars']})" if l.get("stars") is not None else ""
            note_part = f" — <i>{html.escape(l.get('summary') or l.get('note') or '')[:80]}</i>" if (l.get("summary") or l.get("note")) else ""
            lines.append(
                f"• <a href=\"{html.escape(l['url'])}\"><b>{html.escape(title)}</b></a>{star_str}"
                f"{note_part} <code>[ID: {l['id']}]</code>"
            )
    else:
        lines.append("<i>Aucun lien en attente de lecture. Votre Inbox est à zéro ! 🎉</i>")

    lines.extend([
        "\n💡 <b>Astuces :</b>",
        "• Envoyez n'importe quel lien (GitHub, article, outil) pour l'analyser automatiquement.",
        "• Tapez <code>/radar</code> pour découvrir les outils Cyber & Tech tendances du moment.",
    ])

    recent_unread = list(reversed(unread_links[-5:]))
    return {
        "unread_links": recent_unread,
        "unread": recent_unread,
        "message": "\n".join(lines),
    }


def mark_link_as_read(link_id: str) -> bool:
    links = load_inbox_links()
    for l in links:
        if l.get("id") == link_id:
            l["read"] = True
            save_inbox_links(links)
            return True
    return False


async def fetch_cyber_tech_radar(topic: str = "cybersecurity") -> dict:
    """Fetches trending open-source tools on GitHub and top tech news from HackerNews."""
    clean_topic = re.sub(r"^/radar(?:@[a-zA-Z0-9_]+)?\s*", "", topic.strip(), flags=re.IGNORECASE).strip()
    if not clean_topic or clean_topic.lower() in ("veille", "actus", "cyber", "sec", "pentest"):
        gh_query = "topic:security+topic:pentesting+stars:>50"
        display_topic = "Cybersécurité & Pentest"
    elif clean_topic.lower() in ("ai", "ia", "llm", "agent"):
        gh_query = "topic:ai+topic:agent+stars:>100"
        display_topic = "IA & Agents Autonomes"
    elif clean_topic.lower() in ("python", "rust", "linux", "hyprland"):
        gh_query = f"topic:{clean_topic.lower()}+stars:>50"
        display_topic = clean_topic.capitalize()
    else:
        gh_query = f"{clean_topic}+stars:>20"
        display_topic = clean_topic

    repos = []
    hn_stories = []

    async with httpx.AsyncClient(timeout=10.0) as client:
        # 1. Fetch active GitHub repositories pushed recently
        try:
            resp = await client.get(
                f"https://api.github.com/search/repositories?q={gh_query}&sort=updated&order=desc&per_page=5",
                headers={"Accept": "application/vnd.github+json"},
            )
            if resp.status_code == 200:
                for r in resp.json().get("items", [])[:5]:
                    repos.append({
                        "name": r.get("full_name", ""),
                        "url": r.get("html_url", ""),
                        "desc": (r.get("description") or "Outil open-source")[:110],
                        "stars": r.get("stargazers_count", 0),
                        "lang": r.get("language") or "Code",
                    })
        except Exception as e:
            logging.debug(f"Radar GitHub fetch error: {e}")

        # 2. Fetch HackerNews top/security stories via Algolia HN API
        try:
            hn_q = "security OR linux OR open source" if display_topic.startswith("Cyber") else clean_topic
            resp_hn = await client.get(
                "https://hn.algolia.com/api/v1/search_by_date",
                params={"query": hn_q, "tags": "story", "numericFilters": "points>30", "hitsPerPage": 4},
            )
            if resp_hn.status_code == 200:
                for h in resp_hn.json().get("hits", [])[:4]:
                    url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
                    hn_stories.append({
                        "title": h.get("title", "Article"),
                        "url": url,
                        "points": h.get("points", 0),
                    })
        except Exception as e:
            logging.debug(f"Radar HN fetch error: {e}")

    lines = [f"📡 <b>Radar Veille Cyber & Tech — {html.escape(display_topic)}</b>\n"]

    if repos:
        lines.append("🐙 <b>Projets & Outils Open-Source Actifs (GitHub) :</b>")
        for r in repos:
            lines.append(
                f"• <a href=\"{html.escape(r['url'])}\"><b>{html.escape(r['name'])}</b></a> "
                f"(⭐ {r['stars']} • <code>{html.escape(r['lang'])}</code>)\n"
                f"  └ <i>{html.escape(r['desc'])}</i>"
            )
        lines.append("")

    if hn_stories:
        lines.append("📰 <b>Articles & Discussions Techniques (HackerNews) :</b>")
        for s in hn_stories:
            lines.append(
                f"• <a href=\"{html.escape(s['url'])}\">{html.escape(s['title'])}</a> "
                f"(🔥 {s['points']} pts)"
            )
        lines.append("")

    if not repos and not hn_stories:
        lines.append("<i>Impossible de récupérer le flux en direct pour le moment. Vérifiez la connexion réseau.</i>")
    else:
        lines.append("💡 <i>Envoyez l'URL d'un outil dans le chat pour l'analyser, le sauver dans Obsidian ou le cloner !</i>")

    return {"repos": repos, "stories": hn_stories, "message": "\n".join(lines)}
