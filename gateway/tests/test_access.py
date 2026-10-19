"""Tests de la politique d'accès fail-closed des bots."""

import unittest
from unittest.mock import patch

from bots import discord_bot, telegram_bot


class TestAccessPolicy(unittest.TestCase):
    def _assert_bot_policy(self, module):
        with patch.object(module, "ALLOWED_USER_IDS", set()), patch.object(
            module, "ALLOW_ALL_USERS", False
        ):
            self.assertFalse(module._is_allowed(123))

        with patch.object(module, "ALLOWED_USER_IDS", set()), patch.object(
            module, "ALLOW_ALL_USERS", True
        ):
            self.assertTrue(module._is_allowed(123))

        with patch.object(module, "ALLOWED_USER_IDS", {123}), patch.object(
            module, "ALLOW_ALL_USERS", False
        ):
            self.assertTrue(module._is_allowed(123))
            self.assertFalse(module._is_allowed(456))

    def test_discord_policy(self):
        self._assert_bot_policy(discord_bot)

    def test_telegram_policy(self):
        self._assert_bot_policy(telegram_bot)


if __name__ == "__main__":
    unittest.main()
