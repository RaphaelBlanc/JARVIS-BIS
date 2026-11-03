"""Tests unitaires pour le client Moodle (support wstoken et MoodleSession)."""

import asyncio
from datetime import datetime
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from core.tools import moodle_client
from core.tools.dispatcher import execute_tool_action


class TestMoodleClient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.patch_url = patch.object(moodle_client, "MOODLE_URL", "https://moodle.example.com")
        self.patch_url.start()

    def tearDown(self):
        self.patch_url.stop()

    def test_is_moodle_configured(self):
        with patch.object(moodle_client, "MOODLE_URL", ""), patch.object(moodle_client, "MOODLE_API_KEY", ""):
            self.assertFalse(moodle_client.is_moodle_configured())
        with patch.object(moodle_client, "MOODLE_URL", "https://moodle.example.com"), patch.object(moodle_client, "MOODLE_API_KEY", ""):
            self.assertFalse(moodle_client.is_moodle_configured())
        with patch.object(moodle_client, "MOODLE_URL", ""), patch.object(moodle_client, "MOODLE_API_KEY", "dummy_key"):
            self.assertFalse(moodle_client.is_moodle_configured())
        with patch.object(moodle_client, "MOODLE_URL", "https://moodle.example.com"), patch.object(moodle_client, "MOODLE_API_KEY", "dummy_key"):
            self.assertTrue(moodle_client.is_moodle_configured())

    def test_is_session_cookie(self):
        # 32 caractères hexadécimaux -> wstoken classique
        self.assertFalse(moodle_client.is_session_cookie("0123456789abcdef0123456789abcdef"))
        self.assertFalse(moodle_client.is_session_cookie("abcdef0123456789abcdef0123456789"))

        # Cookie de session Moodle (chaîne non strictement hexadécimale de 32 chars)
        self.assertTrue(moodle_client.is_session_cookie("fake_moodle_session_test_xyz123"))
        self.assertTrue(moodle_client.is_session_cookie("dummy_cookie_session_abc"))
        self.assertTrue(moodle_client.is_session_cookie("short_token"))

    @patch("httpx.AsyncClient")
    async def test_call_moodle_ajax_cookie_domain_restriction(self, mock_client_cls):
        """Vérifie que le cookie de session Moodle est strictement restreint au domaine configuré."""
        from unittest.mock import MagicMock
        with patch.object(moodle_client, "MOODLE_URL", "https://moodle.example.com"), \
             patch.object(moodle_client, "MOODLE_API_KEY", "dummy_session_cookie"):
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = '{"sesskey":"mock_dummy_sesskey_98765"}'
            mock_client.get.return_value = mock_resp
            mock_post_resp = MagicMock()
            mock_post_resp.json.return_value = [{"data": "ok"}]
            mock_post_resp.raise_for_status.return_value = None
            mock_client.post.return_value = mock_post_resp

            await moodle_client.call_moodle_ajax("core_test_func")

            _, kwargs = mock_client_cls.call_args
            jar = kwargs.get("cookies")
            self.assertIsNotNone(jar)
            cookies_list = list(jar.jar)
            self.assertEqual(len(cookies_list), 1)
            cookie = cookies_list[0]
            self.assertEqual(cookie.name, "MoodleSession")
            self.assertEqual(cookie.value, "dummy_session_cookie")
            self.assertEqual(cookie.domain, "moodle.example.com")

    @patch("core.tools.moodle_client.call_moodle_ws")
    async def test_get_upcoming_assignments_filters_past_and_escapes_html(self, mock_call):
        future_ts = int(datetime.now().timestamp()) + 3600
        past_ts = int(datetime.now().timestamp()) - 3600

        mock_call.return_value = {
            "events": [
                {
                    "name": "Devoir & TP <Special>",
                    "course": {"fullname": "Cours & Travaux <Dirigés>"},
                    "timesort": future_ts,
                    "url": "https://moodle.example.com/mod/assign/view.php?id=10&tab=1",
                },
                {
                    "name": "Ancien Devoir Passé",
                    "course": {"fullname": "Ancien Cours"},
                    "timesort": past_ts,
                    "url": "https://moodle.example.com/mod/assign/view.php?id=5",
                },
            ]
        }
        with patch.object(moodle_client, "MOODLE_API_KEY", "dummy_test_session_cookie"):
            res = await moodle_client.get_upcoming_assignments()
            self.assertTrue(res["success"])
            # L'ancien devoir passé doit être exclu (pas de repli sur le passé)
            self.assertEqual(len(res["assignments"]), 1)
            self.assertEqual(res["assignments"][0]["title"], "Devoir & TP <Special>")
            # Les caractères HTML doivent être échappés dans le message
            self.assertIn("Devoir &amp; TP &lt;Special&gt;", res["message"])
            self.assertIn("Cours &amp; Travaux &lt;Dirigés&gt;", res["message"])
            self.assertNotIn("Ancien Devoir Passé", res["message"])

    @patch("core.tools.moodle_client.call_moodle_ws")
    async def test_get_upcoming_assignments_empty(self, mock_call):
        mock_call.return_value = {"events": []}
        with patch.object(moodle_client, "MOODLE_API_KEY", "dummy_test_session_cookie"):
            res = await moodle_client.get_upcoming_assignments()
            self.assertTrue(res["success"])
            self.assertEqual(len(res["assignments"]), 0)
            self.assertIn("Aucun devoir", res["message"])

    @patch("core.tools.moodle_client.call_moodle_ws")
    async def test_get_enrolled_courses_escapes_html(self, mock_call):
        mock_call.return_value = {
            "courses": [
                {"id": 101, "fullname": "Algorithmique & Graphes <Avancés>"},
                {"id": 102, "fullname": "Maths & Séries"},
            ]
        }
        with patch.object(moodle_client, "MOODLE_API_KEY", "dummy_test_session_cookie"):
            res = await moodle_client.get_enrolled_courses()
            self.assertTrue(res["success"])
            self.assertEqual(len(res["courses"]), 2)
            self.assertIn("Algorithmique &amp; Graphes &lt;Avancés&gt;", res["message"])
            self.assertIn("output", res)

    @patch("core.tools.moodle_client.call_moodle_ws")
    async def test_error_message_sanitizes_sesskey(self, mock_call):
        mock_call.side_effect = Exception("Erreur critique sesskey=mock_dummy_sesskey_98765 dans l'appel")
        with patch.object(moodle_client, "MOODLE_API_KEY", "dummy_test_session_cookie"):
            res = await moodle_client.get_enrolled_courses()
            self.assertFalse(res["success"])
            self.assertNotIn("mock_dummy_sesskey_98765", res["message"])
            self.assertNotIn("mock_dummy_sesskey_98765", res["output"])

    @patch("core.tools.moodle_client.get_enrolled_courses")
    async def test_dispatcher_moodle_cours(self, mock_courses):
        mock_courses.return_value = {"success": True, "message": "Cours ok", "output": "Cours ok", "courses": []}
        with patch.dict(os.environ, {"ENABLE_TOOL_MOODLE": "1"}):
            res = await execute_tool_action("moodle", "cours")
            self.assertTrue(res["success"])
            self.assertEqual(res["message"], "Cours ok")


if __name__ == "__main__":
    unittest.main()
