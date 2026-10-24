"""
core/hardware_relay.py — Exécution directe des outils matériels et système.

Exécute les commandes locales (screenshot grim, webcam ffmpeg, verrouillage de session,
contrôle du son, fenêtres Hyprland, etc.).
Si le bot est hébergé sur un serveur headless sans session graphique, renvoie un message
d'information clair sans faire planter l'application.
"""

import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("core.hardware_relay")

_FORBIDDEN_PATH_PREFIXES = (
    "/etc", "/bin", "/sbin", "/usr", "/boot", "/dev", "/sys", "/proc", "/root"
)


def _sanitize_output_path(
    user_path: Optional[str],
    default_prefix: str = "nc_screenshot_",
    default_suffix: str = ".png",
    initial_bytes: Optional[bytes] = None,
) -> Tuple[Optional[Path], Optional[str]]:
    """Sanitise le chemin de sortie dans un dossier temporaire fixe (O_NOFOLLOW|O_EXCL) et nettoie les vieux temporaires."""
    tmp_dir = Path(tempfile.gettempdir()).resolve()
    try:
        now = time.time()
        for pat in ("nc_screenshot_*.png", "nc_cam_*.jpg"):
            for old_tmp in tmp_dir.glob(pat):
                if now - old_tmp.stat().st_mtime > 300:
                    old_tmp.unlink(missing_ok=True)
    except Exception:
        pass

    if not user_path:
        with tempfile.NamedTemporaryFile(prefix=default_prefix, suffix=default_suffix, dir=str(tmp_dir), delete=False) as tmp_f:
            if initial_bytes is not None:
                tmp_f.write(initial_bytes)
                tmp_f.flush()
            return Path(tmp_f.name), None

    raw_path = Path(user_path).expanduser()
    # Refuse tout fichier caché (dotfile : .bashrc, .ssh, .config, etc.)
    if any(part.startswith(".") for part in raw_path.parts):
        return None, f"Chemin de sortie interdit : {user_path}"

    try:
        if raw_path.is_symlink():
            return None, f"Chemin de sortie interdit : {user_path}"
        resolved = raw_path.resolve()
    except OSError:
        return None, f"Chemin de sortie interdit : {user_path}"

    resolved_str = str(resolved)
    if any(resolved_str == prefix or resolved_str.startswith(f"{prefix}/") for prefix in _FORBIDDEN_PATH_PREFIXES):
        return None, f"Chemin de sortie interdit : {user_path}"

    try:
        is_sym = resolved.is_symlink()
    except OSError:
        return None, f"Chemin de sortie interdit : {user_path}"

    # N'autorise l'écriture que directement sous /tmp avec O_NOFOLLOW | O_EXCL
    if resolved.parent != tmp_dir or is_sym:
        return None, f"Chemin de sortie interdit (seul {tmp_dir} est autorisé) : {user_path}"

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(resolved), flags, 0o600)
        with os.fdopen(fd, "wb") as f:
            if initial_bytes is not None:
                f.write(initial_bytes)
                f.flush()
    except OSError:
        return None, f"Chemin de sortie interdit : {user_path}"

    return resolved, None


