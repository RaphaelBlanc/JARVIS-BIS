"""Tests unitaires pour le client Moodle EPITA (support wstoken et MoodleSession)."""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from core.tools import moodle_client
from core.tools.dispatcher import execute_tool_action


class TestMoodleClient(unittest.IsolatedAsyncioTestCase):

    def test_is_session_cookie(self):
        # 32 hex chars -> wstoken classique
        self.assertFalse(moodle_client.is_session_cookie("56c3a54b0d46d40d405933cff0bc9a6d"))
        self.assertFalse(moodle_client.is_session_cookie("0123456789abcdef0123456789abcdef"))

        # Cookie de session Moodle (26 chars alphanum avec lettres > f)
        self.assertTrue(moodle_client.is_session_cookie("omgd5j6bc030dp8ccfie476aem"))
        self.assertTrue(moodle_client.is_session_cookie("short_token"))

    @patch("core.tools.moodle_client.call_moodle_ws")
    async def test_get_upcoming_assignments(self, mock_call):
        mock_call.return_value = {
            "events": [
                {
                    "name": "TP1 IA Générative",
                    "course": {"fullname": "IA S1 2025"},
                    "timesort": 1765148400,
                    "url": "https://moodle.epita.fr/mod/assign/view.php?id=123",
                }
            ]
        }
        with patch.object(moodle_client, "MOODLE_API_KEY", "omgd5j6bc030dp8ccfie476aem"):
            res = await moodle_client.get_upcoming_assignments()
            self.assertTrue(res["success"])
            self.assertEqual(len(res["assignments"]), 1)
            self.assertIn("TP1 IA Générative", res["message"])

    @patch("core.tools.moodle_client.call_moodle_ws")
    async def test_get_enrolled_courses(self, mock_call):
        mock_call.return_value = {
            "courses": [
                {"id": 6945, "fullname": "[algo] Graphs"},
                {"id": 6189, "fullname": "Analyse, séries numériques"},
            ]
        }
        with patch.object(moodle_client, "MOODLE_API_KEY", "omgd5j6bc030dp8ccfie476aem"):
            res = await moodle_client.get_enrolled_courses()
            self.assertTrue(res["success"])
            self.assertEqual(len(res["courses"]), 2)
            self.assertIn("[algo] Graphs", res["message"])

    @patch("core.tools.moodle_client.get_enrolled_courses")
    async def test_dispatcher_moodle_cours(self, mock_courses):
        mock_courses.return_value = {"success": True, "message": "Cours ok", "courses": []}
        with patch.dict(os.environ, {"ENABLE_TOOL_MOODLE": "1"}):
            res = await execute_tool_action("moodle", "cours")
            self.assertTrue(res["success"])
            self.assertEqual(res["message"], "Cours ok")


if __name__ == "__main__":
    unittest.main()
