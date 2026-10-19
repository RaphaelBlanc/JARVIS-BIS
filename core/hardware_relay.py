"""
core/hardware_relay.py — Passerelle matérielle hybride (Local vs Serveur + PC-Agent).

Ce module permet d'exécuter des actions matérielles Linux/Wayland (capture d'écran,
webcam, verrouillage de session, contrôle du son, presse-papier, etc.) :
1. En mode LOCAL : exécution directe sur la machine via `core.tools.system_tools`.
2. En mode SERVEUR : relais transparent vers un `pc-agent` connecté en WebSocket.
   Si le PC est éteint ou non connecté, renvoie un message d'information gracieux.
"""

import asyncio
import logging
import os
import uuid
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("core.hardware_relay")


class HardwareRelay:
    """Gestionnaire central des actions matérielles pour l'architecture hybride."""

    def __init__(self) -> None:
        self._connected_agents: Dict[str, Any] = {}  # {agent_id: websocket}
        self._pending_requests: Dict[str, asyncio.Future] = {}

    @property
    def is_local_mode(self) -> bool:
        """Indique si l'application tourne en mode local direct."""
        mode = os.getenv("DEPLOY_MODE", "local").strip().lower()
        return mode in ("local", "standalone", "direct")

    @property
    def is_agent_connected(self) -> bool:
        """Indique si au moins un agent PC local est actuellement connecté."""
        return len(self._connected_agents) > 0

    def register_agent(self, agent_id: str, websocket: Any) -> None:
        """Enregistre un nouvel agent PC connecté."""
        self._connected_agents[agent_id] = websocket
        logger.info(f"Agent PC connecté : {agent_id} (total connectés: {len(self._connected_agents)})")

    def unregister_agent(self, agent_id: str) -> None:
        """Déconnecte un agent PC."""
        self._connected_agents.pop(agent_id, None)
        logger.info(f"Agent PC déconnecté : {agent_id} (total connectés: {len(self._connected_agents)})")

    def handle_agent_response(self, response_data: dict) -> None:
        """Reçoit la réponse d'un agent pour une requête en attente."""
        req_id = response_data.get("request_id")
        if req_id and req_id in self._pending_requests:
            future = self._pending_requests.pop(req_id)
            if not future.done():
                future.set_result(response_data)

    async def execute(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 20.0,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Exécute une action matérielle (en local direct ou via le relais WebSocket).

        Returns:
            Tuple[bool, str, Optional[dict]] -> (succès, message, données_supplémentaires)
        """
        params = params or {}

        # 1. Mode LOCAL direct
        if self.is_local_mode:
            return await self._execute_local(action, params)

        # 2. Mode SERVEUR : vérifier si un agent PC est en ligne
        if not self._connected_agents:
            return (
                False,
                "⚠️ <b>PC local hors ligne</b>\n"
                "Cette commande nécessite l'accès au matériel (Wayland/écran/audio/webcam), "
                "mais aucun agent PC local n'est actuellement connecté au serveur distant.\n\n"
                "💡 <i>Lancez l'agent local sur votre PC via <code>systemctl --user start assistant-pc-agent</code>.</i>",
                None,
            )

        # Prendre le premier agent disponible
        agent_id, ws = next(iter(self._connected_agents.items()))
        req_id = uuid.uuid4().hex[:12]
        payload = {
            "request_id": req_id,
            "action": action,
            "params": params,
        }

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_requests[req_id] = future

        try:
            await ws.send_json(payload)
            response = await asyncio.wait_for(future, timeout=timeout)
            success = bool(response.get("success", False))
            message = str(response.get("message", ""))
            data = response.get("data")
            return success, message, data
        except asyncio.TimeoutError:
            self._pending_requests.pop(req_id, None)
            return False, f"⏱️ Délai d'attente dépassé ({timeout}s) pour l'action {action} sur le PC distant.", None
        except Exception as exc:
            self._pending_requests.pop(req_id, None)
            logger.error(f"Erreur transmission agent PC ({action}): {exc}")
            return False, f"❌ Erreur de communication avec le PC local : {exc}", None

    async def _execute_local(self, action: str, params: Dict[str, Any]) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Exécution directe sur le système d'exploitation local."""
        try:
            from core.tools import system_tools

            if action == "screenshot":
                from pathlib import Path
                out_path = Path(params.get("output_path", "/tmp/screenshot.png"))
                ok, msg = await system_tools.take_screenshot(out_path)
                return ok, msg, {"file_path": str(out_path)} if ok else None

            elif action == "lock":
                ok, msg = await system_tools.lock_session()
                return ok, msg, None

            elif action == "cam":
                from pathlib import Path
                out_path = Path(params.get("output_path", "/tmp/cam_snapshot.jpg"))
                ok, msg = await system_tools.capture_webcam(out_path)
                return ok, msg, {"file_path": str(out_path)} if ok else None

            elif action == "media":
                subaction = params.get("subaction", "play-pause")
                val = params.get("value", "")
                ok, msg = await system_tools.media_action(subaction, val)
                return ok, msg, None

            elif action == "clipboard_set":
                text = params.get("text", "")
                ok, msg = await system_tools.set_clipboard(text)
                return ok, msg, None

            elif action == "clipboard_get":
                ok, content = await system_tools.get_clipboard()
                return ok, content, {"clipboard": content}

            elif action == "say":
                text = params.get("text", "")
                ok, msg = await system_tools.speak_text(text)
                return ok, msg, None

            elif action == "windows":
                ok, data = await system_tools.get_windows_list()
                return ok, "Liste des fenêtres récupérée.", {"windows": data}

            elif action == "top":
                ok, msg = await system_tools.get_top_processes()
                return ok, msg, None

            elif action == "kill":
                target = params.get("target", "")
                ok, msg = await system_tools.kill_process_by_target(target)
                return ok, msg, None

            elif action == "git_scan":
                home_dir = params.get("home_dir")
                summary = await system_tools.scan_all_git_repos(home_dir)
                return True, summary, None

            elif action == "hardware_health":
                ok, msg, health_data = await system_tools.check_hardware_health()
                return ok, msg, health_data

            elif action == "run_shell":
                cmd = params.get("command", "")
                duration_limit = params.get("timeout", 60)
                code, out, err, dur = await system_tools.run_shell_task(cmd, duration_limit=duration_limit)
                return code == 0, f"Code {code} ({dur:.1f}s)\n{out}\n{err}".strip(), {"returncode": code, "stdout": out, "stderr": err}

            else:
                return False, f"Action matérielle inconnue : {action}", None

        except Exception as exc:
            logger.error(f"Erreur exécution locale {action}: {exc}", exc_info=True)
            return False, f"Erreur locale : {exc}", None


# Instance singleton globale
HARDWARE_RELAY = HardwareRelay()
