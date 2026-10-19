"""Tests du support des messages vocaux Telegram et Discord."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bots import discord_bot, telegram_bot


class TestTelegramVoice(unittest.IsolatedAsyncioTestCase):
    def _make_update(self):
        voice_file = SimpleNamespace(download_to_drive=AsyncMock())
        media = SimpleNamespace(get_file=AsyncMock(return_value=voice_file))
        waiting = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(
            voice=media,
            audio=None,
            message_id=1,
            reply_text=AsyncMock(return_value=waiting),
        )
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=456),
            message=message,
        )
        return update, waiting

    async def test_voice_is_transcribed_and_forwarded(self):
        update, waiting = self._make_update()

        with (
            patch.object(telegram_bot, "_is_allowed", return_value=True),
            patch.object(telegram_bot, "_save_chat_id"),
            patch.object(telegram_bot, "track_chat_message"),
            patch.object(
                telegram_bot,
                "transcribe_audio",
                new=AsyncMock(return_value=("bonjour", "test")),
            ),
            patch.object(
                telegram_bot, "_process_prompt_and_reply", new=AsyncMock()
            ) as process,
        ):
            await telegram_bot.handle_voice(update, None)

        process.assert_awaited_once()
        self.assertEqual(process.await_args.args[1], "bonjour")
        waiting.edit_text.assert_awaited()

    async def test_failed_transcription_reports_error(self):
        update, waiting = self._make_update()

        with (
            patch.object(telegram_bot, "_is_allowed", return_value=True),
            patch.object(telegram_bot, "_save_chat_id"),
            patch.object(telegram_bot, "track_chat_message"),
            patch.object(
                telegram_bot,
                "transcribe_audio",
                new=AsyncMock(return_value=(None, "échec")),
            ),
            patch.object(
                telegram_bot, "_process_prompt_and_reply", new=AsyncMock()
            ) as process,
        ):
            await telegram_bot.handle_voice(update, None)

        process.assert_not_awaited()
        waiting.edit_text.assert_awaited()
        self.assertIn("transcrire", waiting.edit_text.await_args.args[0])


class TestDiscordVoice(unittest.IsolatedAsyncioTestCase):
    async def test_audio_attachment_is_transcribed(self):
        attachment = SimpleNamespace(
            filename="voice.ogg",
            read=AsyncMock(return_value=b"fake-audio"),
        )

        with patch.object(
            discord_bot,
            "transcribe_audio",
            new=AsyncMock(return_value=("salut", "test")),
        ):
            text = await discord_bot._transcribe_audio_attachment(attachment)

        self.assertEqual(text, "salut")


if __name__ == "__main__":
    unittest.main()
