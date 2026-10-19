"""
core/hardware_relay.py — Exécution directe des outils matériels et système.

Exécute les commandes locales (screenshot grim, webcam ffmpeg, verrouillage de session,
contrôle du son, fenêtres Hyprland, etc.).
Si le bot est hébergé sur un serveur headless sans session graphique, renvoie un message
d'information clair sans faire planter l'application.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("core.hardware_relay")


class HardwareRelay:
    """Gestionnaire des actions matérielles et système."""

    async def execute(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 20.0,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Exécute une action matérielle directement sur le système hôte."""
        params = params or {}

        try:
            from core.tools import system_tools

            # Actions nécessitant un affichage graphique / Wayland
            is_wayland = bool(os.getenv("WAYLAND_DISPLAY") or os.getenv("XDG_SESSION_TYPE") == "wayland")
            has_display = bool(os.getenv("DISPLAY") or is_wayland)

            if action in ("screenshot", "lock", "cam") and not has_display and not os.path.exists("/dev/video0"):
                return (
                    False,
                    "⚠️ <b>Commande matérielle indisponible</b>\n"
                    "L'assistant tourne actuellement sur un serveur distant sans session graphique ou sans webcam connectée.",
                    None,
                )

            if action == "screenshot":
                out_path = Path(params.get("output_path", "/tmp/screenshot.png"))
                ok, msg = await system_tools.take_screenshot(out_path)
                return ok, msg, {"file_path": str(out_path)} if ok else None

            elif action == "lock":
                ok, msg = await system_tools.lock_desktop_session()
                return ok, msg, None

            elif action == "cam":
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
            logger.error(f"Erreur exécution matérielle {action}: {exc}", exc_info=True)
            return False, f"Erreur système ({action}) : {exc}", None


# Instance singleton globale
HARDWARE_RELAY = HardwareRelay()
