"""Intelligent Mail Sorter for JARVIS.

Sorts and triages incoming Gmail messages into 4 intuitive categories:
- 🔴 Urgent / Important (requires prompt attention)
- 🟡 À Répondre (messages awaiting a reply)
- 🔵 Info & EPITA (school updates, notifications, platform digests)
- ⚪ Newsletters & Publicités (marketing, promo)
Provides one-click response draft generation, archiving, or snooze.
"""

from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re


def categorize_email(sender: str, subject: str, snippet: str = "") -> dict:
    """Categorizes an email using high-speed deterministic heuristic rules (0 token)."""
    text_corpus = f"{sender} {subject} {snippet}".lower()

    # Category 1: Publicités / Newsletters
    promo_keywords = [
        "newsletter", "offre", "promo", "soldes", "désabonner", "unsubscribe",
        "remise", "vente privée", "bon plan", "no-reply", "noreply", "notification@"
    ]
    if any(k in text_corpus for k in promo_keywords):
        return {
            "tag": "⚪ Newsletters & Pubs",
            "priority": 4,
            "badge": "Pub/Notif",
            "action_needed": False,
        }

    # Category 2: Études & EPITA
    epita_keywords = [
        "epita", "moodle", "cri", "ionis", "pedago", "tp", "soutenance",
        "qcm", "devoir", "prof", "enseignant", "campus"
    ]
    if any(k in text_corpus for k in epita_keywords):
        return {
            "tag": "🔵 Info & EPITA",
            "priority": 3,
            "badge": "EPITA",
            "action_needed": True,
        }

    # Category 3: Urgent / Important
    urgent_keywords = [
        "urgent", "important", "action requise", "immédiat", "deadline",
        "sécurité", "alerte", "retard", "convocation", "attestation", "facture"
    ]
    if any(k in text_corpus for k in urgent_keywords):
        return {
            "tag": "🔴 Urgent & Important",
            "priority": 1,
            "badge": "URGENT",
            "action_needed": True,
        }

    # Category 4: Default Personal / A Répondre
    return {
        "tag": "🟡 À Répondre / Personnel",
        "priority": 2,
        "badge": "À traiter",
        "action_needed": True,
    }


def format_mail_triage_summary(emails: list[dict]) -> str:
    """Formats emails classified into a clean structured Telegram summary."""
    if not emails:
        return (
            "✉️ <b>Tri Intelligent de vos Emails</b>\n\n"
            "<i>Aucun nouvel email non lu trouvé. Boîte de réception à jour ! 🎉</i>"
        )

    categorized: dict[str, list[dict]] = {}
    for m in emails:
        cat_info = categorize_email(
            m.get("sender", ""),
            m.get("subject", ""),
            m.get("snippet", "")
        )
        tag = cat_info["tag"]
        m["category_info"] = cat_info
        categorized.setdefault(tag, []).append(m)

    lines = [
        "✉️ <b>Tri Intelligent de vos Emails</b>",
        f"• <b>Total non lus analysés :</b> {len(emails)}\n",
    ]

    tag_order = [
        "🔴 Urgent & Important",
        "🟡 À Répondre / Personnel",
        "🔵 Info & EPITA",
        "⚪ Newsletters & Pubs",
    ]

    for tag in tag_order:
        items = categorized.get(tag, [])
        if not items:
            continue
        lines.append(f"<b>{tag} ({len(items)}) :</b>")
        for mail in items:
            s_name = mail.get("sender", "Inconnu").split("<")[0].strip()[:20]
            subj = mail.get("subject", "Sans objet")[:35]
            m_id = mail.get("id", "")
            lines.append(f"• <b>{s_name}</b> : <i>{subj}</i> <code>[ID: {m_id}]</code>")
        lines.append("")

    lines.append("💡 <i>Cliquez sur les boutons ci-dessous pour traiter ou préparer un brouillon en 1 clic.</i>")
    return "\n".join(lines)
