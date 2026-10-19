"""
pc-agent/agent.py — Démon client local pour l'architecture hybride.

Ce script tourne sur votre PC personnel (Fedora/Arch Linux avec Wayland/Hyprland)
et maintient une connexion WebSocket sortante sécurisée vers le serveur hébergé distant.

Il permet d'exécuter à distance sur votre PC les actions matérielles demandées
sur Telegram, Discord ou Noctalia :
  - Capture d'écran (grim)
  - Photo webcam discrète (ffmpeg /dev/video0)
  - Verrouillage de session (loginctl)
  - Presse-papier (wl-copy, wl-paste)
  - Synthèse vocale sur les haut-parleurs (spd-say)
  - Contrôle multimédia & volume (playerctl, wpctl)
  - Gestion des fenêtres et processus (hyprctl, kill, top)
  - Surveillance hardware (batterie, RAM, disque)
"""

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
import sys
import time

import aiohttp
from dotenv import load_dotenv

# Ajoute la racine du projet au PYTHONPATH pour importer core.tools
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [pc-agent] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("pc-agent")

SERVER_URL = os.getenv("HOME_SERVER_URL", os.getenv("GATEWAY_PUBLIC_URL", "http://127.0.0.1:8000")).rstrip("/")
SECRET_KEY = os.getenv("SECRET_KEY", "")


async def execute_local_hardware_action(action: str, params: dict) -> dict:
    """Exécute l'action demandée localement et retourne le dictionnaire de réponse."""
    from core.tools import system_tools

    params = params or {}
    logger.info(f"Exécution locale de l'action: {action} (params: {params})")

    try:
        if action == "screenshot":
            tmp_out = Path("/tmp/agent_screenshot.png")
            ok, msg = await system_tools.take_screenshot(tmp_out)
            data = {}
            if ok and tmp_out.exists():
                data["image_base64"] = base64.b64encode(tmp_out.read_bytes()).decode("utf-8")
                data["filename"] = "screenshot.png"
            return {"success": ok, "message": msg, "data": data}

        elif action == "cam":
            tmp_out = Path("/tmp/agent_cam.jpg")
            ok, msg = await system_tools.capture_webcam(tmp_out)
            data = {}
            if ok and tmp_out.exists():
                data["image_base64"] = base64.b64encode(tmp_out.read_bytes()).decode("utf-8")
                data["filename"] = "webcam.jpg"
            return {"success": ok, "message": msg, "data": data}

        elif action == "lock":
            ok, msg = await system_tools.lock_session()
            return {"success": ok, "message": msg}

        elif action == "media":
            sub = params.get("subaction", "play-pause")
            val = params.get("value", "")
            ok, msg = await system_tools.media_action(sub, val)
            return {"success": ok, "message": msg}

        elif action == "clipboard_set":
            text = params.get("text", "")
            ok, msg = await system_tools.set_clipboard(text)
            return {"success": ok, "message": msg}

        elif action == "clipboard_get":
            ok, content = await system_tools.get_clipboard()
            return {"success": ok, "message": content, "data": {"clipboard": content}}

        elif action == "say":
            text = params.get("text", "")
            ok, msg = await system_tools.speak_text(text)
            return {"success": ok, "message": msg}

        elif action == "windows":
            ok, windows = await system_tools.get_windows_list()
            return {"success": ok, "message": "Liste des fenêtres récupérée.", "data": {"windows": windows}}

        elif action == "top":
            ok, msg = await system_tools.get_top_processes()
            return {"success": ok, "message": msg}

        elif action == "kill":
            target = params.get("target", "")
            ok, msg = await system_tools.kill_process_by_target(target)
            return {"success": ok, "message": msg}

        elif action == "git_scan":
            summary = await system_tools.scan_all_git_repos()
            return {"success": True, "message": summary}

        elif action == "hardware_health":
            ok, msg, health_data = await system_tools.check_hardware_health()
            return {"success": ok, "message": msg, "data": health_data}

        elif action == "run_shell":
            cmd = params.get("command", "")
            duration = params.get("timeout", 60)
            code, out, err, dur = await system_tools.run_shell_task(cmd, duration_limit=duration)
            msg = f"Code retour {code} ({dur:.1f}s)\n{out}\n{err}".strip()
            return {"success": code == 0, "message": msg, "data": {"returncode": code, "stdout": out, "stderr": err}}

        else:
            return {"success": False, "message": f"Action locale inconnue: {action}"}

    except Exception as exc:
        logger.error(f"Erreur lors de l'exécution de {action}: {exc}", exc_info=True)
        return {"success": False, "message": f"Erreur PC local ({action}) : {exc}"}


async def run_agent() -> None:
    """Boucle principale maintenant la connexion WebSocket persistante vers le serveur."""
    if not SECRET_KEY:
        logger.error("SECRET_KEY n'est pas défini dans l'environnement.")
        return

    # Calcule l'URL WebSocket
    ws_url = SERVER_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws/pc-agent"
    logger.info(f"Démarrage de l'agent PC. Cible serveur: {ws_url}")

    backoff = 2
    while True:
        try:
            headers = {"X-Secret-Key": SECRET_KEY}
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url, headers=headers, heartbeat=25.0) as ws:
                    logger.info("✅ Connecté au serveur assistant distant.")
                    backoff = 2  # Réinitialise le délai de reconnexion

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                payload = json.loads(msg.data)
                                req_id = payload.get("request_id")
                                action = payload.get("action")
                                params = payload.get("params", {})

                                if req_id and action:
                                    logger.info(f"Requête reçue: {action} (ID: {req_id})")
                                    result = await execute_local_hardware_action(action, params)
                                    result["request_id"] = req_id
                                    await ws.send_json(result)
                            except Exception as exc:
                                logger.error(f"Erreur traitement message: {exc}")

                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning("Connexion WebSocket fermée par le serveur.")
                            break

        except asyncio.CancelledError:
            logger.info("Arrêt de l'agent PC.")
            break
        except Exception as exc:
            logger.warning(f"Erreur de connexion serveur: {exc}. Reconnexion dans {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 30)


if __name__ == "__main__":
    try:
        asyncio.run(run_agent())
    except KeyboardInterrupt:
        logger.info("Agent arrêté manuellement.")
