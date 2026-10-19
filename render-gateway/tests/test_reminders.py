"""Tests du module de rappels et de la détection en langage naturel."""

import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import reminders
from bots import telegram_bot


class TestParseDelayOrTime(unittest.TestCase):
    def test_relative_units(self):
        now = datetime.datetime.now(reminders.TIMEZONE)
        for value, expected in [
            ("30s", datetime.timedelta(seconds=30)),
            ("15m", datetime.timedelta(minutes=15)),
            ("2h", datetime.timedelta(hours=2)),
            ("1j", datetime.timedelta(days=1)),
            ("15 minutes", datetime.timedelta(minutes=15)),
        ]:
            target, error = reminders.parse_delay_or_time(value, now=now, force_relative=True)
            self.assertIsNone(error)
            self.assertEqual(target, now + expected)

    def test_ambiguous_1h30(self):
        now = datetime.datetime(2026, 10, 19, 10, 0, tzinfo=reminders.TIMEZONE)
        relative, _ = reminders.parse_delay_or_time("1h30", now=now, force_relative=True)
        fixed, _ = reminders.parse_delay_or_time("1h30", now=now, force_relative=False)
        self.assertEqual(relative, now + datetime.timedelta(hours=1, minutes=30))
        self.assertEqual(fixed, datetime.datetime(2026, 10, 20, 1, 30, tzinfo=reminders.TIMEZONE))

    def test_absolute_time(self):
        now = datetime.datetime(2026, 10, 19, 10, 0, tzinfo=reminders.TIMEZONE)
        target, error = reminders.parse_delay_or_time("14h30", now=now)
        self.assertIsNone(error)
        self.assertEqual(target, datetime.datetime(2026, 10, 19, 14, 30, tzinfo=reminders.TIMEZONE))


class TestNaturalReminder(unittest.TestCase):
    def test_examples(self):
        cases = {
            "Rappelle-moi dans 15 min de couper le feu": ("15 min", "couper le feu"),
            "Rappel moi les pattes dans 7m": ("7m", "les pattes"),
            "Je veux un rappel dans 3 minutes pour me dire que je vais me coucher.": (
                "3 minutes",
                "je vais me coucher",
            ),
            "Rappelle-moi de sortir le pain dans 20 minutes": ("20 minutes", "sortir le pain"),
            "Mets un rappel à 14h30 d'appeler le médecin": ("14h30", "appeler le médecin"),
            "Rappel dans 30m pour couper le gaz": ("30m", "couper le gaz"),
            "Peux-tu me rappeler dans 10 min de sortir le pain": ("10 min", "sortir le pain"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(reminders.detect_natural_reminder(text), expected)

    def test_non_reminder_messages(self):
        for text in [
            "Salut, ça va ?",
            "Rappelle-moi comment fonctionne le bot",
            "Quelle heure est-il ?",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(reminders.detect_natural_reminder(text))

    def test_datetime_detection(self):
        now = datetime.datetime(2026, 10, 19, 10, 0, tzinfo=reminders.TIMEZONE)
        target, message = reminders.detect_natural_reminder_datetime(
            "Rappelle-moi dans 1h30 de sortir le pain", now=now
        )
        self.assertEqual(target, now + datetime.timedelta(hours=1, minutes=30))
        self.assertEqual(message, "sortir le pain")


class TestSplitCommand(unittest.TestCase):
    def test_duration_and_message(self):
        now = datetime.datetime.now(reminders.TIMEZONE)
        for raw in [
            "15m Sortir le pain",
            "15 minutes Sortir le pain",
            "1h30 Appeler le médecin",
        ]:
            with self.subTest(raw=raw):
                target, message, error = reminders.split_time_and_message(raw)
                self.assertIsNone(error)
                self.assertTrue(message)
                self.assertGreater(target, now)


class TestReminderStorage(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_reminders.db"
        self.patch = patch.object(reminders, "DB_PATH", self.db_path)
        self.patch.start()
        reminders.init_db()

    def tearDown(self):
        self.patch.stop()
        self.temp_dir.cleanup()

    def test_create_list_due_cancel(self):
        now = datetime.datetime.now(reminders.TIMEZONE)
        reminder = reminders.add_reminder(
            platform="telegram",
            chat_id="123",
            target_id="456",
            message="Test",
            remind_at=now - datetime.timedelta(seconds=1),
            target_name="Utilisateur",
        )
        self.assertEqual(reminder["status"], "pending")

        active = reminders.list_active_reminders("123")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["message"], "Test")

        due = reminders.get_due_reminders(now)
        self.assertEqual(len(due), 1)

        reminders.mark_reminder_sent(reminder["id"])
        self.assertEqual(reminders.get_due_reminders(now), [])
        self.assertEqual(reminders.list_active_reminders("123"), [])

    def test_cancel(self):
        now = datetime.datetime.now(reminders.TIMEZONE)
        reminder = reminders.add_reminder(
            platform="discord",
            chat_id="789",
            target_id="1011",
            message="Autre test",
            remind_at=now + datetime.timedelta(minutes=5),
        )
        self.assertTrue(reminders.cancel_reminder(reminder["id"], "789"))
        self.assertFalse(reminders.cancel_reminder(reminder["id"], "789"))
        self.assertEqual(reminders.list_active_reminders("789"), [])


class TestTelegramReminderIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_natural_reminder_skips_runner(self):
        now = datetime.datetime.now(reminders.TIMEZONE)
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=123),
            effective_user=SimpleNamespace(id=456, full_name="Test User"),
        )
        waiting = SimpleNamespace(edit_text=AsyncMock())
        reminder = {
            "id": 7,
            "platform": "telegram",
            "chat_id": "123",
            "target_id": "456",
            "target_name": "Test User",
            "message": "sortir le pain",
            "remind_at": (now + datetime.timedelta(minutes=5)).isoformat(),
            "created_at": now.isoformat(),
            "status": "pending",
        }

        with (
            patch.object(telegram_bot, "add_reminder", return_value=reminder) as add,
            patch.object(
                telegram_bot, "forward_to_runner", new=AsyncMock()
            ) as forward,
        ):
            await telegram_bot._process_prompt_and_reply(
                update,
                "Rappelle-moi dans 5 min de sortir le pain",
                waiting,
            )

        add.assert_called_once()
        forward.assert_not_awaited()
        waiting.edit_text.assert_awaited()
        self.assertIn("Rappel programmé", waiting.edit_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
