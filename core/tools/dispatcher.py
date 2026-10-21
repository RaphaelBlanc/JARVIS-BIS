"""
core/tools/dispatcher.py — Dispatcher universel pour exécuter les outils.

Fournit des fonctions d'exécution uniques pour tous les modules (Moodle, Budget,
Courses, Obsidian, Focus, Idées, Projets, Système PC, etc.), utilisables de façon
transparente par :
  1. Le bot Telegram
  2. Le bot Discord
  3. L'API Noctalia / Desktop (/api/action, /api/ask)
  4. Le routeur NLP 0-token
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config import DATA_DIR
from core.hardware_relay import HARDWARE_RELAY
from core.tools.base import ToolRegistry
import core.tools.definitions  # noqa: F401 (Enregistre tous les outils)

logger = logging.getLogger("core.dispatcher")


async def execute_tool_action(
    tool_id: str,
    action: str,
    params: Optional[Dict[str, Any]] = None,
    platform: str = "generic",  # "telegram", "discord", "desktop", "generic"
) -> Dict[str, Any]:
    """Point d'entrée universel pour exécuter une action d'un module/tool."""
    params = params or {}

    # 1. Vérifie si le module est activé
    if not ToolRegistry.is_tool_enabled(tool_id):
        return {
            "success": False,
            "error": f"Le module '{tool_id}' est actuellement désactivé. Activez-le dans le fichier .env (ENABLE_TOOL_{tool_id.upper()}=1).",
            "message": f"⚠️ Le module <b>{tool_id}</b> est désactivé.",
        }

    tool = ToolRegistry.get_tool(tool_id)

    # 2. Si le module requiert le matériel local, passer par HARDWARE_RELAY
    if tool and tool.requires_local_hardware:
        ok, msg, data = await HARDWARE_RELAY.execute(action, params)
        return {
            "success": ok,
            "message": msg,
            "data": data or {},
        }

    # 3. Exécution métier selon le module
    try:
        # ── MOODLE EPITA ─────────────────────────────────────────────────────────
        if tool_id == "moodle":
            from core.tools import moodle_client
            if action in ("devoirs", "assignments"):
                res = await moodle_client.get_upcoming_assignments()
                return res

        # ── FINANCES & BUDGET ────────────────────────────────────────────────────
        elif tool_id == "expenses":
            from core.tools import expense_tracker
            if action == "add":
                amt = float(params.get("amount", 0.0))
                desc = params.get("description", "Dépense")
                cat = params.get("category")
                item = expense_tracker.add_expense(amt, desc, cat, source=platform)
                # Vérifie alerte budget
                budget_status = expense_tracker.load_budget()
                monthly = expense_tracker.get_monthly_summary()
                msg = (
                    f"💳 <b>Dépense enregistrée :</b> {item['amount']:.2f} €\n"
                    f"• <b>Commerçant :</b> {item['description']}\n"
                    f"• <b>Catégorie :</b> {item['category']}\n"
                    f"• <b>Total du mois :</b> {monthly['total']:.2f} €"
                )
                return {"success": True, "message": msg, "item": item, "summary": monthly}

            elif action in ("summary", "bilan"):
                summary = expense_tracker.get_monthly_summary()
                msg = (
                    f"📊 <b>Bilan financier ({summary['month']}) :</b>\n\n"
                    f"• <b>Total dépensé :</b> <code>{summary['total']:.2f} €</code>\n"
                    f"• <b>Nombre de dépenses :</b> {summary['count']}\n"
                )
                if summary.get("by_category"):
                    msg += "\n<b>Par catégorie :</b>\n"
                    for cat, amt in summary["by_category"].items():
                        msg += f"  • {cat} : <code>{amt:.2f} €</code>\n"
                return {"success": True, "message": msg, "summary": summary}

            elif action == "budget":
                sub = params.get("subaction", "status")
                if sub == "status":
                    cfg = expense_tracker.load_budget()
                    monthly = expense_tracker.get_monthly_summary()
                    cap = float(cfg.get("limit") or cfg.get("monthly_cap") or 0)
                    if cap <= 0:
                        return {"success": True, "message": "ℹ️ Aucun plafond de budget mensuel configuré."}
                    spent = monthly["total"]
                    pct = min(100.0, (spent / cap) * 100) if cap > 0 else 0
                    msg = (
                        f"💳 <b>Budget Mensuel :</b>\n\n"
                        f"• <b>Dépensé :</b> <code>{spent:.2f} €</code> / <code>{cap:.2f} €</code> ({pct:.1f}%)\n"
                        f"• <b>Reste :</b> <code>{max(0.0, cap - spent):.2f} €</code>\n"
                    )
                    return {"success": True, "message": msg, "budget": cfg}
                elif sub == "set":
                    cap = float(params.get("cap", 500.0))
                    res = expense_tracker.set_monthly_budget(cap)
                    return {"success": True, "message": f"✅ Budget mensuel défini à {cap:.2f} €.", "budget": res}

        # ── COURSES (SHOPPING LIST) ──────────────────────────────────────────────
        elif tool_id == "shopping":
            from core.tools import shopping_list
            if action == "list":
                status = shopping_list.format_courses_status()
                return {"success": True, "message": status["text"], "items": status}

            elif action == "add":
                raw_text = params.get("text", "")
                items = shopping_list.add_course_items(raw_text)
                status = shopping_list.format_courses_status()
                return {"success": True, "message": f"🛒 Articles ajoutés !\n\n{status['text']}", "items": items}

            elif action == "toggle":
                item_id = params.get("item_id", "")
                items, toggled = shopping_list.toggle_course_item(item_id)
                status = shopping_list.format_courses_status()
                return {"success": True, "message": status["text"], "items": items, "toggled": toggled}

            elif action == "clear_done":
                count = shopping_list.clear_done_courses()
                status = shopping_list.format_courses_status()
                return {"success": True, "message": f"🧹 {count} article(s) cochés retirés.\n\n{status['text']}"}

            elif action == "clear_all":
                count = shopping_list.clear_all_courses()
                return {"success": True, "message": f"🗑️ {count} article(s) retirés. Liste entièrement vidée."}

        # ── OBSIDIAN BRIDGE ──────────────────────────────────────────────────────
        elif tool_id == "obsidian":
            from core.tools import obsidian_bridge
            if action in ("search", "kb"):
                q = params.get("query", "")
                results = obsidian_bridge.search_vault(q, max_results=params.get("limit", 5))
                if not results:
                    return {"success": True, "message": f"🔍 Aucune note trouvée pour : <i>{q}</i>"}
                msg = f"🧠 <b>Notes Obsidian trouvées ({len(results)}) :</b>\n\n"
                for r in results:
                    msg += f"📄 <b>{r['title']}</b> (<code>{r['folder']}</code>)\n{r.get('preview', '')}\n\n"
                return {"success": True, "message": msg, "results": results}

            elif action in ("note", "create"):
                text = params.get("text", "")
                note_file = obsidian_bridge.create_or_append_vault_note(text)
                return {"success": True, "message": f"📝 Note enregistrée dans : <code>{note_file.name}</code>"}

            elif action in ("overview", "vault"):
                stats = obsidian_bridge.get_vault_overview()
                msg = (
                    f"🧠 <b>Statistiques du Coffre Obsidian :</b>\n\n"
                    f"• <b>Total notes :</b> {stats['total_notes']}\n"
                    f"• <b>Dossiers :</b> {len(stats.get('folders', {}))}\n"
                )
                return {"success": True, "message": msg, "stats": stats}

        # ── FOCUS & DEEP WORK ────────────────────────────────────────────────────
        elif tool_id == "focus":
            from core.tools import focus_manager
            if action == "start":
                duration_m = int(params.get("duration", 25))
                topic = params.get("topic", "Travail")
                session = focus_manager.parse_focus_input(f"{duration_m}m {topic}")
                msg = f"🎯 <b>Session Focus démarrée !</b>\n• <b>Durée :</b> {duration_m} min\n• <b>Sujet :</b> {topic}"
                return {"success": True, "message": msg, "session": session}

            elif action == "stats":
                stats = focus_manager.get_productivity_stats()
                msg = (
                    f"📊 <b>Statistiques Deep Work :</b>\n\n"
                    f"• <b>Temps total :</b> {stats.get('total_hours', 0):.1f} h\n"
                    f"• <b>Sessions terminées :</b> {stats.get('sessions_count', 0)}\n"
                    f"• <b>Série actuelle :</b> {stats.get('streak_days', 0)} jour(s) 🔥\n"
                )
                return {"success": True, "message": msg, "stats": stats}

        # ── IDEAS INCUBATOR ──────────────────────────────────────────────────────
        elif tool_id == "ideas":
            from core.tools import idea_incubator
            if action == "list":
                ideas = idea_incubator.load_ideas()
                msg = idea_incubator.format_ideas_list(ideas)
                return {"success": True, "message": msg, "ideas": ideas}

            elif action == "incubate":
                raw_text = params.get("text", "")
                idea = await idea_incubator.incubate_idea(raw_text)
                card = idea_incubator.format_idea_card(idea)
                return {"success": True, "message": card, "idea": idea}

        # ── PROJECTS & DEV AGENT ─────────────────────────────────────────────────
        elif tool_id == "projects":
            from core.tools import project_manager
            if action == "list":
                projs = project_manager.load_projects_registry()
                msg = "📁 <b>Cockpit Projets :</b>\n\n"
                for p in projs.values():
                    msg += f"• <b>{p.get('name', 'Sans nom')}</b> : {p.get('status', 'actif')}\n"
                return {"success": True, "message": msg, "projects": projs}

            elif action == "cockpit":
                name = params.get("name", "")
                proj = project_manager.resolve_project(name)
                if not proj:
                    return {"success": False, "message": f"Projet '{name}' introuvable."}
                return {"success": True, "message": f"📁 <b>Projet {proj.get('name')}</b>\nChemin: <code>{proj.get('path')}</code>", "project": proj}

        # ── UNIVERSAL INBOX & TECH RADAR ─────────────────────────────────────────
        elif tool_id == "inbox":
            from core.tools import universal_inbox
            if action == "radar":
                topic = params.get("topic", "cyber")
                data = await universal_inbox.fetch_cyber_tech_radar(topic)
                return {"success": True, "message": data.get("message", "Pas d'actualités."), "data": data}

            elif action == "capture":
                url = params.get("url", "")
                note = params.get("note", "")
                res = await universal_inbox.capture_and_enrich_link(url, note)
                return {"success": True, "message": f"🌐 Lien capturé dans l'Inbox : {url}", "item": res}

        # ── TRAVEL ADVISOR ───────────────────────────────────────────────────────
        elif tool_id == "travel":
            from core.tools import travel_advisor
            if action == "plan":
                dest = params.get("destination", "Paris")
                target_time = params.get("time", "08:00")
                plan = travel_advisor.calculate_departure_plan(dest, target_time)
                msg = (
                    f"🗺️ <b>Trajet vers {dest} :</b>\n\n"
                    f"• <b>Heure d'arrivée cible :</b> {target_time}\n"
                    f"• <b>Durée estimée :</b> {plan.get('duration_min', 30)} min (+ {plan.get('margin_min', 10)} min de marge)\n"
                    f"• <b>Heure de départ conseillée :</b> ⏰ <b>{plan.get('departure_time', '07:20')}</b>"
                )
                return {"success": True, "message": msg, "plan": plan}

        # ── SECURITY PIN ─────────────────────────────────────────────────────────
        elif tool_id == "security_pin":
            from core.tools import security
            user_id = int(params.get("user_id", 0))
            if action == "status":
                is_auth = security.is_user_authenticated(user_id)
                is_cfg = security.is_pin_configured()
                return {
                    "success": True,
                    "configured": is_cfg,
                    "authenticated": is_auth,
                    "message": "🔒 Authentifié" if is_auth else "🔒 Verrouillé",
                }

        return {"success": False, "error": f"Action '{action}' non reconnue pour le module '{tool_id}'."}

    except Exception as exc:
        logger.error(f"Erreur dispatcher [{tool_id}.{action}]: {exc}", exc_info=True)
        return {"success": False, "error": str(exc), "message": f"❌ Erreur lors de l'exécution : {exc}"}
