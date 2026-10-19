"""Tests du suivi des tokens et de la compaction automatique."""

import asyncio
import unittest
from unittest.mock import patch

import agy_bridge


class TestTokenUsage(unittest.TestCase):
    def setUp(self):
        agy_bridge._TOKEN_USAGE.update(
            {
                "date": "",
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "prompt_chars": 0,
                "output_chars": 0,
                "tokens_available": False,
            }
        )

    def test_fallback_logs_prompt_and_response_sizes(self):
        counts = agy_bridge.record_token_usage("bonjour", "salut")
        stats = agy_bridge.get_token_usage()
        self.assertIsNone(counts)
        self.assertEqual(stats["requests"], 1)
        self.assertEqual(stats["prompt_chars"], len("bonjour"))
        self.assertEqual(stats["output_chars"], len("salut"))
        self.assertFalse(stats["tokens_available"])

    def test_usage_is_extracted_and_accumulated(self):
        result = {
            "status": "SUCCESS",
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        }
        agy_bridge.record_token_usage("question", "réponse", result)
        agy_bridge.record_token_usage(
            "question 2",
            "réponse 2",
            {
                "status": "SUCCESS",
                "usageMetadata": {
                    "promptTokenCount": 80,
                    "candidatesTokenCount": 20,
                },
            },
        )
        stats = agy_bridge.get_token_usage()
        self.assertEqual(stats["requests"], 2)
        self.assertEqual(stats["input_tokens"], 200)
        self.assertEqual(stats["output_tokens"], 50)
        self.assertTrue(stats["tokens_available"])


class TestAutoCompaction(unittest.TestCase):
    def tearDown(self):
        agy_bridge.reset_session()

    def test_threshold_resets_history(self):
        with patch.object(agy_bridge, "AUTO_COMPACT_TURNS", 2):
            agy_bridge.reset_session()
            agy_bridge._note_turn()
            self.assertFalse(agy_bridge._auto_compact_due())
            agy_bridge._note_turn()
            self.assertTrue(agy_bridge._auto_compact_due())
            self.assertTrue(asyncio.run(agy_bridge._maybe_auto_compact()))
            self.assertEqual(agy_bridge.SESSION_CONFIG["turns"], 0)
            self.assertFalse(agy_bridge.SESSION_CONFIG["has_active_session"])

    def test_disabled_when_zero(self):
        with patch.object(agy_bridge, "AUTO_COMPACT_TURNS", 0):
            agy_bridge.SESSION_CONFIG["turns"] = 999
            self.assertFalse(agy_bridge._auto_compact_due())


if __name__ == "__main__":
    unittest.main()
