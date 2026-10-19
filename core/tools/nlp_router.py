"""Ultra-fast local intent classifier and natural language router for JARVIS.

Detects user intent locally (0 token, < 5ms) for BOTH text and transcribed voice messages
to route everyday and engineering requests directly to native features:
- Obsidian Second Brain (/kb, /note, /vault)
- Deep Work & Productivity (/focus, /stats)
- Idea Incubator (/idea, /ideas)
- Project Factory & DevOps (/proj, /dev, /newproj, /ship)
- Cyber & Tech Radar (/radar)
- Universal Inbox (URLs & notes)
- Expenses & Budget (/depense, /depenses, /budget)
- Shopping list (/courses)
- Reminders & Recurring routines (/remind, /cron)
- Travel departure advisor (/depart)
- System & Desktop control (/pc, /screenshot, /cam, /lock, /guard, /git, /briefing)
Falls back to the AI pipeline when complex open-ended reasoning or Workspace MCP tools are needed.
"""

import re


def classify_natural_input(text: str) -> dict:
    """Classifies user text into an intent with extracted parameters (0 token execution)."""
    t = text.strip()
    t_lower = t.lower()

    # 1. URL / Link capture (if message is primarily a link + optional short comment)
    urls = re.findall(r"https?://[^\s]+", t)
    if urls and len(t.split()) <= len(urls) + 8:
        note = re.sub(r"https?://[^\s]+", "", t).strip()
        note = re.sub(r"^(?:sauvegarde|enregistre|garde|ajoute|inbox|lien)\s*(?:ce\s+lien)?\s*[:=-]?\s*", "", note, flags=re.IGNORECASE).strip()
        return {"intent": "inbox_link", "url": urls[0], "note": note}

    # 2. Obsidian Note Creation ("Note dans Obsidian: ...", "Fiche Obsidian ...", "Note: ...")
    if re.match(r"^(?:note\s+dans\s+obsidian|ajoute\s+(?:une\s+)?(?:note|fiche)\s+(?:dans\s+)?obsidian|fiche\s+obsidian|note\s*[:=]|pense\s+[àa]\s*[:=]?)\s*", t_lower):
        clean_note = re.sub(
            r"^(?:note\s+dans\s+obsidian|ajoute\s+(?:une\s+)?(?:note|fiche)\s+(?:dans\s+)?obsidian|fiche\s+obsidian|note\s*[:=]|pense\s+[àa]\s*[:=]?)\s*(?:sur\s+)?",
            "",
            t,
            flags=re.IGNORECASE,
        ).strip()
        return {"intent": "obsidian_note", "text": clean_note}

    # 3. Obsidian Knowledge Base Search ("Cherche dans mes notes ...", "Qu'est-ce que j'ai noté sur ...", "Montre ma fiche sur ...")
    m_kb = re.match(
        r"^(?:cherche|recherche|trouve|montre|affiche|lis|ouvre)\s+(?:dans\s+)?(?:mes\s+notes(?:\s+obsidian)?|mon\s+(?:vault\s+)?obsidian|ma\s+fiche(?:\s+obsidian)?|la\s+fiche|la\s+note)\s+(?:sur\s+|de\s+|concernant\s+)?(.+)$",
        t,
        re.IGNORECASE,
    )
    if m_kb:
        return {"intent": "obsidian_search", "query": m_kb.group(1).strip(" ?!.")}

    m_kb2 = re.match(
        r"^(?:qu'est-ce\s+que\s+j'ai\s+not[eé]|que\s+disent\s+mes\s+notes)\s+(?:dans\s+obsidian\s+)?(?:sur|à\s+propos\s+de)\s+(.+)$",
        t,
        re.IGNORECASE,
    )
    if m_kb2:
        return {"intent": "obsidian_search", "query": m_kb2.group(1).strip(" ?!.")}

    if any(p in t_lower for p in ("état de mon vault", "statistiques obsidian", "combien de notes dans obsidian", "ouvre mon vault obsidian")):
        return {"intent": "obsidian_overview"}

    # 4. Idea Incubator ("J'ai une idée : ...", "Idée de projet : ...", "Liste mes idées")
    if re.match(r"^(?:liste|montre|affiche|voir)\s+(?:mes\s+|toutes\s+mes\s+)?id[eé]es(?:\s+de\s+projets?)?", t_lower) or "backlog d'idées" in t_lower or "backlog d idees" in t_lower:
        return {"intent": "ideas_list"}

    m_idea = re.match(
        r"^(?:j['\s]?ai\s+une\s+id[eé]e(?:\s+de\s+projet|\s+d['\s]?outil|\s+de\s+startup)?|id[eé]e\s+de\s+projet|id[eé]e\s+d['\s]?outil|nouvelle\s+id[eé]e|incube\s+(?:cette\s+)?id[eé]e|note\s+cette\s+id[eé]e)\s*[:=-]?\s*(.+)$",
        t,
        re.IGNORECASE,
    )
    if m_idea:
        return {"intent": "idea_incubate", "text": m_idea.group(1).strip()}

    # 5. Project Factory: New Project Scaffolding ("Crée un nouveau projet appelé ...", "Initialise un projet ...")
    m_newproj = re.match(
        r"^(?:cr[eé]e|initialise|g[eé]n[eè]re|lance)\s+un\s+(?:nouveau\s+)?projet\s+(?:python|cli|cyber|web|fastapi)?\s*(?:appel[eé]|nomm[eé])?\s*([a-zA-Z0-9_\-]+)(?:\s+(?:en\s+|avec\s+le\s+template\s+)?(python-cli|cyber-tool|fastapi|web-ts|c-rust))?(?:\s+(.+))?$",
        t,
        re.IGNORECASE,
    )
    if m_newproj:
        return {
            "intent": "project_new",
            "name": m_newproj.group(1).strip(),
            "template": (m_newproj.group(2) or "python-cli").lower(),
            "description": (m_newproj.group(3) or "").strip(),
        }

    # 6. Project Factory: Coding Agent Delegation ("Code sur ReconForge : ...", "Dans le projet ReconForge ajoute ...")
    m_dev = re.match(
        r"^(?:code|d[eé]veloppe|impl[eé]mente|corrige|ajoute|modifie)\s+(?:sur|dans)\s+(?:le\s+projet\s+)?([a-zA-Z0-9_\-]+)\s*[:,-]?\s*(.+)$",
        t,
        re.IGNORECASE,
    )
    if m_dev:
        return {
            "intent": "project_dev",
            "project": m_dev.group(1).strip(),
            "prompt": m_dev.group(2).strip(),
        }

    m_dev2 = re.match(
        r"^(?:dans|sur)\s+(?:le\s+projet\s+|mon\s+projet\s+)?(reconforge|jarvis|cyber|portfolio|[a-zA-Z0-9_\-]+)\s*[,:-]?\s*((?:ajoute|impl[eé]mente|corrige|modifie|cr[eé]e|refactorise|analyse|optimise)\s+.+)$",
        t,
        re.IGNORECASE,
    )
    if m_dev2:
        return {
            "intent": "project_dev",
            "project": m_dev2.group(1).strip(),
            "prompt": m_dev2.group(2).strip(),
        }

    # 7. Project Factory: Run Project Tests ("Lance les tests de ReconForge", "Teste le projet ...")
    m_test = re.match(
        r"^(?:lance|ex[eé]cute|fais\s+tourner|v[eé]rifie)\s+les\s+tests(?:\s+unitaires)?\s+(?:de|du\s+projet|sur)\s+([a-zA-Z0-9_\-]+)$",
        t,
        re.IGNORECASE,
    )
    if m_test:
        return {"intent": "project_test", "project": m_test.group(1).strip(" ?!.")}

    # 8. Project Factory: Ship / LinkedIn Post ("Génère un post LinkedIn pour ReconForge", "Prépare la release de ...")
    m_ship = re.match(
        r"^(?:g[eé]n[eè]re|r[eé]dige|pr[eé]pare|[eé]cris)\s+(?:un\s+post\s+linkedin|le\s+post\s+linkedin|la\s+release|le\s+kit\s+de\s+lancement)\s+(?:pour|sur|de|du\s+projet)\s+(.+)$",
        t,
        re.IGNORECASE,
    )
    if m_ship:
        proj_t = m_ship.group(1).strip(" ?!. ")
        return {"intent": "project_ship", "project": proj_t, "target": proj_t}

    # 9. Project Factory: Project Status / Overview ("Où en est ReconForge ?", "Statut de mes projets", "Mes projets")
    m_proj_single = re.match(
        r"^(?:o[uù]\s+en\s+est|statut\s+de|état\s+de|cockpit\s+de|ouvre\s+le\s+projet|roadmap\s+de)\s+(?:mon\s+projet\s+|le\s+projet\s+)?([a-zA-Z0-9_\-]+)\s*\??$",
        t,
        re.IGNORECASE,
    )
    if m_proj_single and m_proj_single.group(1).lower() not in ("mes", "mon", "la", "le", "pc"):
        return {"intent": "project_cockpit", "project": m_proj_single.group(1).strip()}

    if any(p in t_lower for p in ("statut de mes projets", "état de mes projets", "liste mes projets", "affiche mes projets", "cockpit projets", "où en sont mes projets")):
        return {"intent": "projects_overview"}

    # 10. Focus / Deep Work & Stats ("Lance un focus de 45m sur ...", "Arrête le focus", "Mes stats de productivité")
    if re.match(r"^(?:arr[eê]te|stoppe|termine|annule)\s+(?:le\s+)?(?:focus|pomodoro|mode\s+concentration)", t_lower):
        return {"intent": "focus_control", "text": "stop"}

    if re.match(r"^(?:lance|d[eé]marre|active|mets)\s+(?:un\s+)?(?:mode\s+)?(?:focus|pomodoro|session\s+de\s+concentration)", t_lower) or t_lower.startswith("pomodoro ") or t_lower.startswith("focus "):
        clean_f = re.sub(r"^(?:lance|d[eé]marre|active|mets)\s+(?:un\s+)?(?:mode\s+)?(?:focus|pomodoro|session\s+de\s+concentration)\s*(?:de\s+|sur\s+|pour\s+)?", "", t, flags=re.IGNORECASE).strip()
        return {"intent": "focus_control", "text": clean_f}

    if any(p in t_lower for p in ("combien de temps j'ai travaillé", "mes stats de productivité", "statistiques de travail", "bilan de productivité", "temps de focus")):
        return {"intent": "productivity_stats"}

    # 11. Cyber & Tech Radar ("Veille cyber", "Actus tech du jour", "Nouveaux outils pentest")
    if any(p in t_lower for p in ("veille cyber", "veille tech", "radar cyber", "actus cyber", "actus pentest", "nouveaux outils cyber", "outils github sécurité", "tendances github")):
        topic = "cyber"
        if "ia" in t_lower or "ai" in t_lower:
            topic = "ai"
        elif "python" in t_lower:
            topic = "python"
        elif "rust" in t_lower:
            topic = "rust"
        return {"intent": "radar", "topic": topic}

    # 12. Memory storage
    if re.match(r"^(?:retiens\s+que|rappelle-toi\s+que|m[eé]morise\s+que|note\s+bien\s+que)\s+", t_lower):
        clean_mem = re.sub(r"^(?:retiens\s+que|rappelle-toi\s+que|m[eé]morise\s+que|note\s+bien\s+que)\s+", "", t, flags=re.IGNORECASE).strip()
        return {"intent": "memory_add", "text": clean_mem}

    # 13. Recurring tasks (/cron)
    if re.match(r"^(?:tous\s+les|chaque)\s+", t_lower) and any(
        w in t_lower for w in ("jour", "matin", "soir", "mois", "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
    ):
        return {"intent": "cron_add", "text": t}

    # 14. Departure travel calculation
    if any(k in t_lower for k in ("heure de partir", "quand partir", "partir pour", "partir à", "trajet vers")):
        m_dest = re.search(
            r"(?:pour|vers|[àa])\s+([a-zA-Z0-9\séèêëàâôûîïç\-]+?)(?:\s+(?:[àa]|pour)\s+(\d{1,2}(?:h\d*|:\d*)?))?$",
            t,
            re.IGNORECASE,
        )
        if m_dest:
            return {"intent": "depart", "text": t}

    # 15. Shopping list items
    if re.match(r"^(?:ajoute|rajoute|mets|ach[eè]te|acheter|prends|prendre)\s+(?:du|de\s+la|des|de|un|une)?\s*(.+?)\s+(?:[àa]\s+la\s+liste|aux\s+courses|sur\s+les\s+courses)$", t_lower):
        m_c = re.match(r"^(?:ajoute|rajoute|mets|ach[eè]te|acheter|prends|prendre)\s+(?:du|de\s+la|des|de|un|une)?\s*(.+?)\s+(?:[àa]\s+la\s+liste|aux\s+courses|sur\s+les\s+courses)$", t, re.IGNORECASE)
        if m_c:
            return {"intent": "courses_add", "items": m_c.group(1).strip()}

    if re.match(r"^(?:ach[eè]te|acheter|prends|prendre)\s+(?:du|de\s+la|des|de|un|une)\s+(.+)$", t_lower):
        m_c = re.match(r"^(?:ach[eè]te|acheter|prends|prendre)\s+(?:du|de\s+la|des|de|un|une)\s+(.+)$", t, re.IGNORECASE)
        if m_c:
            return {"intent": "courses_add", "items": m_c.group(1).strip()}

    if t_lower.startswith("courses:") or t_lower.startswith("course:"):
        items = re.sub(r"^courses?\s*[:=]?\s*", "", t, flags=re.IGNORECASE).strip()
        return {"intent": "courses_add", "items": items}

    if any(p in t_lower for p in ("liste de courses", "affiche ma liste de courses", "montre ma liste de courses", "montre les courses", "voir les courses")):
        return {"intent": "courses_list"}

    # 16. Reminders
    if re.match(r"^(?:rappelle-moi\s+(?:de\s+|d')?|rappel\s*[:=]?\s*)\s*", t_lower):
        clean_rem = re.sub(r"^(?:rappelle-moi\s+(?:de\s+|d')?|rappel\s*[:=]?\s*)\s*", "", t, flags=re.IGNORECASE).strip()
        clean_rem = re.sub(r"^(dans\s+\d+\s*(?:s|sec|min|m|h|heures?))\s+(?:de\s+|d')", r"\1 ", clean_rem, flags=re.IGNORECASE)
        return {"intent": "reminder_add", "text": clean_rem}

    # 17. Natural expense
    if any(w in t_lower for w in ("payé", "acheté", "achat", "dépensé", "dépense", "coûté")):
        m_amt = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:€|euros?)?", t, re.IGNORECASE)
        if m_amt:
            try:
                amt_val = float(m_amt.group(1).replace(",", "."))
                merchant_desc = re.sub(r"^(?:dépense|dépensé|payé|acheté|achat|j'ai payé)\s*", "", t, flags=re.IGNORECASE)
                merchant_desc = re.sub(r"(\d+(?:[.,]\d+)?)\s*(?:€|euros?)?", "", merchant_desc, flags=re.IGNORECASE).strip()
                return {"intent": "expense_record", "amount": amt_val, "merchant": merchant_desc or "Dépense", "text": t}
            except ValueError:
                pass

    if any(p in t_lower for p in ("bilan de mes dépenses", "combien j'ai dépensé", "mes dépenses du mois", "bilan dépenses")):
        return {"intent": "expenses_summary"}

    if any(p in t_lower for p in ("où en est mon budget", "statut de mon budget", "mon budget du mois")):
        return {"intent": "budget_status"}

    # 18. Fast System & Daily Briefing intents in natural language
    if any(p in t_lower for p in ("fais-moi mon briefing", "fais moi mon briefing", "briefing du matin", "briefing du jour", "résumé de ma journée", "cockpit du jour")):
        return {"intent": "briefing_now"}

    if any(p in t_lower for p in ("prends un screenshot", "prends une capture d'écran", "prends une capture d écran", "prends une capture d'ecran", "capture d'écran du pc", "montre mon écran")):
        return {"intent": "system_screenshot"}

    if any(p in t_lower for p in ("prends une photo webcam", "capture webcam", "qui est devant mon pc")):
        return {"intent": "system_cam"}

    if any(p in t_lower for p in ("verrouille le pc", "verrouille ma session", "lock le pc")):
        return {"intent": "system_lock"}

    if any(p in t_lower for p in ("active le mode sentinelle", "arme le mode sentinelle", "surveille mon pc")):
        return {"intent": "guard_on"}

    if any(p in t_lower for p in ("désactive le mode sentinelle", "désarme le mode sentinelle", "arrête la sentinelle")):
        return {"intent": "guard_off"}

    if any(p in t_lower for p in ("état de mes dépôts git", "statut git de mes projets", "scanne mes repos git")):
        return {"intent": "system_git"}

    if any(p in t_lower for p in ("état de mon pc", "diagnostic du pc", "santé de mon pc", "batterie du pc")):
        return {"intent": "system_pc"}

    # Fallback to AI pipeline
    return {"intent": "ai_query", "text": t}
