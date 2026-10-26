"""Tests pour la gestion des pièces jointes et fichiers reçus par le home-runner."""

import base64
import os
import shutil
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

os.environ["SECRET_KEY"] = "test-secret-key-123"

import main


class TestFileAttachments(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.auth_headers = {"X-Secret-Key": "test-secret-key-123"}
        self.upload_dir = Path("/tmp/assistant_uploads")
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    @patch("main.run_agy", new_callable=AsyncMock)
    def test_run_with_single_text_file(self, mock_run):
        mock_run.return_value = {
            "success": True,
            "output": "Fichier texte analysé.",
            "duration_ms": 100,
        }
        content = "print('Hello world!')\n"
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")

        resp = self.client.post(
            "/run",
            json={
                "prompt": "Explique ce code",
                "files": [
                    {
                        "filename": "script.py",
                        "content_base64": b64,
                        "mime_type": "text/x-python",
                    }
                ],
            },
            headers=self.auth_headers,
        )
        self.assertEqual(resp.status_code, 200)
        mock_run.assert_awaited_once()
        active_prompt = mock_run.call_args[1]["prompt"]
        self.assertIn("[Fichier fourni par l'utilisateur: /tmp/assistant_uploads/", active_prompt)
        self.assertIn("script.py]", active_prompt)
        self.assertIn("Explique ce code", active_prompt)

    @patch("main.run_agy", new_callable=AsyncMock)
    def test_run_with_image_file(self, mock_run):
        mock_run.return_value = {
            "success": True,
            "output": "Image reçue.",
            "duration_ms": 100,
        }
        b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii")

        resp = self.client.post(
            "/run",
            json={
                "prompt": "Décris cette image",
                "files": [
                    {
                        "filename": "capture.png",
                        "content_base64": b64,
                        "mime_type": "image/png",
                    }
                ],
            },
            headers=self.auth_headers,
        )
        self.assertEqual(resp.status_code, 200)
        mock_run.assert_awaited_once()
        active_prompt = mock_run.call_args[1]["prompt"]
        self.assertIn("[Image fournie par l'utilisateur: /tmp/assistant_uploads/", active_prompt)
        self.assertIn("capture.png]", active_prompt)

    def test_run_rejects_too_many_files(self):
        b64 = base64.b64encode(b"test").decode("ascii")
        files = [
            {"filename": f"file_{i}.txt", "content_base64": b64}
            for i in range(6)
        ]
        resp = self.client.post(
            "/run",
            json={
                "prompt": "Trop de fichiers",
                "files": files,
            },
            headers=self.auth_headers,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Trop de fichiers joints", resp.json()["detail"])

    def test_run_rejects_invalid_base64(self):
        resp = self.client.post(
            "/run",
            json={
                "prompt": "Bad base64",
                "files": [
                    {"filename": "bad.txt", "content_base64": "invalid!base64!!"}
                ],
            },
            headers=self.auth_headers,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("base64 invalide", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()
