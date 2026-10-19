"""Tests des endpoints de l'API Desktop (/api/ask, /api/status, /api/briefing, /api/reminders)."""

import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

# Configure la clé secrète pour les tests
os.environ["SECRET_KEY"] = "test-secret-key-123"
os.environ["HOME_SERVER_URL"] = "http://127.0.0.1:8080"

import main
import reminders


class TestDesktopAPI(unittest.TestCase):
    def setUp(self):
        reminders.init_db()
        self.client = TestClient(main.app)
        self.auth_headers = {"X-Secret-Key": "test-secret-key-123"}

    def test_ask_without_secret_fails(self):
        resp = self.client.post("/api/ask", json={"prompt": "Bonjour"})
        self.assertEqual(resp.status_code, 401)

    @patch("gateway.forward_to_runner", new_callable=AsyncMock)
    def test_ask_success(self, mock_forward):
        mock_forward.return_value = {
            "success": True,
            "output": "Bonjour depuis le runner !",
            "error": None,
        }
        resp = self.client.post(
            "/api/ask",
            json={
                "prompt": "Bonjour",
                "platform": "desktop",
                "user_id": "test_user",
            },
            headers=self.auth_headers,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["output"], "Bonjour depuis le runner !")
        mock_forward.assert_awaited_once()

    @patch("gateway.fetch_health", new_callable=AsyncMock)
    @patch("gateway.fetch_session", new_callable=AsyncMock)
    def test_status_endpoint(self, mock_session, mock_health):
        mock_health.return_value = {"status": "ok", "version": "1.0.0"}
        mock_session.return_value = {"model": "gemini", "turns": 2}

        resp = self.client.get("/api/status", headers=self.auth_headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["online"])
        self.assertEqual(data["runner"]["status"], "ok")
        self.assertEqual(data["session"]["turns"], 2)

    @patch("main.get_briefing_text", new_callable=AsyncMock)
    def test_briefing_endpoint(self, mock_briefing):
        mock_briefing.return_value = {
            "success": True,
            "output": "Voici le briefing du jour.",
            "cached": True,
        }
        resp = self.client.post(
            "/api/briefing",
            json={"force_refresh": False},
            headers=self.auth_headers,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["output"], "Voici le briefing du jour.")
        self.assertTrue(data["cached"])

    def test_reminder_desktop_platform(self):
        resp = self.client.post(
            "/api/reminders",
            json={
                "platform": "desktop",
                "chat_id": "noctalia_user",
                "message": "Faire une pause café",
                "delay": "15m",
            },
            headers=self.auth_headers,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["reminder"]["platform"], "desktop")
