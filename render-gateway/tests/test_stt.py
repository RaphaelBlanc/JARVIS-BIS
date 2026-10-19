"""Tests du module STT : langue automatique, préchauffage et limites."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import stt


class FakeModel:
    def __init__(self):
        self.kwargs = None

    def transcribe(self, path, **kwargs):
        self.kwargs = kwargs
        return (
            [SimpleNamespace(text=" bonjour "), SimpleNamespace(text=" le monde ")],
            SimpleNamespace(language="fr", language_probability=0.98),
        )


class TestSTT(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.audio = Path(self.temp_dir.name) / "voice.ogg"
        self.audio.write_bytes(b"fake-audio")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_resolve_language(self):
        self.assertIsNone(stt._resolve_language("auto"))
        self.assertIsNone(stt._resolve_language(""))
        self.assertEqual(stt._resolve_language("FR"), "fr")
        with patch.object(stt, "STT_LANGUAGE", "en"):
            self.assertEqual(stt._resolve_language(None), "en")

    def test_local_language_auto(self):
        model = FakeModel()
        with patch.object(stt, "_get_local_model", return_value=model):
            text, detected = stt._transcribe_local_sync(self.audio, None)
        self.assertEqual(text, "bonjour le monde")
        self.assertEqual(detected, "fr")
        self.assertNotIn("language", model.kwargs)

    def test_local_forced_language(self):
        model = FakeModel()
        with patch.object(stt, "_get_local_model", return_value=model):
            stt._transcribe_local_sync(self.audio, "en")
        self.assertEqual(model.kwargs["language"], "en")

    async def test_transcribe_auto_uses_local(self):
        model = FakeModel()
        with (
            patch.object(stt, "GROQ_API_KEY", ""),
            patch.object(stt, "OPENAI_API_KEY", ""),
            patch.object(stt, "_get_local_model", return_value=model),
            patch.object(stt, "LOCAL_WHISPER_MODEL", "base"),
            patch.object(stt, "STT_LANGUAGE", "auto"),
        ):
            text, engine = await stt.transcribe_audio(self.audio)
        self.assertEqual(text, "bonjour le monde")
        self.assertIn("local-whisper-base", engine)
        self.assertIn("fr", engine)

    async def test_too_large_audio(self):
        with patch.object(stt, "MAX_AUDIO_BYTES", 1):
            text, error = await stt.transcribe_audio(self.audio)
        self.assertIsNone(text)
        self.assertIn("trop volumineux", error)

    async def test_warmup_loads_model(self):
        with (
            patch.object(stt, "GROQ_API_KEY", ""),
            patch.object(stt, "OPENAI_API_KEY", ""),
            patch.object(stt, "_get_local_model", return_value=FakeModel()) as loader,
        ):
            await stt.warmup_stt()
        loader.assert_called_once()


if __name__ == "__main__":
    unittest.main()
