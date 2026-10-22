"""
tests/test_multi_pc_node.py — Tests unitaires pour l'isolation multi-session et le Node Hub.

Valide :
1. L'isolation des sessions conversationnelles (multi-PC) dans agy_bridge.
2. Le registre, la résolution et la transmission de commandes dans NodeHub.
3. Le bac à sable de sécurité (Sandbox) de l'agent client (refus sudo, refus .ssh/clés/secrets/traversal).
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path

# Ajoute la racine du projet au sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "home-runner"))

from core.node_hub import ConnectedNode, NodeHub
from clients.node_agent import is_command_safe, is_path_safe, NodeAgent
from agy_bridge import (
    get_session_info,
    update_session,
    reset_session,
    _note_turn,
)


class TestMultiSessionIsolation(unittest.TestCase):
    """Vérifie que deux PC (ou utilisateurs) ont des sessions strictement indépendantes."""

    def setUp(self):
        reset_session("laptop")
        reset_session("tour")
        reset_session("default")

    def test_session_independence(self):
        # 1. Configurer laptop avec un modèle et effort spécifique
        update_session(model="claude-sonnet-4-6", effort="high", session_key="laptop")
        _note_turn("laptop")
        _note_turn("laptop")

        # 2. Configurer tour avec un autre modèle
        update_session(model="gemini-3.8-flash-low", effort="low", session_key="tour")
        _note_turn("tour")

        # 3. Vérifier que les paramètres et le compteur de tours ne fuient pas
        s_laptop = get_session_info("laptop")
        s_tour = get_session_info("tour")
        s_default = get_session_info("default")

        self.assertEqual(s_laptop["model"], "claude-sonnet-4-6")
        self.assertEqual(s_laptop["effort"], "high")
        self.assertEqual(s_laptop["turns"], 2)

        self.assertEqual(s_tour["model"], "gemini-3.8-flash-low")
        self.assertEqual(s_tour["effort"], "low")
        self.assertEqual(s_tour["turns"], 1)

        self.assertEqual(s_default["turns"], 0)

    def test_session_reset_isolation(self):
        update_session(model="claude-sonnet-4-6", session_key="laptop")
        update_session(model="gemini-3.8-flash-low", session_key="tour")

        # Reset laptop
        reset_session("laptop")

        s_laptop = get_session_info("laptop")
        s_tour = get_session_info("tour")

        self.assertEqual(s_laptop["turns"], 0)
        self.assertFalse(s_laptop["has_active_session"])
        self.assertEqual(s_tour["model"], "gemini-3.8-flash-low")


class DummyWebSocket:
    """Mock WebSocket pour tester NodeHub sans serveur actif."""
    def __init__(self):
        self.sent_messages = []
        self.closed = False

    async def send_json(self, data):
        self.sent_messages.append(data)

    async def close(self, code=1000, reason=""):
        self.closed = True


class TestNodeHub(unittest.IsolatedAsyncioTestCase):
    """Vérifie l'enregistrement et la résolution des nœuds dans NodeHub."""

    async def asyncSetUp(self):
        self.hub = NodeHub()
        self.ws_laptop = DummyWebSocket()
        self.ws_tour = DummyWebSocket()

        await self.hub.register_node(
            node_id="xenospacex-laptop",
            websocket=self.ws_laptop,
            info={"hostname": "arch-laptop", "os": "linux", "capabilities": ["exec", "screenshot"]},
        )
        await self.hub.register_node(
            node_id="xenospacex-tour",
            websocket=self.ws_tour,
            info={"hostname": "workstation-tour", "os": "linux", "capabilities": ["exec", "media"]},
        )

    async def test_list_nodes(self):
        nodes = self.hub.list_nodes()
        self.assertEqual(len(nodes), 2)
        node_ids = {n["node_id"] for n in nodes}
        self.assertIn("xenospacex-laptop", node_ids)
        self.assertIn("xenospacex-tour", node_ids)

    async def test_node_resolution(self):
        # 1. Résolution exacte
        n1 = self.hub.get_node("xenospacex-laptop")
        self.assertIsNotNone(n1)
        self.assertEqual(n1.node_id, "xenospacex-laptop")

        # 2. Résolution par hostname
        n2 = self.hub.get_node("workstation-tour")
        self.assertIsNotNone(n2)
        self.assertEqual(n2.node_id, "xenospacex-tour")

        # 3. Résolution partielle ("laptop" -> "xenospacex-laptop")
        n3 = self.hub.get_node("laptop")
        self.assertIsNotNone(n3)
        self.assertEqual(n3.node_id, "xenospacex-laptop")

        # 4. Nœud inconnu
        n_unknown = self.hub.get_node("serveur-inconnu")
        self.assertIsNone(n_unknown)

    async def test_request_response_matching(self):
        async def fake_node_reply():
            await asyncio.sleep(0.05)
            # Inspecter le message envoyé
            self.assertTrue(len(self.ws_laptop.sent_messages) > 0)
            msg = self.ws_laptop.sent_messages[0]
            req_id = msg["req_id"]
            # Simuler la réponse du nœud
            self.hub.handle_incoming_message("xenospacex-laptop", {
                "type": "response",
                "req_id": req_id,
                "success": True,
                "stdout": "Linux 6.12 arch-laptop",
            })

        asyncio.create_task(fake_node_reply())

        res = await self.hub.send_action("laptop", "exec", {"command": "uname -a"}, timeout=2.0)
        self.assertTrue(res.get("success"))
        self.assertEqual(res.get("stdout"), "Linux 6.12 arch-laptop")


