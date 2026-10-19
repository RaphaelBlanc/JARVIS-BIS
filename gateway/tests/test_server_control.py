"""Tests du script d'actions serveur whitelistées (sans toucher au vrai système)."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "server-control.sh"


class TestServerControl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.trace = self.root / "trace"
        self.env = dict(
            os.environ,
            HOME=str(self.root),
            PATH=f"{self.bin}:/usr/bin:/bin",
            XDG_RUNTIME_DIR=str(self.root / "run"),
            TRACE=str(self.trace),
            SERVER_CONTROL_SLEEP="0",
        )
        (self.root / "run").mkdir()

        self._fake("systemctl", """#!/bin/sh
case "$*" in
  *"--user is-active assistant-tunnel-quick"*) echo inactive ;;
  *"--user is-active assistant-tunnel"*) echo inactive ;;
  *"is-active assistant-gateway"*) echo active ;;
  *"is-active assistant-runner"*) echo active ;;
  *) exit 0 ;;
esac
""")
        self._fake("curl", "#!/bin/sh\nprintf '{\"status\":\"ok\"}\\n'\n")
        self._fake("journalctl", "#!/bin/sh\nexit 0\n")
        self._fake("systemd-run", "#!/bin/sh\nexit 0\n")
        self._fake("sudo", """#!/bin/sh
if [ "${1:-}" = "-n" ]; then shift; fi
if [ "${1:-}" = "true" ]; then exit "${SUDO_OK:-0}"; fi
printf '%s\\n' "$*" >> "$TRACE"
exit 0
""")

    def _fake(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def run_action(self, action, **env):
        return subprocess.run(
            ["bash", str(SCRIPT), action],
            env=dict(self.env, **env),
            text=True,
            capture_output=True,
            timeout=10,
        )

    def test_help_and_unknown_action(self):
        self.assertEqual(self.run_action("help").returncode, 0)
        result = self.run_action("rm -rf /")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Action inconnue", result.stderr)

    def test_status_reads_services_and_health(self):
        result = self.run_action("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("assistant-gateway", result.stdout)
        self.assertIn("active", result.stdout)
        self.assertIn("assistant-runner", result.stdout)
        self.assertIn("/health", result.stdout)

    def test_restart_gateway_uses_systemd_run_outside_the_service(self):
        result = self.run_action("restart-gateway")
        self.assertEqual(result.returncode, 0, result.stderr)
        trace = self.trace.read_text()
        self.assertIn("systemd-run", trace)
        self.assertIn("--collect", trace)
        self.assertIn("--on-active=0", trace)
        self.assertIn("/bin/systemctl restart assistant-gateway", trace)

    def test_restart_is_refused_without_passwordless_sudo(self):
        result = self.run_action("restart-runner", SUDO_OK="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sudo sans mot de passe", result.stderr)

    def test_restart_runner_calls_systemctl(self):
        result = self.run_action("restart-runner")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("systemctl restart assistant-runner", self.trace.read_text())


if __name__ == "__main__":
    unittest.main()
