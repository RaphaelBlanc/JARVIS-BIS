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
from unittest.mock import patch

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

        with patch.dict(os.environ, {"ENABLE_NODE_EXEC": "1"}):
            res = await self.hub.send_action("xenospacex-laptop", "exec", {"command": "uname -a"}, timeout=6.0)
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


class TestRemoteConfirmRelay(unittest.IsolatedAsyncioTestCase):
    """Vérifie le protocole de confirmation distante TOTP (confirm_request -> confirm_response)."""

    async def asyncSetUp(self):
        self.hub = NodeHub()
        self.ws = DummyWebSocket()
        # Nœud annonçant remote_confirm
        await self.hub.register_node(
            "pc-remote",
            self.ws,
            info={"capabilities": ["exec", "remote_confirm"], "hostname": "remote-station"},
        )

    async def test_capability_remote_confirm_enforced(self):
        ws_legacy = DummyWebSocket()
        await self.hub.register_node(
            "pc-legacy",
            ws_legacy,
            info={"capabilities": ["exec"], "hostname": "legacy-station"},
        )
        called = False

        def cb(node_id, msg):
            nonlocal called
            called = True

        self.hub.on_confirm_request(cb)
        self.hub.handle_incoming_message("pc-legacy", {
            "type": "confirm_request",
            "req_id": "legacy_req_1",
            "summary": "action sensible",
            "expires_in": 30,
        })
        self.assertFalse(called)
        self.assertIsNone(self.hub.get_pending_confirmation("legacy_req_1"))

    async def test_confirm_request_and_allow_with_totp(self):
        received_req = None

        def cb(node_id, msg):
            nonlocal received_req
            received_req = msg

        self.hub.on_confirm_request(cb)
        self.hub._pending_request_nodes["req_123"] = "pc-remote"
        self.hub.handle_incoming_message("pc-remote", {
            "type": "confirm_request",
            "req_id": "req_123",
            "action": "exec",
            "summary": "exécuter rm -rf /tmp/test",
            "expires_in": 32,
            "code_required": True,
        })

        self.assertIsNotNone(received_req)
        self.assertEqual(received_req["req_id"], "req_123")

        # Validation de la présence dans pending
        pending = self.hub.get_pending_confirmation("req_123")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["node_id"], "pc-remote")

        # Envoi de la confirmation avec code TOTP à 6 chiffres
        ok = await self.hub.send_confirm_response("req_123", decision="allow", code="654321")
        self.assertTrue(ok)
        self.assertEqual(len(self.ws.sent_messages), 1)
        resp_msg = self.ws.sent_messages[0]
        self.assertEqual(resp_msg["type"], "confirm_response")
        self.assertEqual(resp_msg["req_id"], "req_123")
        self.assertEqual(resp_msg["decision"], "allow")
        self.assertEqual(resp_msg["code"], "654321")

        # La confirmation a été consommée
        self.assertIsNone(self.hub.get_pending_confirmation("req_123"))

    async def test_confirm_allow_requires_valid_6_digits(self):
        self.hub._pending_request_nodes["req_invalid_code"] = "pc-remote"
        self.hub.handle_incoming_message("pc-remote", {
            "type": "confirm_request",
            "req_id": "req_invalid_code",
            "expires_in": 30,
        })
        # Sans code
        ok1 = await self.hub.send_confirm_response("req_invalid_code", decision="allow", code=None)
        self.assertFalse(ok1)

        # Code à 5 chiffres
        ok2 = await self.hub.send_confirm_response("req_invalid_code", decision="allow", code="12345")
        self.assertFalse(ok2)

        # Code alphanumérique non valide
        ok3 = await self.hub.send_confirm_response("req_invalid_code", decision="allow", code="abcdef")
        self.assertFalse(ok3)

    async def test_confirm_deny(self):
        self.hub._pending_request_nodes["req_deny_1"] = "pc-remote"
        self.hub.handle_incoming_message("pc-remote", {
            "type": "confirm_request",
            "req_id": "req_deny_1",
            "summary": "touch /tmp/evil",
            "expires_in": 30,
        })
        ok = await self.hub.send_confirm_response("req_deny_1", decision="deny")
        self.assertTrue(ok)
        self.assertEqual(len(self.ws.sent_messages), 1)
        resp = self.ws.sent_messages[0]
        self.assertEqual(resp["decision"], "deny")
        self.assertNotIn("code", resp)

    async def test_confirm_unavailable(self):
        self.hub._pending_request_nodes["req_unavail_1"] = "pc-remote"
        self.hub.handle_incoming_message("pc-remote", {
            "type": "confirm_request",
            "req_id": "req_unavail_1",
            "summary": "touch /tmp/test",
            "expires_in": 30,
        })
        ok = await self.hub.send_confirm_unavailable("req_unavail_1", node_id="pc-remote")
        self.assertTrue(ok)
        self.assertEqual(len(self.ws.sent_messages), 1)
        resp = self.ws.sent_messages[0]
        self.assertEqual(resp, {"type": "confirm_unavailable", "req_id": "req_unavail_1"})
        self.assertIsNone(self.hub.get_pending_confirmation("req_unavail_1"))

    async def test_confirm_cancel(self):
        canceled_req = None

        def cancel_cb(node_id, msg):
            nonlocal canceled_req
            canceled_req = msg

        self.hub.on_confirm_cancel(cancel_cb)
        self.hub._pending_request_nodes["req_cancel_1"] = "pc-remote"
        self.hub.handle_incoming_message("pc-remote", {
            "type": "confirm_request",
            "req_id": "req_cancel_1",
            "expires_in": 30,
        })
        self.assertIsNotNone(self.hub.get_pending_confirmation("req_cancel_1"))

        # Annulation
        self.hub.handle_incoming_message("pc-remote", {
            "type": "confirm_cancel",
            "req_id": "req_cancel_1",
        })
        self.assertIsNotNone(canceled_req)
        self.assertIsNone(self.hub.get_pending_confirmation("req_cancel_1"))

    async def test_full_action_flow_with_confirm_request_then_response(self):
        confirm_seen = False

        def on_confirm(node_id, msg):
            nonlocal confirm_seen
            confirm_seen = True

        self.hub.on_confirm_request(on_confirm)

        async def fake_node_behavior():
            await asyncio.sleep(0.02)
            self.assertEqual(len(self.ws.sent_messages), 1)
            action_msg = self.ws.sent_messages[0]
            req_id = action_msg["req_id"]

            # 1. Le nœud émet confirm_request
            self.hub.handle_incoming_message("pc-remote", {
                "type": "confirm_request",
                "req_id": req_id,
                "action": "exec",
                "summary": "lancer discord",
                "expires_in": 30,
                "code_required": True,
            })

            self.assertTrue(confirm_seen)

            # 2. Le serveur autorise avec le code TOTP
            ok = await self.hub.send_confirm_response(req_id, decision="allow", code="123456")
            self.assertTrue(ok)

            # 3. Le nœud exécute et envoie la réponse finale
            self.hub.handle_incoming_message("pc-remote", {
                "type": "response",
                "req_id": req_id,
                "success": True,
                "stdout": "discord launched",
            })

        asyncio.create_task(fake_node_behavior())

        with patch.dict(os.environ, {"ENABLE_NODE_EXEC": "1"}):
            res = await self.hub.send_action("pc-remote", "exec", {"command": "discord"}, timeout=2.0)
        self.assertTrue(res.get("success"))
        self.assertEqual(res.get("stdout"), "discord launched")

    async def test_send_action_enforces_enable_node_exec_and_explicit_node_id(self):
        with patch.dict(os.environ, {"ENABLE_NODE_EXEC": "0"}):
            res_disabled = await self.hub.send_action("pc-remote", "exec", {"command": "id"}, timeout=6.0)
            self.assertFalse(res_disabled.get("success"))
            self.assertIn("ENABLE_NODE_EXEC", res_disabled.get("error", ""))

            # L'alias run_shell doit également être soumis à ENABLE_NODE_EXEC
            res_run_shell = await self.hub.send_action("pc-remote", "run_shell", {"command": "id"}, timeout=6.0)
            self.assertFalse(res_run_shell.get("success"))
            self.assertIn("ENABLE_NODE_EXEC", res_run_shell.get("error", ""))

        with patch.dict(os.environ, {"ENABLE_NODE_EXEC": "1"}):
            res_auto_exec = await self.hub.send_action("auto", "exec", {"command": "id"}, timeout=6.0)
            self.assertFalse(res_auto_exec.get("success"))
            res_auto_shell = await self.hub.send_action("auto", "run_shell", {"command": "id"}, timeout=6.0)
            self.assertFalse(res_auto_shell.get("success"))
            res_auto_write = await self.hub.send_action("auto", "write_file", {"path": "a.txt", "content": "x"}, timeout=6.0)
            self.assertFalse(res_auto_write.get("success"))
            # Une sous-chaîne ("remote" ou "o") doit être rejetée pour exec et write_file (correspondance exacte requise)
            res_sub_exec = await self.hub.send_action("remote", "exec", {"command": "id"}, timeout=6.0)
            self.assertFalse(res_sub_exec.get("success"))
            res_sub_write = await self.hub.send_action("o", "write_file", {"path": "a.txt", "content": "x"}, timeout=6.0)
            self.assertFalse(res_sub_write.get("success"))
            # Action hors liste blanche rejetée
            res_unknown = await self.hub.send_action("pc-remote", "arbitrary_action", {}, timeout=6.0)
            self.assertFalse(res_unknown.get("success"))
            # Timeout non fini rejeté
            res_nan = await self.hub.send_action("pc-remote", "exec", {"command": "id"}, timeout=float("nan"))
            self.assertFalse(res_nan.get("success"))

    async def test_confirm_request_rejects_other_node_hijack(self):
        # 1. Aucune action en cours -> confirm_request non sollicité rejeté
        self.hub._pending_request_nodes.clear()
        self.hub.handle_incoming_message("pc-remote", {
            "type": "confirm_request",
            "req_id": "req_unsolicited",
            "summary": "texte arbitraire non sollicité",
            "expires_in": 30,
        })
        self.assertIsNone(self.hub.get_pending_confirmation("req_unsolicited"))

        # 2. Action appartenant à un autre nœud -> rejeté
        self.hub._pending_request_nodes["req_owned_by_remote"] = "pc-remote"
        try:
            await self.hub.register_node("pc-other", DummyWebSocket(), {
                "hostname": "other-station",
                "capabilities": ["exec", "remote_confirm"],
            })
            self.hub.handle_incoming_message("pc-other", {
                "type": "confirm_request",
                "req_id": "req_owned_by_remote",
                "summary": "tentative usurpation",
                "expires_in": 30,
            })
            self.assertIsNone(self.hub.get_pending_confirmation("req_owned_by_remote"))
        finally:
            self.hub._pending_request_nodes.clear()

    def test_totp_code_normalization(self):
        import re
        def extract(raw: str):
            cleaned = re.sub(r"[\s\u00a0\u202f.\-]+", "", raw or "")
            if re.fullmatch(r"[0-9]{6}", cleaned, flags=re.ASCII):
                return cleaned
            return None

        self.assertEqual(extract("123456"), "123456")
        self.assertEqual(extract("123 456"), "123456")
        self.assertEqual(extract("123\u00a0456."), "123456")
        self.assertEqual(extract("123-456"), "123456")
        self.assertIsNone(extract("12345"))
        self.assertIsNone(extract("1234567"))
        self.assertIsNone(extract("١٢٣٤٥٦"))  # Non-ASCII digits rejected


