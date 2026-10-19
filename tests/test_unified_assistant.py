"""
tests/test_unified_assistant.py — Tests unitaires pour l'Assistant Personnel Unifié.

Valide :
- L'enregistrement et la configuration des outils dans ToolRegistry
- Le routeur NLP 0-token
- Le dispatcher universel d'outils
- Le HardwareRelay hybride (local vs pc-agent)
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

    async def test_hardware_relay_server_without_agent(self):
        """Vérifie que le mode serveur sans agent PC connecté échoue proprement."""
        relay = HardwareRelay()
        with patch.dict(os.environ, {"DEPLOY_MODE": "server"}):
            ok, msg, data = await relay.execute("screenshot")
            self.assertFalse(ok)
            self.assertIn("hors ligne", msg.lower())

    async def test_hardware_relay_server_with_agent(self):
        """Vérifie la transmission de requête à un agent PC connecté en mode serveur."""
        relay = HardwareRelay()
        mock_ws = AsyncMock()

        # Simule l'envoi et la réponse immédiate de l'agent
        async def fake_send_json(payload):
            req_id = payload.get("request_id")
            relay.handle_agent_response({
                "request_id": req_id,
                "success": True,
                "message": "Capture réussie via l'agent.",
                "data": {"image_base64": "fake_png_data"},
            })

        mock_ws.send_json = fake_send_json
        relay.register_agent("test-agent-1", mock_ws)

        with patch.dict(os.environ, {"DEPLOY_MODE": "server"}):
            ok, msg, data = await relay.execute("screenshot", timeout=2.0)
            self.assertTrue(ok)
            self.assertEqual(msg, "Capture réussie via l'agent.")
            self.assertEqual(data.get("image_base64"), "fake_png_data")


if __name__ == "__main__":
    unittest.main()
