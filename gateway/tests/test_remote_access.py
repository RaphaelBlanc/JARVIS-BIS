"""Tests de l'accès distant : limitation de débit et métadonnées publiques."""

import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from starlette.requests import Request

os.environ.setdefault("SECRET_KEY", "test-secret-key-123")
os.environ.setdefault("HOME_SERVER_URL", "http://127.0.0.1:8080")

import main  # noqa: E402


def _request(headers=None, client=("127.0.0.1", 4242)) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/status",
            "headers": headers or [],
            "client": client,
        }
    )


class TestRemoteAccess(unittest.TestCase):
    def setUp(self):
        self.previous_limit = main.DESKTOP_RATE_LIMIT
        self.previous_trust = main.TRUST_PROXY_HEADERS
        main.reset_rate_limit()
        self.client = TestClient(main.app)
        self.headers = {"X-Secret-Key": "test-secret-key-123"}

    def tearDown(self):
        main.DESKTOP_RATE_LIMIT = self.previous_limit
        main.TRUST_PROXY_HEADERS = self.previous_trust
        main.reset_rate_limit()

    @patch("gateway.fetch_health", new_callable=AsyncMock)
    @patch("gateway.fetch_session", new_callable=AsyncMock)
    def test_desktop_api_is_rate_limited(self, mock_session, mock_health):
        mock_health.return_value = {"status": "ok"}
        mock_session.return_value = {"turns": 0}
        main.DESKTOP_RATE_LIMIT = 2
        for _ in range(2):
            self.assertEqual(
                self.client.get("/api/status", headers=self.headers).status_code,
                200,
            )
        resp = self.client.get("/api/status", headers=self.headers)
        self.assertEqual(resp.status_code, 429)
        self.assertIn("Retry-After", resp.headers)

    def test_health_is_not_rate_limited(self):
        main.DESKTOP_RATE_LIMIT = 1
        for _ in range(5):
            self.assertEqual(self.client.get("/ping").status_code, 200)
            self.assertEqual(self.client.get("/health").status_code, 200)

    def test_proxy_header_only_trusted_when_configured(self):
        req = _request([(b"cf-connecting-ip", b"203.0.113.9")])
        main.TRUST_PROXY_HEADERS = False
        self.assertEqual(main._client_ip(req), "127.0.0.1")
        main.TRUST_PROXY_HEADERS = True
        self.assertEqual(main._client_ip(req), "203.0.113.9")

    @patch("main.GATEWAY_PUBLIC_URL", "https://assistant.example.test")
    @patch("gateway.fetch_health", new_callable=AsyncMock)
    @patch("gateway.fetch_session", new_callable=AsyncMock)
    def test_status_exposes_configured_public_url(self, mock_session, mock_health):
        mock_health.return_value = {"status": "ok"}
        mock_session.return_value = {"turns": 1}
        resp = self.client.get("/api/status", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()["gateway"]["public_url"], "https://assistant.example.test"
        )


if __name__ == "__main__":
    unittest.main()