class TestHardwareRelaySecurity(unittest.TestCase):
    """Vérifie la sanitisation des chemins et le nettoyage des temporaires."""

    def test_sanitize_forbidden_paths(self):
        from core.hardware_relay import _sanitize_output_path

        # Chemins interdits (système + dotfiles utilisateur)
        for bad in [
            "/etc/passwd",
            "/root/.bashrc",
            "/usr/bin/python",
            "/boot/grub",
            "~/.bashrc",
            "~/.ssh/authorized_keys",
            "~/.config/autostart/evil.desktop",
        ]:
            p, err = _sanitize_output_path(bad)
            self.assertIsNone(p)
            self.assertIn("interdit", err)

        # Chemin autorisé
        p_ok, err_ok = _sanitize_output_path("/tmp/safe_shot.png")
        self.assertIsNone(err_ok)
        self.assertEqual(str(p_ok), "/tmp/safe_shot.png")
        p_ok.unlink(missing_ok=True)

        # Génération automatique si None
        p_auto, err_auto = _sanitize_output_path(None)
        self.assertIsNone(err_auto)
        self.assertIsNotNone(p_auto)
        self.assertTrue("nc_screenshot_" in str(p_auto))
        # Nettoyage
        p_auto.unlink(missing_ok=True)


class TestTrustSessionOrigin(unittest.IsolatedAsyncioTestCase):
    """Vérifie la gestion des sessions de confiance (origin, session, session_end) sur Telegram, Discord et Desktop."""

    async def asyncSetUp(self):
        self.hub = NodeHub()
        self.ws_laptop = DummyWebSocket()
        self.ws_tour = DummyWebSocket()
        await self.hub.register_node(
            "laptop",
            self.ws_laptop,
            info={"hostname": "laptop", "capabilities": ["exec", "remote_confirm", "trust_session"]},
        )
        await self.hub.register_node(
            "tour",
            self.ws_tour,
            info={"hostname": "tour", "capabilities": ["exec", "remote_confirm", "trust_session"]},
        )

    def test_normalize_and_build_origin_multi_platform(self):
        from core.node_hub import normalize_origin

        # 1. Telegram
        o_tg = self.hub.build_origin(session_key="telegram:8227462739")
        self.assertIsNotNone(o_tg)
        self.assertEqual(o_tg["via"], "telegram")
        self.assertEqual(o_tg["chat"], 8227462739)
        self.assertTrue(len(o_tg["conversation"]) >= 8)

        # 2. Discord
        o_dc = self.hub.build_origin(session_key="discord:123456789")
        self.assertIsNotNone(o_dc)
        self.assertEqual(o_dc["via"], "discord")
        self.assertEqual(o_dc["chat"], 123456789)

        # 3. Desktop (laptop & tour)
        o_laptop = self.hub.build_origin(session_key="desktop:laptop")
        o_tour = self.hub.build_origin(session_key="desktop:tour")
        self.assertEqual(o_laptop["via"], "desktop")
        self.assertEqual(o_laptop["chat"], "laptop")
        self.assertEqual(o_tour["via"], "desktop")
        self.assertEqual(o_tour["chat"], "tour")
        self.assertNotEqual(o_laptop["conversation"], o_tour["conversation"])

        # Stabilité dans la même conversation, rotation sur reset
        o_laptop_2 = self.hub.build_origin(session_key="desktop:laptop")
        self.assertEqual(o_laptop, o_laptop_2)
        self.hub.rotate_conversation_token("desktop:laptop")
        o_laptop_after_reset = self.hub.build_origin(session_key="desktop:laptop")
        self.assertNotEqual(o_laptop["conversation"], o_laptop_after_reset["conversation"])

        # Origines invalides rejetées
        self.assertIsNone(normalize_origin(None))
        self.assertIsNone(normalize_origin({}))
        self.assertIsNone(normalize_origin({"via": "telegram", "chat": 123}))
        self.assertIsNone(normalize_origin({"via": "", "chat": 123, "conversation": "abc"}))

    def test_agy_bridge_no_persistent_token_stored(self):
        from agy_bridge import resume_conversation, get_or_set_origin_token, rotate_origin_token

        # Le runner ne conserve aucun jeton d'origine persistant
        self.assertEqual(get_or_set_origin_token("desktop:laptop"), "")
        self.assertEqual(rotate_origin_token("desktop:laptop"), "")

        # Sur resume_conversation, aucun session_token n'est injecté
        resumed = resume_conversation("conv-abc-12345", session_key="desktop:laptop")
        self.assertEqual(get_or_set_origin_token("desktop:laptop"), "")
        self.assertNotIn("session_token", resumed["session"])

    async def test_exec_forwards_origin_and_tracks_active_session_and_session_end(self):
        updates = []

        def on_sess(nid, sess, orig):
            updates.append((nid, sess, orig))

        self.hub.on_session_update(on_sess)
        origin = self.hub.build_origin(session_key="desktop:tour")

        async def fake_tour_reply():
            await asyncio.sleep(0.02)
            msg = self.ws_tour.sent_messages[-1]
            self.assertEqual(msg["action"], "exec")
            self.assertEqual(msg["origin"], origin)
            self.hub.handle_incoming_message("tour", {
                "type": "response",
                "req_id": msg["req_id"],
                "success": True,
                "stdout": "ok",
                "session": {"reste_s": 895, "reste_actions": 19},
            })

        asyncio.create_task(fake_tour_reply())
        with patch.dict(os.environ, {"ENABLE_NODE_EXEC": "1"}):
            res = await self.hub.exec_cmd("uptime", node_id="tour", origin=origin, timeout=2.0)

        self.assertTrue(res.get("success"))
        self.assertEqual(res.get("session"), {"reste_s": 895, "reste_actions": 19})
        active = self.hub.get_active_session("tour")
        self.assertIsNotNone(active)
        self.assertEqual(active["reste_actions"], 19)
        # origin retiré par défaut des réponses publiques de list_nodes / get_active_session
        self.assertNotIn("origin", active)
        active_internal = self.hub.get_active_session("tour", include_origin=True)
        self.assertEqual(active_internal["origin"], origin)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][0], "tour")
        self.assertEqual(updates[0][1], {"reste_s": 895, "reste_actions": 19})

        # Fermeture explicite via send_session_end
        closed = await self.hub.send_session_end("tour")
        self.assertEqual(closed, ["tour"])
        self.assertEqual(self.ws_tour.sent_messages[-1], {"type": "session_end"})
        self.assertIsNone(self.hub.get_active_session("tour"))
        self.assertIsNone(updates[-1][1])

    async def test_distinct_sessions_per_machine_and_remote_confirm(self):
        origin_tour = self.hub.build_origin(session_key="desktop:tour")

        async def fake_laptop_screenshot_with_remote_confirm():
            await asyncio.sleep(0.02)
            action_msg = self.ws_laptop.sent_messages[-1]
            req_id = action_msg["req_id"]
            self.assertEqual(action_msg["action"], "screenshot")
            self.assertEqual(action_msg["origin"], origin_tour)

            # 1. Le laptop demande une confirmation à distance (confirm_request)
            self.hub.handle_incoming_message("laptop", {
                "type": "confirm_request",
                "req_id": req_id,
                "action": "screenshot",
                "summary": "JARVIS veut faire une capture de ton écran",
                "expires_in": 32,
                "code_required": True,
            })
            await asyncio.sleep(0.02)

            # 2. La confirmation distante est validée via Telegram / API avec le code TOTP
            await self.hub.send_confirm_response(req_id, decision="allow", code="654321", node_id="laptop")
            await asyncio.sleep(0.02)

            # 3. Le laptop reçoit confirm_response avec le code 654321
            conf_resp = self.ws_laptop.sent_messages[-1]
            self.assertEqual(conf_resp["type"], "confirm_response")
            self.assertEqual(conf_resp["req_id"], req_id)
            self.assertEqual(conf_resp["decision"], "allow")
            self.assertEqual(conf_resp["code"], "654321")

            # 4. Le laptop exécute la capture et ouvre automatiquement sa session de confiance dès ce 1er accès
            self.hub.handle_incoming_message("laptop", {
                "type": "response",
                "req_id": req_id,
                "success": True,
                "image_base64": "iVBORw0KGgo=",
                "session": {"reste_s": 900, "reste_actions": 20},
            })

        asyncio.create_task(fake_laptop_screenshot_with_remote_confirm())
        res = await self.hub.send_action("laptop", "screenshot", {}, origin=origin_tour, timeout=2.0)
        self.assertTrue(res.get("success"))

        # Vérifie que la session est active sur laptop mais PAS sur tour (sessions distinctes par machine)
        sess_laptop = self.hub.get_active_session("laptop")
        sess_tour = self.hub.get_active_session("tour")
        self.assertIsNotNone(sess_laptop)
        self.assertEqual(sess_laptop["reste_actions"], 20)
        self.assertIsNone(sess_tour)


