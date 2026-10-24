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
                res_vault = await obsidian_bridge.search_vault(q, limit=int(params.get("limit", 5)))
                results = res_vault.get("results", [])
                if not results:
                    return {"success": True, "message": res_vault.get("message", f"🔍 Aucune note trouvée pour : <i>{q}</i>"), "results": []}
                return {"success": True, "message": res_vault.get("message", ""), "results": results}

            elif action in ("note", "create"):
                text = params.get("text", "")
                note_res = obsidian_bridge.create_or_append_vault_note(text)
                return {
                    "success": True,
                    "message": note_res.get("message", f"📝 Note enregistrée dans : <code>{note_res.get('rel', '')}</code>"),
                    "note": note_res,
                }

            elif action in ("overview", "vault"):
                stats = obsidian_bridge.get_vault_overview()
                msg = stats.get("message") or (
                    f"🧠 <b>Statistiques du Coffre Obsidian :</b>\n\n"
                    f"• <b>Total notes :</b> {stats.get('total_notes', stats.get('total', 0))}\n"
                    f"• <b>Dossiers :</b> {len(stats.get('folders', {}))}\n"
                )
                return {"success": True, "message": msg, "stats": stats}

        # ── FOCUS & DEEP WORK ────────────────────────────────────────────────────
        elif tool_id == "focus":
            from core.tools import focus_manager
            if action == "start":
                raw_text = params.get("raw_text")
                if raw_text:
                    duration_m, topic = focus_manager.parse_focus_input(str(raw_text))
                else:
                    duration_m = int(params.get("duration", 25))
                    topic = params.get("topic", "Travail")
                msg = f"🎯 <b>Session Focus démarrée !</b>\n• <b>Durée :</b> {duration_m} min\n• <b>Sujet :</b> {topic}"
                return {"success": True, "message": msg, "session": {"duration": duration_m, "topic": topic}}

            elif action == "stats":
                stats = focus_manager.get_productivity_stats()
                msg = stats.get("message") or (
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
                res_ideas = idea_incubator.format_ideas_list()
                return {
                    "success": True,
                    "message": res_ideas.get("message", "Aucune idée.") if isinstance(res_ideas, dict) else str(res_ideas),
                    "ideas": res_ideas.get("ideas", []) if isinstance(res_ideas, dict) else [],
                }

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
                card = universal_inbox.format_enriched_link_card(res)
                return {"success": True, "message": card, "item": res}

        # ── TRAVEL ADVISOR ───────────────────────────────────────────────────────
        elif tool_id == "travel":
            from core.tools import travel_advisor
            if action == "plan":
                raw_text = params.get("text")
                if not raw_text:
                    dest = params.get("destination", "Paris")
                    target_time = params.get("time", "")
                    raw_text = f"{dest} à {target_time}" if target_time else dest
                parsed = travel_advisor.parse_depart_request(str(raw_text)) or {"destination": str(raw_text), "target_time": None}
                plan = travel_advisor.calculate_departure_plan(parsed)
                return {"success": True, "message": plan.get("message", ""), "plan": plan}

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

        # ── NODES & MULTI-PC ─────────────────────────────────────────────────────
        elif tool_id == "nodes":
            from core.node_hub import NODE_HUB
            if action in ("list", "status"):
                nodes = NODE_HUB.list_nodes()
                if not nodes:
                    return {"success": True, "message": "ℹ️ Aucun nœud client connecté actuellement.", "nodes": []}
                msg = f"🖥️ <b>Nœuds clients connectés ({len(nodes)}) :</b>\n\n"
                for n in nodes:
                    msg += f"• <b>{n['node_id']}</b> ({n['hostname']}) — {n['os_name']}\n  Activité: il y a {n['idle_seconds']}s\n"
                return {"success": True, "message": msg, "nodes": nodes}

            elif action in ("exec", "run_shell"):
                if os.getenv("ENABLE_NODE_EXEC", "0").lower() not in ("1", "true", "yes"):
                    return {
                        "success": False,
                        "error": "L'exécution distante de commandes (exec) sur les nœuds clients est désactivée par défaut par sécurité. Définissez ENABLE_NODE_EXEC=1 côté serveur pour l'autoriser.",
                        "message": "⚠️ <b>Exécution distante désactivée</b> : l'exécution de commandes shell sur les machines clientes requiert ENABLE_NODE_EXEC=1 côté serveur JARVIS.",
                    }
                node_id = params.get("node_id") or params.get("node")
                if not node_id or str(node_id).strip().lower() in ("", "auto", "default", "any"):
                    return {
                        "success": False,
                        "error": "node_id obligatoire : le ciblage implicite/auto est interdit pour exec par sécurité.",
                        "message": "⚠️ Précisez la machine cible (ex: node_id='laptop').",
                    }
                cmd = str(params.get("command", "")).strip()
                timeout = float(params.get("timeout", 60.0))
                res = await NODE_HUB.exec_cmd(cmd, node_id=node_id, timeout=timeout)
                return {
                    "success": res.get("success", False),
                    "message": res.get("message") or res.get("error", ""),
                    "result": res,
                }

            elif action == "read_file":
                path = str(params.get("path", "")).strip()
                node_id = params.get("node_id") or params.get("node")
                res = await NODE_HUB.read_file(path, node_id=node_id, timeout=35.0)
                return {
                    "success": res.get("success", False),
                    "message": f"Fichier {path} lu ({res.get('size', 0)} octets)." if res.get("success") else res.get("error", ""),
                    "result": res,
                }

            elif action == "write_file":
                node_id = params.get("node_id") or params.get("node")
                if not node_id or str(node_id).strip().lower() in ("", "auto", "default", "any"):
                    return {
                        "success": False,
                        "error": "node_id obligatoire : le ciblage implicite/auto est interdit pour write_file par sécurité.",
                        "message": "⚠️ Précisez la machine cible (ex: node_id='laptop').",
                    }
                path = str(params.get("path", "")).strip()
                content = str(params.get("content", ""))
                res = await NODE_HUB.write_file(path, content, node_id=node_id, timeout=35.0)
                return {
                    "success": res.get("success", False),
                    "message": f"Fichier {path} écrit avec succès." if res.get("success") else res.get("error", ""),
                    "result": res,
                }

            elif action == "screenshot":
                node_id = params.get("node_id") or params.get("node")
                res = await NODE_HUB.send_action(node_id, "screenshot", params, timeout=35.0)
                return {
                    "success": res.get("success", False),
                    "message": "Capture d'écran reçue." if res.get("success") else res.get("error", ""),
                    "result": res,
                }

        return {"success": False, "error": f"Action '{action}' non reconnue pour le module '{tool_id}'."}

    except Exception as exc:
        logger.error(f"Erreur dispatcher [{tool_id}.{action}]: {exc}", exc_info=True)
        return {"success": False, "error": str(exc), "message": f"❌ Erreur lors de l'exécution : {exc}"}
