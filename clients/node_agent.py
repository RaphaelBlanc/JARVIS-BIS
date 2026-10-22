#!/usr/bin/env python3
"""
clients/node_agent.py — Agent nœud client JARVIS (à installer sur Laptop, Tour, etc.).

Maintient une connexion WebSocket sortante permanente vers la Gateway JARVIS.
Permet au serveur JARVIS d'exécuter des actions, lire/écrire des fichiers et lancer
des commandes locales avec le niveau de privilège de l'utilisateur connecté.

SÉCURITÉ RENFORCÉE (SANDBOX STRICTE) :
- Interdiction stricte de sudo, su, pkexec, doas ou toute tentative d'élévation de privilèges.
- Liste noire absolue des répertoires/fichiers sensibles (~/.ssh, ~/.gnupg, ~/.aws, assistant.conf, .env, clés privées).
- Vérification contre le path traversal (résolution des liens symboliques et chemins canoniques).
- Limite de taille de fichier stricte (5 Mo max).
"""

import argparse
import asyncio
import base64
import ipaddress
import json
import logging
import os
import platform
import re
import shutil
import socket
import struct
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] node-agent: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("node_agent")

CONF_PATH = Path.home() / ".config" / "nc" / "assistant.conf"
MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 Mo

# Répertoires et fichiers strictement interdits d'accès (lecture ou écriture)
FORBIDDEN_EXACT_NAMES = {
    "assistant.conf",
    "ia.conf",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    ".env",
    "master.key",
    "credentials",
    "hosts.yml",
    ".git-credentials",
    ".netrc",
}

FORBIDDEN_EXTENSIONS = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".kdbx",
}

FORBIDDEN_ROOTS = [
    Path.home() / ".ssh",
    Path.home() / ".gnupg",
    Path.home() / ".aws",
    Path.home() / ".config" / "gh",
    Path.home() / ".local" / "share" / "keyrings",
    Path.home() / ".mozilla",
    Path.home() / ".config" / "chromium",
    Path.home() / ".config" / "google-chrome",
    Path.home() / ".config" / "BraveSoftware",
    Path.home() / ".config" / "microsoft-edge",
    Path.home() / ".claude",
    Path("/etc/shadow"),
    Path("/etc/sudoers"),
    Path("/root"),
]

# Chemins strictement interdits en écriture (protection contre la persistance d'attaques)
FORBIDDEN_WRITE_PATHS = [
    Path.home() / ".bashrc",
    Path.home() / ".bash_profile",
    Path.home() / ".bash_logout",
    Path.home() / ".profile",
    Path.home() / ".zshrc",
    Path.home() / ".zprofile",
    Path.home() / ".zshenv",
    Path.home() / ".zlogin",
    Path.home() / ".config" / "fish",
    Path.home() / ".config" / "autostart",
    Path.home() / ".config" / "systemd",
    Path.home() / ".config" / "nc",
    Path.home() / ".local" / "bin" / "nc-noeud",
    Path.home() / ".local" / "bin" / "node_agent.py",
    Path("/etc/systemd"),
    Path("/etc/cron.d"),
    Path("/etc/cron.daily"),
    Path("/etc/cron.hourly"),
    Path("/etc/cron.weekly"),
    Path("/etc/cron.monthly"),
    Path("/var/spool/cron"),
]


def is_private_or_tailscale_host(host: str) -> bool:
    """Vérifie si l'hôte est local, privé (RFC 1918) ou Tailscale (100.64.0.0/10)."""
    if not host:
        return False
    clean = host.strip().lower()
    if clean in ("localhost", "127.0.0.1", "::1") or clean.endswith(".local") or clean.endswith(".ts.net"):
        return True
    try:
        ip = ipaddress.ip_address(clean)
        if ip.is_loopback or ip.is_private:
            return True
        if ip in ipaddress.ip_network("100.64.0.0/10"):
            return True
    except ValueError:
        pass
    return False