class TestAskRouteAndSessionTokenIsolation(unittest.TestCase):
    """Vérifie le durcissement de sécurité de /api/ask et l'isolation des fichiers de jeton d'origine."""

    def test_api_ask_rejects_bot_session_keys_and_platforms(self):
        try:
            from gateway.main import _ASK_SESSION_KEY_RE, app
            has_fastapi = True
        except ImportError:
            import re
            _ASK_SESSION_KEY_RE = re.compile(r'^(?:default|desktop:[\w.@-]{1,180})$', re.ASCII)
            has_fastapi = False

        # 1. Validation de la regex anti-usurpation
        self.assertIsNone(_ASK_SESSION_KEY_RE.fullmatch("telegram:5673671100"))
        self.assertIsNone(_ASK_SESSION_KEY_RE.fullmatch("discord:123456"))
        self.assertIsNone(_ASK_SESSION_KEY_RE.fullmatch("admin:bot"))
        self.assertIsNotNone(_ASK_SESSION_KEY_RE.fullmatch("default"))
        self.assertIsNotNone(_ASK_SESSION_KEY_RE.fullmatch("desktop:laptop"))
        self.assertIsNotNone(_ASK_SESSION_KEY_RE.fullmatch("desktop:tour"))

        if has_fastapi:
            from fastapi.testclient import TestClient
            import os

            client = TestClient(app)
            sec = os.getenv("SECRET_KEY", "test-secret")
            headers = {"X-Secret-Key": sec, "Content-Type": "application/json"}

            resp = client.post("/api/ask", json={"prompt": "test", "session_key": "telegram:5673671100"}, headers=headers)
            self.assertEqual(resp.status_code, 400)
            self.assertIn("Clé de session refusée", resp.text)

            resp = client.post("/api/ask", json={"prompt": "test", "platform": "telegram"}, headers=headers)
            self.assertEqual(resp.status_code, 400)
            self.assertIn("Plateforme refusée", resp.text)

    def test_active_origin_token_isolated_per_session(self):
        from importlib.machinery import SourceFileLoader
        import hashlib
        import tempfile

        loader = SourceFileLoader("agy_bridge_mod", str(Path(__file__).parent.parent / "home-runner" / "agy_bridge.py"))
        agy_bridge = loader.load_module()

        # Vérifie que deux sessions distinctes écrivent dans deux fichiers token distincts
        session_a = "desktop:laptop"
        session_b = "telegram:123456"
        token_a = "0123456789abcdef0123456789abcdef"
        token_b = "fedcba9876543210fedcba9876543210"

        d = agy_bridge._origin_runtime_dir()
        file_a = agy_bridge._origin_token_file(d, session_a)
        file_b = agy_bridge._origin_token_file(d, session_b)
        self.assertNotEqual(file_a, file_b)

        with agy_bridge.active_origin_token_scope(token_a, session_a):
            self.assertTrue(file_a.exists())
            self.assertEqual(file_a.read_text(encoding="utf-8").strip(), token_a)

            with agy_bridge.active_origin_token_scope(token_b, session_b):
                self.assertTrue(file_b.exists())
                self.assertEqual(file_b.read_text(encoding="utf-8").strip(), token_b)
                self.assertTrue(file_a.exists())

            self.assertFalse(file_b.exists())
            self.assertTrue(file_a.exists())

        self.assertFalse(file_a.exists())

    def test_permission_modes_restricted(self):
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader("agy_bridge_mod2", str(Path(__file__).parent.parent / "home-runner" / "agy_bridge.py"))
        agy_bridge = loader.load_module()

        self.assertNotIn("always-proceed", agy_bridge._ALLOWED_PERMISSION_MODES)
        self.assertNotIn("yolo", agy_bridge._ALLOWED_PERMISSION_MODES)
        self.assertNotIn("bypass", agy_bridge._ALLOWED_PERMISSION_MODES)
        self.assertIn("default", agy_bridge._ALLOWED_PERMISSION_MODES)
        self.assertIn("request-review", agy_bridge._ALLOWED_PERMISSION_MODES)


if __name__ == "__main__":
    unittest.main()
