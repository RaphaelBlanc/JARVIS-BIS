"""Tests de la configuration et de l'état du briefing matinal."""

import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import briefing


class TestBriefingConfig(unittest.TestCase):
    def test_parse_days(self):
        self.assertEqual(briefing.parse_days("mon,tue,wed,thu,fri"), {0, 1, 2, 3, 4})
        self.assertEqual(briefing.parse_days("lundi, dimanche"), {0, 6})
        self.assertEqual(briefing.parse_days("0,6"), {0, 6})
        self.assertEqual(briefing.parse_days("inconnu"), set())

    def test_parse_sections(self):
        self.assertEqual(
            briefing.parse_sections("agenda, email, inconnue"),
            {"agenda", "email"},
        )

    def test_should_send_now(self):
        tz = ZoneInfo("Europe/Paris")
        monday = datetime.datetime(2026, 10, 19, 8, 2, tzinfo=tz)
        monday_late = datetime.datetime(2026, 10, 19, 8, 6, tzinfo=tz)
        sunday = datetime.datetime(2026, 10, 25, 8, 2, tzinfo=tz)
        with (
            patch.object(briefing, "BRIEFING_ENABLED", True),
            patch.object(briefing, "BRIEFING_DAYS", {0, 1, 2, 3, 4}),
            patch.object(briefing, "BRIEFING_HOUR", 8),
            patch.object(briefing, "BRIEFING_MINUTE", 0),
            patch.object(briefing, "BRIEFING_WINDOW_MINUTES", 5),
            patch.object(briefing, "BRIEFING_TIMEZONE", tz),
        ):
            self.assertTrue(briefing.should_send_now(monday))
            self.assertFalse(briefing.should_send_now(monday_late))
            self.assertFalse(briefing.should_send_now(sunday))
            with patch.object(briefing, "BRIEFING_ENABLED", False):
                self.assertFalse(briefing.should_send_now(monday))

    def test_prompt_sections_and_city(self):
        prompt = briefing.build_briefing_prompt(
            sections={"weather", "tasks"}, city="Toulouse"
        )
        self.assertIn("Toulouse", prompt)
        self.assertIn("TÂCHES EN COURS", prompt)
        self.assertNotIn("E-MAILS IMPORTANTS", prompt)
        self.assertNotIn("ACTUALITÉS", prompt)

    def test_prompt_all_sections(self):
        prompt = briefing.build_briefing_prompt(
            sections={"agenda", "news", "email", "weather", "tasks"},
            city="Lyon",
        )
        for title in (
            "EMPLOI DU TEMPS",
            "ACTUALITÉS",
            "E-MAILS",
            "MÉTÉO",
            "TÂCHES",
        ):
            self.assertIn(title, prompt)


class TestBriefingState(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "briefing_state.json"
        self.patch = patch.object(briefing, "STATE_FILE", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp_dir.cleanup()

    def test_platform_tracking(self):
        date_key = "2026-10-19"
        self.assertEqual(briefing.pending_platforms(date_key), ["telegram", "discord"])
        briefing.mark_platform_sent(date_key, "telegram")
        self.assertEqual(briefing.pending_platforms(date_key), ["discord"])
        briefing.mark_platform_sent(date_key, "discord")
        self.assertEqual(briefing.pending_platforms(date_key), [])
        self.assertTrue(self.path.exists())


class TestBriefingCache(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "briefing_cache.json"
        self.patch_file = patch.object(briefing, "CACHE_FILE", self.path)
        self.patch_ttl = patch.object(briefing, "BRIEFING_CACHE_TTL_MINUTES", 120)
        self.patch_file.start()
        self.patch_ttl.start()

    def tearDown(self):
        self.patch_file.stop()
        self.patch_ttl.stop()
        self.temp_dir.cleanup()

    def test_cache_round_trip(self):
        self.assertIsNone(briefing.get_cached_briefing())
        briefing._save_cache("Briefing du jour")
        self.assertEqual(briefing.get_cached_briefing(), "Briefing du jour")

    def test_expired_cache_is_ignored(self):
        briefing._save_cache("Vieux briefing")
        data = briefing._load_cache()
        data["generated_at"] -= 121 * 60
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(briefing.get_cached_briefing())

    def test_ttl_zero_disables_cache(self):
        briefing._save_cache("Briefing du jour")
        with patch.object(briefing, "BRIEFING_CACHE_TTL_MINUTES", 0):
            self.assertIsNone(briefing.get_cached_briefing())


if __name__ == "__main__":
    unittest.main()