def is_path_safe(target_path: Path, mode: str = "read") -> Tuple[bool, str]:
    """Vérifie qu'un chemin ne tente pas d'accéder à des secrets ou de s'installer en persistance."""
    try:
        expanded = target_path.expanduser()
        if expanded.exists():
            resolved = expanded.resolve()
        else:
            resolved = expanded.parent.resolve() / expanded.name
    except Exception as exc:
        return False, f"Impossible de résoudre le chemin : {exc}"

    # Vérification des répertoires sensibles absolus
    for forbidden_root in FORBIDDEN_ROOTS:
        try:
            res_forbidden = forbidden_root.expanduser().resolve()
            if resolved == res_forbidden or res_forbidden in resolved.parents:
                return False, f"Accès refusé au répertoire sensible : {forbidden_root}"
        except Exception:
            pass

    # Vérification des fichiers sensibles spécifiques
    name = resolved.name.lower()
    if name in FORBIDDEN_EXACT_NAMES:
        return False, f"Accès refusé au fichier protégé : {name}"

    for ext in FORBIDDEN_EXTENSIONS:
        if name.endswith(ext):
            return False, f"Accès refusé aux clés et certificats : {name}"

    # Vérification de configuration JARVIS
    try:
        if resolved == CONF_PATH.resolve():
            return False, "Accès refusé au fichier de configuration confidentiel de l'assistant."
    except Exception:
        pass

    # En écriture : interdire toute persistance (shells, autostart, services, cron)
    if mode == "write":
        for fw in FORBIDDEN_WRITE_PATHS:
            try:
                res_fw = fw.expanduser().resolve()
                if resolved == res_fw or res_fw in resolved.parents:
                    return False, f"Écriture refusée sur un chemin système/persistance protégé : {fw.name}"
            except Exception:
                pass

    return True, ""


def is_command_safe(command: str) -> Tuple[bool, str]:
    """Vérifie qu'une commande ne tente pas d'escalade de privilèges ou d'accéder aux secrets."""
    if os.geteuid() == 0:
        return False, "L'agent nœud ne doit JAMAIS être exécuté en tant que root."

    if not command or not command.strip():
        return False, "Commande vide."

    # 1. Neutraliser les techniques d'évasion (quotes imbriquées, antislash, subshells)
    # Ex: s''udo, $(printf sudo), `echo su`, \su
    clean = re.sub(r"['\"\\`]", "", command)

    # 2. Vérification d'élévation de privilèges dans n'importe quel segment
    # Détecte sudo, pkexec, doas avec frontières de mots, et su isolé
    if re.search(r"\b(sudo|pkexec|doas)\b", clean, re.IGNORECASE):
        return False, "L'utilisation de 'sudo', 'pkexec' ou 'doas' est strictement interdite."

    if re.search(r"(?:^|[\s;&|/()${}`])su(?:$|[\s;&|/()${}`])", clean, re.IGNORECASE):
        return False, "L'utilisation de 'su' est strictement interdite."

    # 3. Vérification d'accès aux fichiers sensibles dans les arguments de commandes
    forbidden_terms = (
        ".ssh", ".gnupg", ".aws", ".git-credentials", ".netrc", "hosts.yml",
        "keyrings", "assistant.conf", "shadow", "sudoers"
    )
    for term in forbidden_terms:
        if re.search(rf"(?:^|[\s/]){re.escape(term)}(?:$|[\s/])", clean, re.IGNORECASE):
            return False, f"Accès aux secrets et clés interdit dans les commandes : '{term}'."

    return True, ""


