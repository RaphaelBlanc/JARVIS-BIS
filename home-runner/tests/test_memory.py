"""Tests de la mémoire durable et du profil utilisateur."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import memory


class TestMemoryStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "memory.json"
        self.patch = patch.object(memory, "MEMORY_PATH", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp_dir.cleanup()

    def test_empty_memory(self):
        self.assertEqual(memory.get_memory(), {"profile": {}, "facts": []})
        self.assertEqual(memory.render_memory_prompt(), "")

    def test_add_and_delete_fact(self):
        fact = memory.add_fact("J'aime le café sans sucre", tags=["préférence"])
        self.assertEqual(fact["text"], "J'aime le café sans sucre")
        self.assertEqual(len(fact["id"]), 8)
        self.assertEqual(len(memory.get_memory()["facts"]), 1)
        self.assertTrue(memory.delete_fact(fact["id"]))
        self.assertFalse(memory.delete_fact(fact["id"]))
        self.assertEqual(memory.get_memory()["facts"], [])

    def test_profile_and_prompt(self):
        memory.set_profile("ville", "Toulouse")
        memory.set_profile("fuseau", "Europe/Paris")
        memory.add_fact("Je préfère les réponses courtes")
        prompt = memory.render_memory_prompt()
        self.assertIn("ville : Toulouse", prompt)
        self.assertIn("fuseau : Europe/Paris", prompt)
        self.assertIn("Je préfère les réponses courtes", prompt)
        self.assertIn("Contexte utilisateur durable", prompt)
        self.assertTrue(memory.delete_profile("ville"))
        self.assertNotIn("ville : Toulouse", memory.render_memory_prompt())

    def test_clear_facts_keeps_profile(self):
        memory.set_profile("ville", "Lyon")
        memory.add_fact("Souvenir A")
        memory.add_fact("Souvenir B")
        self.assertEqual(memory.clear_facts(), 2)
        self.assertEqual(memory.get_memory()["facts"], [])
        self.assertEqual(memory.get_memory()["profile"]["ville"], "Lyon")

    def test_prompt_is_truncated(self):
        memory.add_fact("x" * 500)
        prompt = memory.render_memory_prompt(max_chars=120)
        self.assertLessEqual(len(prompt), 160)
        self.assertIn("mémoire tronquée", prompt)

    def test_memory_version_changes(self):
        before = memory.memory_version()
        memory.add_fact("Nouveau souvenir")
        after_fact = memory.memory_version()
        self.assertNotEqual(before, after_fact)

        memory.set_profile("ville", "Toulouse")
        after_profile = memory.memory_version()
        self.assertNotEqual(after_fact, after_profile)

    def test_only_ten_most_recent_facts_are_injected(self):
        with patch.object(memory, "MEMORY_PROMPT_MAX_FACTS", 10):
            for index in range(12):
                memory.add_fact(f"Souvenir {index}")
            prompt = memory.render_memory_prompt(max_chars=10000)
        self.assertNotIn("- Souvenir 0\n", prompt)
        self.assertNotIn("- Souvenir 1\n", prompt)
        self.assertIn("- Souvenir 2\n", prompt)
        self.assertIn("- Souvenir 11\n", prompt)

    def test_default_prompt_budget_is_1500_chars(self):
        with patch.object(memory, "MEMORY_PROMPT_MAX_CHARS", 1500):
            memory.set_profile("ville", "Toulouse")
            for index in range(20):
                memory.add_fact(f"Un souvenir assez long numéro {index} " + "x" * 100)
            prompt = memory.render_memory_prompt()
        self.assertLessEqual(len(prompt), 1500)
        self.assertIn("ville : Toulouse", prompt)
        self.assertIn("mémoire tronquée", prompt)


if __name__ == "__main__":
    unittest.main()