class TestSecuritySandbox(unittest.IsolatedAsyncioTestCase):
    """Vérifie la robustesse de la Sandbox : interdiction de sudo et protection des secrets."""

    def test_command_security(self):
        # Commandes dangereuses avec sudo/su/pkexec doivent être rejetées, y compris avec évasion
        dangerous_commands = [
            "sudo rm -rf /",
            "sudo apt update",
            "pkexec bash",
            "su root",
            "echo test | su",
            "s''udo whoami",
            's""udo ls',
            "$(printf sudo) rm -rf /",
            "doas cat /etc/shadow",
            "echo test | sudo tee /etc/hosts",
            "cat ~/.ssh/id_ed25519",
            "tail -n 10 ~/.git-credentials",
            "grep token ~/.config/gh/hosts.yml",
        ]
        for cmd in dangerous_commands:
            safe, reason = is_command_safe(cmd)
            self.assertFalse(safe, f"La commande '{cmd}' aurait dû être rejetée : {reason}")

        # Commandes utilisateur saines doivent être autorisées
        safe_commands = [
            "ls -la ~/Documents",
            "uptime",
            "cat README.md",
            "git status",
            "python3 -V",
        ]
        for cmd in safe_commands:
            safe, _ = is_command_safe(cmd)
            self.assertTrue(safe, f"La commande '{cmd}' aurait dû être autorisée")

    def test_path_security(self):
        # Chemins sensibles interdits en lecture et écriture
        forbidden_paths = [
            Path.home() / ".ssh" / "id_rsa",
            Path.home() / ".ssh" / "id_ed25519",
            Path.home() / ".ssh" / "authorized_keys",
            Path.home() / ".gnupg" / "secring.gpg",
            Path.home() / ".aws" / "credentials",
            Path.home() / ".config" / "gh" / "hosts.yml",
            Path.home() / ".git-credentials",
            Path.home() / ".netrc",
            Path.home() / ".local" / "share" / "keyrings" / "login.keyring",
            Path.home() / ".mozilla" / "firefox" / "profiles.ini",
            Path.home() / ".config" / "chromium" / "Default" / "Cookies",
            Path.home() / ".config" / "nc" / "assistant.conf",
            Path("/etc/shadow"),
            Path("/etc/sudoers"),
            Path("/root/.bashrc"),
            Path.home() / "projet" / ".env",
            Path.home() / "server.key",
            Path.home() / "cert.pem",
        ]
        for p in forbidden_paths:
            safe_read, r_reason = is_path_safe(p, mode="read")
            self.assertFalse(safe_read, f"La lecture de '{p}' aurait dû être rejetée : {r_reason}")
            safe_write, w_reason = is_path_safe(p, mode="write")
            self.assertFalse(safe_write, f"L'écriture de '{p}' aurait dû être rejetée : {w_reason}")

        # Chemins de persistance interdits spécifiquement en écriture
        persistence_paths = [
            Path.home() / ".bashrc",
            Path.home() / ".zshrc",
            Path.home() / ".profile",
            Path.home() / ".config" / "autostart" / "malicious.desktop",
            Path.home() / ".config" / "systemd" / "user" / "evil.service",
        ]
        for p in persistence_paths:
            safe_write, w_reason = is_path_safe(p, mode="write")
            self.assertFalse(safe_write, f"L'écriture de persistance '{p}' aurait dû être rejetée : {w_reason}")

        # Chemins utilisateur normaux autorisés
        safe_paths = [
            Path.home() / "Documents" / "rapport.txt",
            Path.home() / "code" / "main.py",
            Path("/tmp/test.log"),
        ]
        for p in safe_paths:
            safe, reason = is_path_safe(p, mode="read")
            self.assertTrue(safe, f"Le chemin '{p}' aurait dû être autorisé ({reason})")

    def test_ws_url_security_and_no_secret_in_url(self):
        # 1. ws:// vers IP publique doit être rejeté
        agent_public = NodeAgent("http://51.15.22.44:8000", "my-secret", "node1")
        with self.assertRaises(ValueError):
            agent_public.get_ws_url()

        # 2. ws:// vers Tailscale (100.x.y.z) ou IP privée doit être accepté
        agent_tailscale = NodeAgent("http://100.89.134.15:8000", "my-secret", "node1")
        url_ts = agent_tailscale.get_ws_url()
        self.assertTrue(url_ts.startswith("ws://100.89.134.15:8000/api/node/ws"))
        self.assertNotIn("my-secret", url_ts)

        agent_local = NodeAgent("http://192.168.1.50:8000", "my-secret", "node1")
        url_loc = agent_local.get_ws_url()
        self.assertTrue(url_loc.startswith("ws://192.168.1.50:8000/api/node/ws"))
        self.assertNotIn("my-secret", url_loc)

        # 3. https:// (TLS) vers IP publique doit être accepté
        agent_tls = NodeAgent("https://api.jarvis.example.com", "my-secret", "node1")
        url_tls = agent_tls.get_ws_url()
        self.assertTrue(url_tls.startswith("wss://api.jarvis.example.com/api/node/ws"))
        self.assertNotIn("my-secret", url_tls)

    async def test_node_hub_requires_explicit_node_for_exec_and_write(self):
        hub = NodeHub()
        ws = DummyWebSocket()
        await hub.register_node("single-pc", ws, {"hostname": "single", "capabilities": ["exec"]})

        # exec_cmd sans node_id explicite doit échouer par sécurité
        res_exec = await hub.exec_cmd("uptime", node_id=None)
        self.assertFalse(res_exec["success"])
        self.assertIn("node_id", res_exec["error"])

        # write_file sans node_id explicite doit échouer par sécurité
        res_write = await hub.write_file("/tmp/foo.txt", "content", node_id="")
        self.assertFalse(res_write["success"])
        self.assertIn("node_id", res_write["error"])

    async def test_node_agent_action_execution(self):
        agent = NodeAgent("http://localhost:8000", "test-secret", "test-node")

        # 1. Commande saine
        res_ok = await agent.handle_action("exec", {"command": "echo 'jarvis-ok'"})
        self.assertTrue(res_ok.get("success"))
        self.assertEqual(res_ok.get("stdout"), "jarvis-ok")

        # 2. Commande interdite (sudo)
        res_sudo = await agent.handle_action("exec", {"command": "sudo whoami"})
        self.assertFalse(res_sudo.get("success"))
        self.assertIn("Sécurité", res_sudo.get("error", ""))

        # 3. Écriture puis lecture dans un fichier temporaire autorisé
        test_file = "/tmp/nc_test_safe_file.txt"
        res_write = await agent.handle_action("write_file", {"path": test_file, "content": "bonjour jarvis"})
        self.assertTrue(res_write.get("success"))

        res_read = await agent.handle_action("read_file", {"path": test_file})
        self.assertTrue(res_read.get("success"))
        self.assertEqual(res_read.get("content"), "bonjour jarvis")

        # 4. Tentative de lecture de fichier sensible (ex: .ssh)
        res_ssh = await agent.handle_action("read_file", {"path": str(Path.home() / ".ssh" / "id_rsa")})
        self.assertFalse(res_ssh.get("success"))
        self.assertIn("Sécurité", res_ssh.get("error", ""))

        # 5. Tentative d'écriture sur un fichier de persistance (ex: ~/.bashrc)
        res_persist = await agent.handle_action("write_file", {"path": str(Path.home() / ".bashrc"), "content": "alias ls=evil"})
        self.assertFalse(res_persist.get("success"))
        self.assertIn("Sécurité", res_persist.get("error", ""))

        # Nettoyage
        if os.path.exists(test_file):
            os.unlink(test_file)


if __name__ == "__main__":
    unittest.main()