class StdlibWebSocket:
    """Client WebSocket minimaliste RFC 6455 en pure bibliothèque standard Python."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.closed = False

    async def send(self, data: str | bytes) -> None:
        if isinstance(data, str):
            payload = data.encode("utf-8")
            opcode = 0x01
        else:
            payload = data
            opcode = 0x02

        length = len(payload)
        mask_key = os.urandom(4)
        header = bytearray([0x80 | opcode])

        if length <= 125:
            header.append(0x80 | length)
        elif length <= 65535:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))

        header.extend(mask_key)
        masked_payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        self.writer.write(header + masked_payload)
        await self.writer.drain()

    async def recv(self) -> str:
        while not self.closed:
            head = await self.reader.readexactly(2)
            b1, b2 = head[0], head[1]
            opcode = b1 & 0x0F
            is_masked = bool(b2 & 0x80)
            payload_len = b2 & 0x7F

            if payload_len == 126:
                ext = await self.reader.readexactly(2)
                payload_len = struct.unpack("!H", ext)[0]
            elif payload_len == 127:
                ext = await self.reader.readexactly(8)
                payload_len = struct.unpack("!Q", ext)[0]

            mask = None
            if is_masked:
                mask = await self.reader.readexactly(4)

            payload = await self.reader.readexactly(payload_len)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x08:  # Close
                self.closed = True
                break
            elif opcode == 0x09:  # Ping -> Pong
                header = bytearray([0x8A, 0x80 | len(payload)])
                mk = os.urandom(4)
                header.extend(mk)
                self.writer.write(header + bytes(b ^ mk[i % 4] for i, b in enumerate(payload)))
                await self.writer.drain()
                continue
            elif opcode == 0x0A:  # Pong
                continue
            elif opcode in (0x01, 0x02):
                return payload.decode("utf-8", errors="replace")

        raise ConnectionResetError("Connexion WebSocket fermée.")

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                mk = os.urandom(4)
                self.writer.write(bytearray([0x88, 0x80]) + mk)
                await self.writer.drain()
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass


async def connect_ws(url: str, secret_key: str):
    """Établit une connexion WebSocket avec repli transparent sur la bibliothèque standard."""
    try:
        import websockets
        headers = {"X-Secret-Key": secret_key}
        return await websockets.connect(
            url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
        )
    except ImportError:
        import ssl
        import urllib.parse
        u = urllib.parse.urlsplit(url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "wss" else 80)
        ssl_ctx = ssl.create_default_context() if u.scheme == "wss" else None

        reader, writer = await asyncio.open_connection(host, port, ssl=ssl_ctx)
        sec_key = base64.b64encode(os.urandom(16)).decode("ascii")
        path_query = u.path + ("?" + u.query if u.query else "")
        req = (
            f"GET {path_query} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {sec_key}\r\n"
            f"X-Secret-Key: {secret_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        writer.write(req.encode("utf-8"))
        await writer.drain()

        resp = await reader.readuntil(b"\r\n\r\n")
        status_line = resp.split(b"\r\n")[0].decode()
        if "101" not in status_line:
            writer.close()
            await writer.wait_closed()
            raise ConnectionError(f"Échec handshake HTTP WebSocket : {status_line}")

        return StdlibWebSocket(reader, writer)


class NodeAgent:
    """Agent nœud connecté à la Gateway JARVIS."""

    def __init__(
        self,
        gateway_url: str,
        secret_key: str,
        node_id: Optional[str] = None,
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.secret_key = secret_key
        self.node_id = node_id or socket.gethostname()
        self.hostname = socket.gethostname()
        self.capabilities = [
            "exec",
            "read_file",
            "write_file",
            "screenshot",
            "lock",
            "clipboard_get",
            "clipboard_set",
            "media",
            "notify",
            "system_info",
        ]
        self._running = False

    def get_ws_url(self) -> str:
        """Convertit l'URL HTTP/HTTPS en URL WebSocket WS/WSS avec validation de sécurité."""
        u = urllib.parse.urlsplit(self.gateway_url)
        host = u.hostname or "localhost"
        scheme = u.scheme.lower()

        if scheme == "https":
            ws_scheme = "wss"
        elif scheme == "http":
            if not is_private_or_tailscale_host(host):
                raise ValueError(
                    f"Connexion ws:// refusée vers l'hôte public '{host}' : le secret et les requêtes circuleraient en clair. "
                    "Configurez une URL https:// (TLS) ou utilisez une adresse IP privée / Tailscale."
                )
            ws_scheme = "ws"
        elif scheme == "wss":
            ws_scheme = "wss"
        elif scheme == "ws":
            if not is_private_or_tailscale_host(host):
                raise ValueError(
                    f"Connexion ws:// refusée vers l'hôte public '{host}' : le secret et les requêtes circuleraient en clair. "
                    "Utilisez wss:// ou une adresse IP privée / Tailscale."
                )
            ws_scheme = "ws"
        else:
            ws_scheme = "ws"

        netloc = host
        if u.port:
            netloc += f":{u.port}"

        path = u.path.rstrip("/")
        # Le secret n'est JAMAIS inclus dans l'URL (transmission stricte par en-tête X-Secret-Key)
        return f"{ws_scheme}://{netloc}{path}/api/node/ws"

    async def handle_action(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Traite une action reçue du serveur dans un environnement sécurisé."""
        try:
            if action == "exec":
                command = str(params.get("command", "")).strip()
                if not command:
                    return {"success": False, "error": "Commande vide"}

                safe, reason = is_command_safe(command)
                if not safe:
                    return {"success": False, "error": f"Sécurité : {reason}"}

                timeout = min(float(params.get("timeout", 30.0)), 120.0)
                cwd = params.get("cwd") or str(Path.home())

                start_t = time.perf_counter()
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, "TERM": "dumb"},
                )
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    return {
                        "success": False,
                        "error": f"Timeout ({timeout}s) dépassé lors de l'exécution.",
                    }

                dur = time.perf_counter() - start_t
                out = stdout_bytes.decode(errors="replace").strip()
                err = stderr_bytes.decode(errors="replace").strip()
                code = proc.returncode or 0

                return {
                    "success": code == 0,
                    "returncode": code,
                    "stdout": out,
                    "stderr": err,
                    "duration": round(dur, 2),
                    "message": f"Code {code} ({dur:.1f}s)\n{out}\n{err}".strip(),
                }

            elif action == "read_file":
                path_str = str(params.get("path", "")).strip()
                if not path_str:
                    return {"success": False, "error": "Chemin de fichier manquant"}

                target = Path(path_str).expanduser()
                safe, reason = is_path_safe(target, mode="read")
                if not safe:
                    return {"success": False, "error": f"Sécurité : {reason}"}

                if not target.exists():
                    return {"success": False, "error": f"Fichier inexistant : {path_str}"}

                if target.is_dir():
                    # Liste le contenu d'un répertoire
                    items = []
                    for item in target.iterdir():
                        items.append({
                            "name": item.name,
                            "is_dir": item.is_dir(),
                            "size": item.stat().st_size if item.is_file() else 0,
                        })
                    return {"success": True, "type": "directory", "items": items[:200]}

                if target.stat().st_size > MAX_FILE_BYTES:
                    return {"success": False, "error": f"Fichier trop volumineux (> 5 Mo)"}

                content = target.read_text(encoding="utf-8", errors="replace")
                return {"success": True, "type": "file", "content": content, "size": len(content)}

            elif action == "write_file":
                path_str = str(params.get("path", "")).strip()
                content = str(params.get("content", ""))

                if not path_str:
                    return {"success": False, "error": "Chemin de fichier manquant"}

                if len(content.encode("utf-8")) > MAX_FILE_BYTES:
                    return {"success": False, "error": "Contenu trop volumineux (> 5 Mo)"}

                target = Path(path_str).expanduser()
                safe, reason = is_path_safe(target, mode="write")
                if not safe:
                    return {"success": False, "error": f"Sécurité : {reason}"}

                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                return {"success": True, "path": str(target.resolve()), "bytes_written": len(content)}

            elif action == "screenshot":
                with tempfile.NamedTemporaryFile(prefix="nc-node-screenshot-", suffix=".png", delete=False) as tmp_f:
                    out_path = Path(tmp_f.name)
                try:
                    os.chmod(out_path, 0o600)
                except Exception:
                    pass

                try:
                    is_wayland = bool(os.getenv("WAYLAND_DISPLAY") or os.getenv("XDG_SESSION_TYPE") == "wayland")
                    grim = shutil.which("grim")

                    captured = False
                    if is_wayland and grim:
                        proc = await asyncio.create_subprocess_exec(
                            grim, str(out_path),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        await proc.wait()
                        captured = out_path.exists() and out_path.stat().st_size > 0

                    if not captured:
                        ffmpeg = shutil.which("ffmpeg")
                        display = os.getenv("DISPLAY", ":0")
                        if ffmpeg:
                            proc = await asyncio.create_subprocess_exec(
                                ffmpeg, "-f", "x11grab", "-video_size", "1920x1080",
                                "-i", display, "-vframes", "1", str(out_path), "-y",
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                            )
                            await proc.wait()
                            captured = out_path.exists() and out_path.stat().st_size > 0

                    if not captured:
                        return {"success": False, "error": "Aucun utilitaire de capture disponible (grim/ffmpeg)"}

                    # Encode en base64 pour transmission facile
                    data = base64.b64encode(out_path.read_bytes()).decode("ascii")
                    return {"success": True, "image_base64": data, "format": "png"}
                finally:
                    try:
                        if out_path.exists():
                            out_path.unlink()
                    except Exception:
                        pass

            elif action == "lock":
                hyprlock = shutil.which("hyprlock")
                loginctl = shutil.which("loginctl")
                if hyprlock:
                    await asyncio.create_subprocess_exec(hyprlock)
                    return {"success": True, "message": "Session verrouillée via hyprlock."}
                elif loginctl:
                    proc = await asyncio.create_subprocess_exec(loginctl, "lock-session")
                    await proc.wait()
                    return {"success": True, "message": "Session verrouillée via loginctl."}
                return {"success": False, "error": "Aucun verrouilleur de session trouvé."}

            elif action == "clipboard_get":
                wl_paste = shutil.which("wl-paste")
                xclip = shutil.which("xclip")
                if wl_paste:
                    proc = await asyncio.create_subprocess_exec(
                        wl_paste, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                    )
                    stdout, _ = await proc.communicate()
                    return {"success": True, "clipboard": stdout.decode(errors="replace")}
                elif xclip:
                    proc = await asyncio.create_subprocess_exec(
                        xclip, "-selection", "clipboard", "-o",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                    )
                    stdout, _ = await proc.communicate()
                    return {"success": True, "clipboard": stdout.decode(errors="replace")}
                return {"success": False, "error": "wl-paste / xclip non trouvé"}

            elif action == "clipboard_set":
                text = str(params.get("text", ""))
                wl_copy = shutil.which("wl-copy")
                if wl_copy:
                    proc = await asyncio.create_subprocess_exec(
                        wl_copy, stdin=asyncio.subprocess.PIPE
                    )
                    await proc.communicate(input=text.encode("utf-8"))
                    return {"success": True, "message": "Presse-papier mis à jour"}
                return {"success": False, "error": "wl-copy non trouvé"}

            elif action == "media":
                playerctl = shutil.which("playerctl")
                subaction = str(params.get("subaction", "play-pause"))
                if not playerctl:
                    return {"success": False, "error": "playerctl introuvable"}
                proc = await asyncio.create_subprocess_exec(
                    playerctl, subaction,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await proc.communicate()
                return {"success": proc.returncode == 0, "output": stdout.decode().strip() or stderr.decode().strip()}

            elif action == "notify":
                title = str(params.get("title", "JARVIS"))
                body = str(params.get("body", ""))
                notify_send = shutil.which("notify-send")
                if notify_send:
                    proc = await asyncio.create_subprocess_exec(notify_send, title, body)
                    await proc.wait()
                    return {"success": True}
                return {"success": False, "error": "notify-send introuvable"}

            elif action == "system_info":
                return {
                    "success": True,
                    "hostname": socket.gethostname(),
                    "platform": platform.platform(),
                    "python": sys.version.split()[0],
                    "cpu_count": os.cpu_count(),
                    "user": os.getenv("USER", "unknown"),
                    "uptime": time.time(),
                }

            else:
                return {"success": False, "error": f"Action non supportée : {action}"}

        except Exception as exc:
            logger.exception("Erreur traitement action %s", action)
            return {"success": False, "error": str(exc)}

    async def run(self) -> None:
        """Boucle principale de maintien de connexion WebSocket avec reconnexion automatique."""
        self._running = True
        ws_url = self.get_ws_url()
        backoff = 3.0

        logger.info("Démarrage du nœud JARVIS '%s' vers %s", self.node_id, self.gateway_url)

        while self._running:
            ws = None
            try:
                ws = await connect_ws(ws_url, self.secret_key)
                logger.info("✅ Connecté au hub Gateway JARVIS avec succès !")
                backoff = 3.0

                # 1. Enregistrement initial
                reg_payload = {
                    "type": "register",
                    "node_id": self.node_id,
                    "hostname": self.hostname,
                    "os": sys.platform,
                    "capabilities": self.capabilities,
                    "metadata": {
                        "user": os.getenv("USER", "unknown"),
                        "platform": platform.platform(),
                    },
                }
                await ws.send(json.dumps(reg_payload))

                # 2. Boucle de réception des commandes
                while self._running:
                    try:
                        raw_msg = await ws.recv()
                    except (ConnectionResetError, EOFError):
                        break
                    if not raw_msg:
                        continue

                    try:
                        msg = json.loads(raw_msg)
                    except Exception:
                        continue

                    msg_type = msg.get("type")
                    req_id = msg.get("req_id")

                    if msg_type == "ping":
                        await ws.send(json.dumps({"type": "pong"}))
                        continue

                    if msg_type == "action" and req_id:
                        action = msg.get("action", "")
                        params = msg.get("params", {})
                        logger.info("⚡ Action reçue : %s (req_id=%s)", action, req_id)
                        res = await self.handle_action(action, params)
                        resp_payload = {
                            "type": "response",
                            "req_id": req_id,
                            **res,
                        }
                        await ws.send(json.dumps(resp_payload))

            except asyncio.CancelledError:
                logger.info("Arrêt de l'agent nœud demandé.")
                break
            except Exception as exc:
                logger.warning("Connexion perdue ou échec : %s. Nouvelle tentative dans %.1fs...", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 30.0)
            finally:
                if ws:
                    try:
                        await ws.close()
                    except Exception:
                        pass


def load_config() -> Dict[str, str]:
    """Lit la configuration locale depuis ~/.config/nc/assistant.conf."""
    cfg = {}
    if CONF_PATH.exists():
        try:
            for line in CONF_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip().strip('"').strip("'")
        except Exception as exc:
            logger.debug("Lecture configuration impossible: %s", exc)
    return cfg


def main():
    parser = argparse.ArgumentParser(description="Agent client JARVIS Node")
    parser.add_argument("--url", help="URL de la Gateway JARVIS (ex: http://100.89.134.15:8000)")
    parser.add_argument("--secret", help="Clé secrète JARVIS")
    parser.add_argument("--node-id", help="Identifiant du nœud (défaut: hostname)")
    args = parser.parse_args()

    cfg = load_config()
    url = args.url or os.getenv("JARVIS_GATEWAY_URL") or cfg.get("URL")
    secret = args.secret or os.getenv("JARVIS_SECRET_KEY") or cfg.get("SECRET_KEY")
    node_id = args.node_id or os.getenv("JARVIS_NODE_ID") or cfg.get("CLIENT_ID") or socket.gethostname()

    if not url or not secret:
        print("Erreur : URL et SECRET_KEY doivent être fournis via arguments, variables d'environnement ou ~/.config/nc/assistant.conf", file=sys.stderr)
        sys.exit(1)

    agent = NodeAgent(gateway_url=url, secret_key=secret, node_id=node_id)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        print("\nArrêt de l'agent nœud.")


if __name__ == "__main__":
    main()
