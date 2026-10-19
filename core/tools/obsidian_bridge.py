"""Native Obsidian Vault Bridge for JARVIS.

Connects JARVIS directly to the user's real Obsidian vault:
- /home/cyprien/Documents/Apps/Obsidian/vaults/showcase-vault
Provides:
- Ultra-fast ripgrep (rg) and Python search across all Markdown notes (/kb)
- Clean Telegram HTML rendering of Obsidian notes (wikilinks, callouts, code blocks)
- Intelligent note creation and appending routed to the right category folder (/note)
- Vault statistics and recent notes overview (/vault)
"""

import asyncio
from datetime import datetime
import html
import logging
import os
from pathlib import Path
import re
import shutil

from core.config import OBSIDIAN_VAULT_PATH, DATA_DIR

VAULT_ROOT = OBSIDIAN_VAULT_PATH
FALLBACK_NOTES_MD = DATA_DIR / "notes.md"

FOLDER_KEYWORDS: dict[str, list[str]] = {
    "Web Security": [
        "xss", "sqli", "sql injection", "csrf", "ssrf", "jwt", "cors", "cookie",
        "oauth", "open redirect", "lfi", "rfi", "path traversal", "xxe", "ssti",
        "deserialization", "owasp", "api", "http", "https", "graphql", "web",
    ],
    "Operating Systems": [
        "linux", "windows", "active directory", "kerberos", "suid", "sudo",
        "privilege escalation", "privesc", "rootkit", "persistence", "kernel",
        "registry", "android", "process injection", "cron", "pam", "capabilities",
    ],
    "Cryptography": [
        "crypto", "rsa", "aes", "tls", "ssl", "hash", "hashing", "padding oracle",
        "cipher", "diffie-hellman", "ecc", "hmac", "certificat", "certificate",
    ],
    "Networking": [
        "dns", "tcp", "udp", "ip", "smb", "ldap", "snmp", "arp", "mitm", "bgp",
        "vlan", "vpn", "wireguard", "pivoting", "tunneling", "port forwarding", "zone transfer",
    ],
    "Cloud & DevOps": [
        "aws", "s3", "iam", "azure", "gcp", "kubernetes", "k8s", "docker",
        "container", "terraform", "ci/cd", "cicd", "github actions", "devops", "cloud",
    ],
    "Tools": [
        "nmap", "ffuf", "gobuster", "sqlmap", "burp", "caido", "metasploit",
        "impacket", "bloodhound", "hashcat", "john", "hydra", "wireshark",
        "frida", "objection", "mobsf", "apktool", "tmux", "regex", "reconforge",
    ],
    "Resources": [
        "tryhackme", "thm", "hackthebox", "htb", "root-me", "rootme", "portswigger",
        "gtfobins", "lolbas", "cve", "mitre", "att&ck", "kill chain", "ctf", "writeup",
    ],
    "Ideas": [
        "idée", "idee", "idea", "concept", "mvp", "brainstorm", "startup", "innovation",
    ],
    "Projects": [
        "projet", "project", "roadmap", "architecture", "release", "sprint",
    ],
}


def ensure_vault_exists() -> Path:
    """Ensures the primary Obsidian vault directory exists."""
    VAULT_ROOT.mkdir(parents=True, exist_ok=True)
    return VAULT_ROOT


def infer_vault_folder(text: str) -> str:
    """Infers the best Obsidian subfolder based on keywords in text."""
    t_lower = text.lower()
    best_folder = "Inbox"
    best_hits = 0

    for folder, keywords in FOLDER_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in t_lower)
        if hits > best_hits:
            best_hits = hits
            best_folder = folder

    return best_folder


def list_all_vault_notes() -> list[Path]:
    """Lists all Markdown files in the Obsidian vault (excluding hidden dirs)."""
    if not VAULT_ROOT.exists():
        return []
    notes = []
    for root, dirs, files in os.walk(VAULT_ROOT):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.endswith(".md"):
                notes.append(Path(root) / f)
    return sorted(notes, key=lambda p: p.stat().st_mtime, reverse=True)


