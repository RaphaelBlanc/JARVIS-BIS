"""
tests/test_unified_assistant.py — Tests unitaires pour l'Assistant Personnel Unifié.

Valide :
- L'enregistrement et la configuration des outils dans ToolRegistry
- Le routeur NLP 0-token
- Le HardwareRelay (exécution directe des outils matériels)
"""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from core.tools.base import ToolRegistry
from core.tools.dispatcher import execute_tool_action
from core.tools.nlp_router import classify_natural_input
from core.hardware_relay import HardwareRelay


class TestUnifiedAssistant(unittest.IsolatedAsyncioTestCase):

    def test_tool_registry(self):
        """Vérifie que tous les outils majeurs sont bien enregistrés."""
        tools = [t.id for t in ToolRegistry.list_tools()]
        self.assertIn("system_pc", tools)
        self.assertIn("moodle", tools)
        self.assertIn("expenses", tools)
        self.assertIn("shopping", tools)
        self.assertIn("obsidian", tools)
        self.assertIn("focus", tools)
        self.assertIn("ideas", tools)
        self.assertIn("projects", tools)
        self.assertIn("inbox", tools)
        self.assertIn("travel", tools)

    def test_nlp_router_intents(self):
        """Vérifie la détection d'intentions sans token."""
        # Courses
        res = classify_natural_input("Achète du lait, des oeufs et du pain")
        self.assertEqual(res.get("intent"), "courses_add")

        res_list = classify_natural_input("Montre ma liste de courses")
        self.assertEqual(res_list.get("intent"), "courses_list")

        # Dépenses
        res_exp = classify_natural_input("Dépense 15.50 repas du midi")
        self.assertEqual(res_exp.get("intent"), "expense_record")
        self.assertEqual(res_exp.get("amount"), 15.50)

        # Obsidian search
        res_kb = classify_natural_input("Cherche dans mes notes obsidian sur linux kernel")
        self.assertEqual(res_kb.get("intent"), "obsidian_search")
        self.assertIn("linux kernel", res_kb.get("query", ""))

        # Focus / Pomodoro
        res_focus = classify_natural_input("Lance un focus de 30m sur Projet ReconForge")
        self.assertEqual(res_focus.get("intent"), "focus_control")

        # Capture de lien Inbox
        res_url = classify_natural_input("Garde ce lien: https://github.com/torvalds/linux super kernel")
        self.assertEqual(res_url.get("intent"), "inbox_link")
        self.assertEqual(res_url.get("url"), "https://github.com/torvalds/linux")

    async def test_dispatcher_disabled_tool(self):
        """Vérifie le message d'avertissement quand un outil est désactivé."""
        with patch.dict(os.environ, {"ENABLE_TOOL_SHOPPING": "0"}):
            res = await execute_tool_action("shopping", "list")
            self.assertFalse(res.get("success"))
            self.assertIn("désactivé", res.get("message", ""))

    async def test_dispatcher_enabled_shopping(self):
        """Vérifie l'exécution d'un outil activé."""
        with patch.dict(os.environ, {"ENABLE_TOOL_SHOPPING": "1"}):
            res = await execute_tool_action("shopping", "list")
            self.assertTrue(res.get("success"))
            self.assertIn("courses", res.get("message", "").lower())

    async def test_hardware_relay_execution(self):
        """Vérifie l'exécution d'une action via HardwareRelay."""
        relay = HardwareRelay()
        with patch("core.tools.system_tools.lock_desktop_session", return_value=(True, "Session verrouillée")):
            ok, msg, data = await relay.execute("lock")
            self.assertTrue(ok)
            self.assertEqual(msg, "Session verrouillée")

    async def test_nc_management_capabilities(self):
        """Vérifie le schéma et les capacités retournées à Noctalia."""
        from gateway.nc_management import dispatch
        res = await dispatch("capabilities", {}, False)
        self.assertEqual(res.get("protocol"), 1)
        self.assertIn("modules", res)
        self.assertIn("fields", res)
        module_ids = [m["id"] for m in res["modules"]]
        self.assertIn("shopping", module_ids)
        self.assertIn("expenses", module_ids)

    def test_nc_engines_snapshot(self):
        """Vérifie la gestion des moteurs du runner."""
        import sys
        from pathlib import Path
        runner_dir = str(Path(__file__).resolve().parent.parent / "home-runner")
        if runner_dir not in sys.path:
            sys.path.insert(0, runner_dir)
        import nc_engines
        self.assertIn("agy", nc_engines.ENGINES)
        self.assertIn("codex", nc_engines.ENGINES)
        self.assertIn("claude", nc_engines.ENGINES)
        self.assertIn("opencode", nc_engines.ENGINES)

    async def test_hardware_relay_extended_actions(self):
        """Vérifie les actions corrigées de HardwareRelay (windows, kill, git_summary, hardware_health)."""
        relay = HardwareRelay()
        with patch("core.tools.system_tools.list_hyprland_windows", return_value=[{"workspace": 1, "class": "kitty", "title": "zsh", "pid": 1234}]):
            ok, msg, _ = await relay.execute("windows")
            self.assertTrue(ok)
            self.assertIn("kitty", msg)

        with patch("core.tools.system_tools.terminate_process", new_callable=AsyncMock, return_value=(True, "Processus arrêté")):
            ok, msg, _ = await relay.execute("kill", {"target": "firefox"})
            self.assertTrue(ok)
            self.assertEqual(msg, "Processus arrêté")

        with patch("core.tools.system_tools.check_hardware_health", return_value=([], {"cpu_percent": 12.0})):
            ok, msg, data = await relay.execute("hardware_health")
            self.assertTrue(ok)
            self.assertEqual(data.get("metrics", {}).get("cpu_percent"), 12.0)

    async def test_dispatcher_extended_tools(self):
        """Vérifie le dispatcher pour focus, ideas et travel."""
        with patch.dict(os.environ, {"ENABLE_TOOL_FOCUS": "1", "ENABLE_TOOL_IDEAS": "1", "ENABLE_TOOL_TRAVEL": "1"}):
            res_ideas = await execute_tool_action("ideas", "list")
            self.assertTrue(res_ideas.get("success"))

            res_focus = await execute_tool_action("focus", "stats")
            self.assertTrue(res_focus.get("success"))

            res_travel = await execute_tool_action("travel", "plan", {"raw_args": "18h30 Gare Matabiau"})
            self.assertTrue(res_travel.get("success"))

    async def test_nc_management_session_key_propagation(self):
        """Vérifie que session_key est bien propagé par nc_management.dispatch."""
        from gateway.nc_management import dispatch
        with patch("gateway.nc_management.runner", new_callable=AsyncMock) as mock_runner:
            mock_runner.return_value = {"status": "updated"}
            await dispatch("session.update", {"model": "gemini-3.8-flash-high"}, True, session_key="desktop:tour")
            mock_runner.assert_called_once()
            _, path, body = mock_runner.call_args[0]
            self.assertEqual(path, "/session")
            self.assertEqual(body.get("session_key"), "desktop:tour")

    async def test_agy_stateless_preserves_persistent_worker(self):
        """Vérifie qu'un appel continue_session=False ne détruit pas le worker persistant actif."""
        import sys
        from pathlib import Path
        runner_dir = str(Path(__file__).resolve().parent.parent / "home-runner")
        if runner_dir not in sys.path:
            sys.path.insert(0, runner_dir)
        import agy_bridge

        class DummyWorker:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

        dummy = DummyWorker()
        agy_bridge._WORKERS["desktop:test"] = dummy
        try:
            with patch.object(agy_bridge, "_run_agy_oneshot", new_callable=AsyncMock) as mock_oneshot:
                mock_oneshot.return_value = {"success": True, "output": "ok"}
                res = await agy_bridge.run_agy(
                    "Météo Toulouse",
                    continue_session=False,
                    reset_session_flag=False,
                    session_key="desktop:test",
                )
                self.assertTrue(res.get("success"))
                self.assertFalse(dummy.stopped)
                self.assertIs(agy_bridge._WORKERS.get("desktop:test"), dummy)
        finally:
            agy_bridge._WORKERS.pop("desktop:test", None)


if __name__ == "__main__":
    unittest.main()

