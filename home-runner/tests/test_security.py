"""Tests des garde-fous d'exécution locale."""

import unittest
from unittest.mock import patch

import agy_bridge


class TestAgySandbox(unittest.TestCase):
    def test_sandbox_enabled_by_default(self):
        with patch.object(agy_bridge, "AGY_SANDBOX", True):
            self.assertEqual(agy_bridge._sandbox_args(), ["--sandbox"])

    def test_sandbox_can_be_disabled(self):
        with patch.object(agy_bridge, "AGY_SANDBOX", False):
            self.assertEqual(agy_bridge._sandbox_args(), [])

    def test_worker_info_exposes_sandbox(self):
        with patch.object(agy_bridge, "AGY_SANDBOX", True):
            info = agy_bridge.get_worker_info()
        self.assertTrue(info["sandbox"])


if __name__ == "__main__":
    unittest.main()