async def search_vault(query: str, limit: int = 8) -> dict:
    """Searches the Obsidian vault by filename and content (using ripgrep + Python ranking)."""
    ensure_vault_exists()
    q = query.strip()
    if not q:
        return get_vault_overview()

    q_lower = q.lower()
    q_words = [w for w in re.split(r"\s+", q_lower) if len(w) >= 2]
    all_notes = list_all_vault_notes()

    scored_results: list[dict] = []
    seen_paths: set[str] = set()

    # 1. Filename / Title matches (highest priority)
    for note_path in all_notes:
        rel_path = str(note_path.relative_to(VAULT_ROOT))
        stem_lower = note_path.stem.lower()
        rel_lower = rel_path.lower()

        score = 0
        if q_lower == stem_lower:
            score += 100
        elif q_lower in stem_lower:
            score += 60
        elif all(w in rel_lower for w in q_words):
            score += 40
        elif any(w in stem_lower for w in q_words):
            score += 25

        if score > 0:
            snippet = _extract_preview_snippet(note_path, q_words)
            scored_results.append({
                "path": note_path,
                "rel": rel_path,
                "title": note_path.stem,
                "folder": note_path.parent.name if note_path.parent != VAULT_ROOT else "Racine",
                "score": score,
                "snippet": snippet,
            })
            seen_paths.add(rel_path)

    # 2. Content search via ripgrep (rg) if available, else Python scan
    rg_bin = shutil.which("rg")
    if rg_bin and len(scored_results) < limit:
        try:
            proc = await asyncio.create_subprocess_exec(
                rg_bin, "-i", "-c", "--type", "md", q, str(VAULT_ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=4.0)
            for line in stdout.decode("utf-8", errors="ignore").splitlines():
                if ":" not in line:
                    continue
                f_str, count_str = line.rsplit(":", 1)
                p = Path(f_str)
                if not p.exists():
                    continue
                rel = str(p.relative_to(VAULT_ROOT))
                if rel in seen_paths:
                    continue
                hits = int(count_str) if count_str.isdigit() else 1
                snippet = _extract_preview_snippet(p, q_words)
                scored_results.append({
                    "path": p,
                    "rel": rel,
                    "title": p.stem,
                    "folder": p.parent.name if p.parent != VAULT_ROOT else "Racine",
                    "score": min(35, 10 + hits * 3),
                    "snippet": snippet,
                })
                seen_paths.add(rel)
        except Exception as e:
            logging.debug(f"rg search fallback: {e}")

    # 3. Multi-word Python content scan if still needed
    if len(scored_results) < limit and q_words:
        for note_path in all_notes:
            rel_path = str(note_path.relative_to(VAULT_ROOT))
            if rel_path in seen_paths:
                continue
            try:
                content = note_path.read_text(encoding="utf-8", errors="ignore")
                c_lower = content.lower()
                word_hits = sum(1 for w in q_words if w in c_lower)
                if word_hits > 0:
                    snippet = _extract_preview_snippet(note_path, q_words, content=content)
                    scored_results.append({
                        "path": note_path,
                        "rel": rel_path,
                        "title": note_path.stem,
                        "folder": note_path.parent.name if note_path.parent != VAULT_ROOT else "Racine",
                        "score": word_hits * 10,
                        "snippet": snippet,
                    })
                    seen_paths.add(rel_path)
            except Exception:
                pass

    scored_results.sort(key=lambda x: x["score"], reverse=True)
    top_matches = scored_results[:limit]

    if not top_matches:
        return {
            "found": False,
            "query": q,
            "matches": [],
            "message": (
                f"🔍 <b>Recherche Obsidian :</b> « <code>{html.escape(q)}</code> »\n\n"
                "<i>Aucune fiche correspondante trouvée dans le vault.</i>\n\n"
                f"💡 <b>Astuce :</b> Créez cette note avec :\n"
                f"<code>/note {html.escape(q)} : votre contenu...</code>"
            ),
        }

    for m in top_matches:
        m["rel_path"] = m["rel"]

    lines = [
        f"🧠 <b>Second Cerveau Obsidian — Résultats pour « {html.escape(q)} »</b>",
        f"<i>{len(scored_results)} fiche(s) trouvée(s) dans showcase-vault</i>\n",
    ]
    for idx, m in enumerate(top_matches, 1):
        lines.append(
            f"{idx}. 📄 <b>{html.escape(m['title'])}</b> <i>({html.escape(m['folder'])})</i>\n"
            f"   └ <i>{html.escape(m['snippet'])}</i>"
        )

    lines.append("\n👇 <i>Cliquez sur une fiche ci-dessous pour la lire intégralement :</i>")
    return {
        "found": True,
        "query": q,
        "matches": top_matches,
        "results": top_matches,
        "message": "\n".join(lines),
    }


def _extract_preview_snippet(note_path: Path, query_words: list[str], content: str | None = None) -> str:
    """Extracts a clean 1-line preview snippet around the first matching keyword."""
    try:
        text = content if content is not None else note_path.read_text(encoding="utf-8", errors="ignore")
        clean_lines = [
            l.strip() for l in text.splitlines()
            if l.strip() and not l.strip().startswith("---") and not l.strip().startswith("# ")
        ]
        for line in clean_lines:
            l_low = line.lower()
            if any(w in l_low for w in query_words):
                cleaned = re.sub(r"[*_`#>\[\]]+", "", line).strip()
                return cleaned[:110] + ("..." if len(cleaned) > 110 else "")
        if clean_lines:
            cleaned = re.sub(r"[*_`#>\[\]]+", "", clean_lines[0]).strip()
            return cleaned[:110] + ("..." if len(cleaned) > 110 else "")
    except Exception:
        pass
    return "Fiche Markdown Obsidian"


def read_vault_note(rel_or_title: str) -> dict:
    """Reads an Obsidian note by relative path or title and formats it for Telegram."""
    ensure_vault_exists()
    target_str = rel_or_title.strip()
    candidate = (VAULT_ROOT / target_str).resolve()

    if not (candidate.exists() and candidate.is_file() and str(candidate).startswith(str(VAULT_ROOT.resolve()))):
        # Search by stem or partial match
        all_notes = list_all_vault_notes()
        candidate = next(
            (p for p in all_notes if p.stem.lower() == target_str.lower() or str(p.relative_to(VAULT_ROOT)).lower() == target_str.lower()),
            None,
        )
        if not candidate:
            candidate = next(
                (p for p in all_notes if target_str.lower() in p.stem.lower()),
                None,
            )

    if not candidate or not candidate.exists():
        return {
            "ok": False,
            "message": f"⚠️ Fiche Obsidian « <code>{html.escape(target_str)}</code> » introuvable.",
        }

    try:
        raw_md = candidate.read_text(encoding="utf-8", errors="replace")
        rel_path = str(candidate.relative_to(VAULT_ROOT))
        folder = candidate.parent.name if candidate.parent != VAULT_ROOT else "Racine"
        mtime = datetime.fromtimestamp(candidate.stat().st_mtime).strftime("%d/%m/%Y %H:%M")

        escaped_body = html.escape(raw_md)
        escaped_body = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"<b>\2</b>", escaped_body)
        escaped_body = re.sub(r"\[\[([^\]]+)\]\]", r"<b>\1</b>", escaped_body)
        if len(escaped_body) > 3300:
            escaped_body = escaped_body[:3300] + "\n\n<i>... [Fiche tronquée pour Telegram]</i>"

        header = (
            f"📖 <b>{html.escape(candidate.stem)}</b>\n"
            f"📁 <code>{html.escape(rel_path)}</code> • 🕒 <i>{mtime}</i>\n"
            f"{'─' * 28}\n\n"
        )
        full_msg = header + escaped_body
        return {
            "ok": True,
            "title": candidate.stem,
            "rel": rel_path,
            "rel_path": rel_path,
            "folder": folder,
            "raw_markdown": raw_md,
            "display_markdown": full_msg,
            "message": full_msg,
            "path": candidate,
        }
    except Exception as e:
        return {"ok": False, "message": f"⚠️ Erreur de lecture : {e}"}


