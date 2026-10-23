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
        timeout: float = 35.0,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Exécute une action matérielle directement sur le système hôte."""
        params = params or {}

        try:
            import base64
            import tempfile
            from core.node_hub import NODE_HUB
            from core.tools import system_tools

            # Actions nécessitant un affichage graphique / Wayland
            is_wayland = bool(os.getenv("WAYLAND_DISPLAY") or os.getenv("XDG_SESSION_TYPE") == "wayland")
            has_display = bool(os.getenv("DISPLAY") or is_wayland)
            node_target = params.get("node_id") or params.get("node")
            target_node = NODE_HUB.get_node(node_target)

            # Si un nœud spécifique est ciblé ou si le serveur est headless et qu'un nœud client est connecté
            # Sécurité : seules les actions graphiques/multimédia sont relayées automatiquement en headless (pas run_shell)
            if target_node and (node_target or (not has_display and action in ("screenshot", "lock", "clipboard_set", "clipboard_get", "media", "notify"))):
                if action == "run_shell":
                    if os.getenv("ENABLE_NODE_EXEC", "0").lower() not in ("1", "true", "yes"):
                        return False, "L'exécution distante de commandes (exec) sur les nœuds clients requiert ENABLE_NODE_EXEC=1 côté serveur.", None
                    if not node_target or str(node_target).strip().lower() in ("", "auto", "default", "any"):
                        return False, "Sécurité : un identifiant de nœud client explicite est obligatoire pour exécuter des commandes.", None
                res = await NODE_HUB.send_action(target_node.node_id, "exec" if action == "run_shell" else action, params, timeout=timeout)
                if action == "screenshot" and res.get("success") and res.get("image_base64"):
                    user_out = params.get("output_path")
                    if user_out:
                        out_path = Path(user_out)
                    else:
                        with tempfile.NamedTemporaryFile(prefix="nc_screenshot_", suffix=".png", delete=False) as tmp_f:
                            out_path = Path(tmp_f.name)
                    out_path.write_bytes(base64.b64decode(res["image_base64"]))
                    return True, f"Capture effectuée sur le nœud {target_node.node_id}.", {"file_path": str(out_path)}
                if res.get("success"):
                    return True, res.get("message") or f"Action '{action}' exécutée sur le nœud {target_node.node_id}.", res
                return False, res.get("error") or f"Échec de l'action sur le nœud {target_node.node_id}.", None

            if action in ("screenshot", "lock", "cam") and not has_display and not os.path.exists("/dev/video0"):
                return (
                    False,
                    "⚠️ <b>Commande matérielle indisponible</b>\n"
                    "L'assistant tourne actuellement sur un serveur distant sans session graphique ou sans webcam connectée (aucun nœud client connecté).",
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
