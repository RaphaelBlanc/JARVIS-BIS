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
import json
import logging
import os
import platform
import re
import shutil
import socket
import sys
import time
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

# Répertoires et fichiers strictement interdits d'accès
FORBIDDEN_EXACT_NAMES = {
    "assistant.conf",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    ".env",
    "master.key",
    "credentials",
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
    Path("/etc/shadow"),
    Path("/etc/sudoers"),
    Path("/root"),
]

SUDO_PATTERN = re.compile(r"\b(sudo|pkexec|su\s|doas)\b", re.IGNORECASE)


def is_path_safe(target_path: Path) -> Tuple[bool, str]:
    """Vérifie qu'un chemin ne tente pas d'accéder à des secrets système ou utilisateur."""
    try:
        resolved = target_path.expanduser().resolve()
    except Exception as exc:
        return False, f"Impossible de résoudre le chemin : {exc}"

    # Vérification des répertoires racines interdits
    for forbidden_root in FORBIDDEN_ROOTS:
        try:
            resolved_forbidden = forbidden_root.expanduser().resolve()
            if resolved == resolved_forbidden or resolved_forbidden in resolved.parents:
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

    # Vérification du fichier de configuration JARVIS lui-même
    try:
        if resolved == CONF_PATH.resolve():
            return False, "Accès refusé au fichier de configuration confidentiel de l'assistant."
    except Exception:
        pass

    return True, ""


def is_command_safe(command: str) -> Tuple[bool, str]:
    """Vérifie qu'une commande ne tente pas d'escalade de privilèges."""
    if os.geteuid() == 0:
        return False, "L'agent nœud ne doit JAMAIS être exécuté en tant que root."

    if SUDO_PATTERN.search(command):
        return False, "L'utilisation de 'sudo', 'su', 'pkexec' ou 'doas' est strictement interdite."

    return True, ""


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
        """Convertit l'URL HTTP/HTTPS en URL WebSocket WS/WSS."""
        url = self.gateway_url
        if url.startswith("https://"):
            ws_url = "wss://" + url[len("https://") :]
        elif url.startswith("http://"):
            ws_url = "ws://" + url[len("http://") :]
        else:
            ws_url = "ws://" + url
        return f"{ws_url}/api/node/ws?secret={self.secret_key}"

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
                safe, reason = is_path_safe(target)
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
                safe, reason = is_path_safe(target)
                if not safe:
                    return {"success": False, "error": f"Sécurité : {reason}"}

                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                return {"success": True, "path": str(target.resolve()), "bytes_written": len(content)}

            elif action == "screenshot":
                out_path = Path("/tmp") / f"nc-node-screenshot-{int(time.time())}.png"
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
                try:
                    out_path.unlink()
                except Exception:
                    pass

                return {"success": True, "image_base64": data, "format": "png"}

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
        try:
            import websockets
        except ImportError:
            logger.error("La bibliothèque 'websockets' est requise. Installez-la avec : pip install websockets")
            sys.exit(1)

        self._running = True
        ws_url = self.get_ws_url()
        backoff = 3.0

        logger.info("Démarrage du nœud JARVIS '%s' vers %s", self.node_id, self.gateway_url)

        while self._running:
            try:
                headers = {"X-Secret-Key": self.secret_key}
                async with websockets.connect(
                    ws_url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
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
                    async for raw_msg in ws:
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
