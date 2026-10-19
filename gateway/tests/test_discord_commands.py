"""Tests statiques de l'unicité des commandes Discord."""

import re
import unittest
from collections import Counter
from pathlib import Path

DISCORD_BOT = Path(__file__).resolve().parents[1] / "bots" / "discord_bot.py"
SLASH_RE = re.compile(r'@app_commands\.command\(name="([^"]+)"')
PREFIX_RE = re.compile(
    r'@bot\.command\(name="([^"]+)"(?:, aliases=\[([^\]]*)\])?'
)


class TestDiscordCommandNames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = DISCORD_BOT.read_text(encoding="utf-8")
        cls.slash = SLASH_RE.findall(cls.source)
        cls.prefix = []
        cls.aliases = []
        for match in PREFIX_RE.finditer(cls.source):
            cls.prefix.append(match.group(1))
            if match.group(2):
                cls.aliases.extend(re.findall(r'"([^"]+)"', match.group(2)))

    def test_no_duplicate_slash_commands(self):
        duplicates = [name for name, count in Counter(self.slash).items() if count > 1]
        self.assertEqual(duplicates, [])

    def test_no_duplicate_prefix_commands(self):
        duplicates = [name for name, count in Counter(self.prefix).items() if count > 1]
        self.assertEqual(duplicates, [])

    def test_aliases_do_not_shadow_commands(self):
        conflicts = sorted(set(self.aliases) & set(self.prefix))
        self.assertEqual(conflicts, [])

    def test_no_duplicate_aliases(self):
        duplicates = [name for name, count in Counter(self.aliases).items() if count > 1]
        self.assertEqual(duplicates, [])

    def test_slash_and_prefix_have_parity(self):
        self.assertEqual(sorted(set(self.slash)), sorted(set(self.prefix)))

    def test_server_slash_is_added_to_command_tree(self):
        """Une commande définie mais non ajoutée au tree n'apparaît pas sur Discord."""
        debut = self.source.index("for cmd in [")
        fin = self.source.index("]:", debut)
        self.assertIn("server_slash", self.source[debut:fin])


if __name__ == "__main__":
    unittest.main()