class HardwareRelay:
    """Gestionnaire des actions matérielles et système."""

    async def execute(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 80.0,
        session_key: Optional[str] = None,
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
            # Sécurité : seules les actions graphiques/multimédia sont relayées automatiquement en headless (pas run_shell/exec/write_file)
            if target_node and (node_target or (not has_display and action in ("screenshot", "lock", "clipboard_set", "clipboard_get", "media", "notify"))):
                remote_action = "exec" if action == "run_shell" else action
                if remote_action in ("exec", "write_file"):
                    if not node_target or str(node_target).strip().lower() in ("", "auto", "default", "any"):
                        return False, f"Sécurité : un identifiant de nœud client explicite est obligatoire pour '{remote_action}'.", None
                if remote_action == "exec":
                    if os.getenv("ENABLE_NODE_EXEC", "0").lower() not in ("1", "true", "yes"):
                        return False, "L'exécution distante de commandes (exec) sur les nœuds clients requiert ENABLE_NODE_EXEC=1 côté serveur.", None
                origin = NODE_HUB.build_origin(session_key=session_key) if session_key else None
                res = await NODE_HUB.send_action(target_node.node_id, remote_action, params, timeout=timeout, origin=origin)
                if action == "screenshot" and res.get("success") and res.get("image_base64"):
                    user_out = params.get("output_path")
                    if user_out:
                        img_bytes = base64.b64decode(res["image_base64"])
                        out_path, err = _sanitize_output_path(
                            user_out,
                            default_prefix="nc_screenshot_",
                            default_suffix=".png",
                            initial_bytes=img_bytes,
                        )
                        if err or not out_path:
                            return False, err or "Chemin de sortie invalide.", None
                        return True, f"Capture effectuée sur le nœud {target_node.node_id}.", {
                            "file_path": str(out_path),
                            "image_base64": res["image_base64"],
                        }
                    return True, f"Capture effectuée sur le nœud {target_node.node_id}.", {
                        "image_base64": res["image_base64"],
                    }
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
                out_path, err = _sanitize_output_path(params.get("output_path"), default_prefix="nc_screenshot_", default_suffix=".png")
                if err or not out_path:
                    return False, err or "Chemin de sortie invalide.", None
                ok, msg = await system_tools.take_screenshot(out_path)
                return ok, msg, {"file_path": str(out_path)} if ok else None

            elif action == "lock":
                ok, msg = await system_tools.lock_desktop_session()
                return ok, msg, None

            elif action == "cam":
                out_path, err = _sanitize_output_path(params.get("output_path"), default_prefix="nc_cam_", default_suffix=".jpg")
                if err or not out_path:
                    return False, err or "Chemin de sortie invalide.", None
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
                windows = await system_tools.list_hyprland_windows()
                if not windows:
                    msg = "🪟 Aucune fenêtre Hyprland détectée."
                else:
                    lines = [f"🪟 <b>Fenêtres ouvertes ({len(windows)}) :</b>"]
                    for w in windows[:15]:
                        lines.append(f"• <b>[{w.get('workspace')}] {w.get('class')}</b> — {w.get('title', '')[:60]} (PID {w.get('pid')})")
                    msg = "\n".join(lines)
                return True, msg, {"windows": windows}

            elif action == "top":
                procs = await system_tools.get_top_processes()
                if not procs:
                    msg = "ℹ️ Impossible de lire les processus."
                else:
                    lines = ["⚙️ <b>Processus les plus gourmands (RAM) :</b>"]
                    for p in procs:
                        lines.append(f"• <code>{p['pid']}</code> <b>{p['comm']}</b> — RAM: {p['mem']}% | CPU: {p['cpu']}%")
                    msg = "\n".join(lines)
                return True, msg, {"processes": procs}

            elif action == "kill":
                target = params.get("target", "")
                force = bool(params.get("force", False))
                ok, msg = await system_tools.terminate_process(target, force=force)
                return ok, msg, None

            elif action == "git_scan":
                repos = await system_tools.scan_all_git_repos()
                summary = system_tools.format_git_summary(repos)
                return True, summary, {"repos": repos}

            elif action == "hardware_health":
                alerts, metrics = system_tools.check_hardware_health()
                lines = ["🖥️ <b>Diagnostic Matériel & Système :</b>"]
                if "ram_pct" in metrics:
                    lines.append(f"• <b>RAM :</b> {metrics.get('ram_used_gb', 0)}/{metrics.get('ram_tot_gb', 0)} Go ({metrics['ram_pct']}%)")
                if "disk_free_gb" in metrics:
                    lines.append(f"• <b>Disque :</b> {metrics['disk_free_gb']} Go libres ({metrics.get('disk_pct', 0)}% utilisé)")
                if "battery_cap" in metrics:
                    lines.append(f"• <b>Batterie :</b> {metrics['battery_cap']}% ({metrics.get('battery_status', 'N/A')})")
                if alerts:
                    lines.append("\n⚠️ <b>Alertes :</b>\n" + "\n".join(alerts))
                return True, "\n".join(lines), {"alerts": alerts, "metrics": metrics}

            elif action == "run_shell":
                cmd = params.get("command", "")
                duration_limit = int(params.get("timeout", 60))
                code, out, err, dur = await system_tools.run_shell_task(cmd, timeout=duration_limit)
                return code == 0, f"Code {code} ({dur:.1f}s)\n{out}\n{err}".strip(), {"returncode": code, "stdout": out, "stderr": err}

            else:
                return False, f"Action matérielle inconnue : {action}", None

        except Exception as exc:
            logger.error(f"Erreur exécution matérielle {action}: {exc}", exc_info=True)
            return False, f"Erreur système ({action}) : {exc}", None


# Instance singleton globale
HARDWARE_RELAY = HardwareRelay()