def create_or_append_vault_note(
    raw_input: str,
    explicit_folder: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Creates or appends to a structured Markdown note inside the Obsidian vault.

    Supports formats:
    - "Titre : Contenu de la note..."
    - "Folder/Titre : Contenu..."
    - Free-form text (auto-titled and placed in the inferred folder or Inbox)
    """
    ensure_vault_exists()
    clean = raw_input.strip()
    clean = re.sub(r"^/(?:note|obs|obsidian)(?:@[a-zA-Z0-9_]+)?\s*", "", clean, flags=re.IGNORECASE).strip()

    title = ""
    body = clean
    folder = explicit_folder

    if ":" in clean and len(clean.split(":", 1)[0].strip()) <= 55:
        left, right = clean.split(":", 1)
        left = left.strip()
        right = right.strip()
        if "/" in left:
            f_part, t_part = left.split("/", 1)
            folder = f_part.strip()
            title = t_part.strip()
        else:
            title = left
        body = right

    if not folder:
        folder = infer_vault_folder(f"{title} {body}")

    now = datetime.now()
    if not title:
        # Generate clean title from first words
        words = re.sub(r"[^\w\séèêëàâôûîïç\-]", "", body).strip().split()[:5]
        title = " ".join(words).capitalize() if words else f"Note_{now.strftime('%Y%m%d_%H%M')}"

    safe_title = re.sub(r'[<>:"/\\|?*]', "-", title).strip()
    target_dir = VAULT_ROOT / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    note_path = target_dir / f"{safe_title}.md"

    auto_tags = list(tags or [])
    folder_tag = re.sub(r"[^\w]", "", folder.lower())
    if folder_tag and folder_tag not in auto_tags:
        auto_tags.append(folder_tag)
    if "jarvis" not in auto_tags:
        auto_tags.append("jarvis")
    tag_line = " ".join(f"#{t}" for t in auto_tags)

    is_append = note_path.exists()
    timestamp_str = now.strftime("%d/%m/%Y à %H:%M")

    if is_append:
        entry = (
            f"\n\n## Mis à jour le {timestamp_str}\n"
            f"{tag_line}\n\n"
            f"{body}\n"
        )
        with open(note_path, "a", encoding="utf-8") as f:
            f.write(entry)
    else:
        entry = (
            f"# {safe_title}\n\n"
            f"**Catégorie :** [[{folder}]] | **Créé le :** {timestamp_str}\n"
            f"**Tags :** {tag_line}\n\n"
            f"---\n\n"
            f"{body}\n"
        )
        with open(note_path, "w", encoding="utf-8") as f:
            f.write(entry)

    # Also mirror to data/notes.md for backward compatibility
    try:
        FALLBACK_NOTES_MD.parent.mkdir(parents=True, exist_ok=True)
        with open(FALLBACK_NOTES_MD, "a", encoding="utf-8") as fb:
            fb.write(f"\n### {safe_title} [{timestamp_str}] ({folder})\n{tag_line}\n\n{body}\n\n---\n")
    except Exception:
        pass

    rel_path = str(note_path.relative_to(VAULT_ROOT))
    action_lbl = "complétée" if is_append else "créée"
    msg = (
        f"🧠 <b>Fiche Obsidian {action_lbl} avec succès !</b>\n\n"
        f"• 📄 <b>Titre :</b> <b>{html.escape(safe_title)}</b>\n"
        f"• 📂 <b>Pôle :</b> <code>{html.escape(folder)}</code>\n"
        f"• 📍 <b>Chemin :</b> <code>{html.escape(rel_path)}</code>\n"
        f"• 🏷️ <b>Tags :</b> <i>{html.escape(tag_line)}</i>"
    )
    return {
        "ok": True,
        "title": safe_title,
        "folder": folder,
        "rel": rel_path,
        "rel_path": rel_path,
        "path": str(note_path),
        "appended": is_append,
        "tags": auto_tags,
        "message": msg,
    }


def get_vault_overview() -> dict:
    """Returns a complete overview of the Obsidian vault categories and latest notes."""
    ensure_vault_exists()
    all_notes = list_all_vault_notes()
    folders: dict[str, int] = {}
    for p in all_notes:
        f_name = p.parent.name if p.parent != VAULT_ROOT else "Racine"
        folders[f_name] = folders.get(f_name, 0) + 1

    lines = [
        "🧠 <b>Second Cerveau Obsidian (showcase-vault)</b>",
        f"• <b>Emplacement :</b> <code>~/Documents/Apps/Obsidian/vaults/showcase-vault</code>",
        f"• <b>Total de fiches :</b> <b>{len(all_notes)} notes Markdown</b>\n",
        "📂 <b>Répartition par Pôle :</b>",
    ]

    for fname, count in sorted(folders.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"• <b>{html.escape(fname)}</b> : {count} fiches")

    recent = all_notes[:5]
    if recent:
        lines.append("\n🕒 <b>5 dernières fiches modifiées :</b>")
        for r in recent:
            rel = str(r.relative_to(VAULT_ROOT))
            mtime = datetime.fromtimestamp(r.stat().st_mtime).strftime("%d/%m %H:%M")
            lines.append(f"• <b>{html.escape(r.stem)}</b> (<code>{html.escape(rel)}</code>) — <i>{mtime}</i>")

    lines.extend([
        "\n💡 <b>Commandes & Langage naturel :</b>",
        "• Chercher : <code>/kb kerberos</code> ou <i>« Cherche dans mes notes XSS »</i>",
        "• Créer/Noter : <code>/note Nmap : astuce scan furtif...</code>",
    ])

    return {
        "found": True,
        "total": len(all_notes),
        "folders": folders,
        "recent": [
            {
                "title": r.stem,
                "rel": str(r.relative_to(VAULT_ROOT)),
                "folder": r.parent.name if r.parent != VAULT_ROOT else "Racine",
            }
            for r in recent
        ],
        "matches": [
            {
                "title": r.stem,
                "rel": str(r.relative_to(VAULT_ROOT)),
                "folder": r.parent.name if r.parent != VAULT_ROOT else "Racine",
            }
            for r in recent
        ],
        "message": "\n".join(lines),
    }
