"""
agy_bridge.py — Module de communication avec Antigravity CLI (agy).

Lance `agy` en subprocess non-interactif, envoie le prompt via stdin,
capture stdout/stderr et retourne un dict structuré.
"""

import asyncio
import logging
import os
import re
import shutil
from typing import Optional

logger = logging.getLogger(__name__)

# Séquences ANSI : couleurs, attributs, déplacements curseur, etc.
_ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def strip_ansi(text: str) -> str:
    """Supprime toutes les séquences d'échappement ANSI d'une chaîne."""
    return _ANSI_ESCAPE.sub('', text)


def find_agy() -> Optional[str]:
    """
    Recherche l'exécutable `agy` dans l'ordre :
    1. PATH système (via shutil.which)
    2. Emplacements connus (répertoire Antigravity CLI, /usr/local/bin)

    Returns:
        Chemin absolu vers l'exécutable, ou None si introuvable.
    """
    # 1. Recherche dans PATH
    path = shutil.which("agy")
    if path:
        logger.debug("agy trouvé via PATH : %s", path)
        return path

    # 2. Emplacements de repli
    candidates = [
        os.path.expanduser("~/.local/bin/agy"),
        os.path.expanduser("~/.gemini/antigravity-cli/agy"),
        "/usr/local/bin/agy",
        "/usr/bin/agy",
    ]
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            logger.debug("agy trouvé dans les emplacements connus : %s", candidate)
            return candidate

    logger.error("agy introuvable dans PATH ni dans les emplacements connus.")
    return None


# Configuration de session en mémoire
SESSION_CONFIG = {
    "model": "gemini-3.8-flash-high",
    "effort": None,
    "mode": None,
    "has_active_session": False,
    "permission_mode": "always-proceed",  # always-proceed | request-review
}

KNOWN_MODELS = [
    {"id": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash (High)", "desc": "Rapide et performant (recommandé)"},
    {"id": "gemini-3.8-flash-medium", "name": "Gemini 3.8 Flash (Medium)", "desc": "Équilibré"},
    {"id": "gemini-3.8-flash-low", "name": "Gemini 3.8 Flash (Low)", "desc": "Très rapide, économe"},
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6 (Thinking)", "desc": "Raisonnement avancé et code"},
    {"id": "claude-opus-4-6-thinking", "name": "Claude Opus 4.6 (Thinking)", "desc": "Capacité maximale"},
    {"id": "gpt-oss-120b-medium", "name": "GPT-OSS 120B (Medium)", "desc": "Open Source"},
]


def get_session_info() -> dict:
    """Retourne la configuration actuelle de la session."""
    return dict(SESSION_CONFIG)


def update_session(
    model: Optional[str] = None,
    effort: Optional[str] = None,
    mode: Optional[str] = None,
    permission_mode: Optional[str] = None,
) -> dict:
    """Met à jour les paramètres de session."""
    if model:
        SESSION_CONFIG["model"] = model.strip()
    if effort:
        SESSION_CONFIG["effort"] = effort.strip().lower()
    if mode:
        SESSION_CONFIG["mode"] = mode.strip().lower()
    if permission_mode:
        SESSION_CONFIG["permission_mode"] = permission_mode.strip().lower()
    return dict(SESSION_CONFIG)


def reset_session() -> dict:
    """Réinitialise la session conversationnelle (démarre une nouvelle conversation vierge)."""
    SESSION_CONFIG["has_active_session"] = False
    return dict(SESSION_CONFIG)


async def run_agy(
    prompt: str,
    timeout: int = 120,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    mode: Optional[str] = None,
    continue_session: bool = True,
    reset_session_flag: bool = False,
    permission_mode: Optional[str] = None,
) -> dict:
    """
    Lance `agy` en subprocess non-interactif (-p / --print) avec support de continuité conversationnelle.

    Args:
        prompt: Texte de la requête.
        timeout: Délai d'attente en secondes.
        model: Modèle spécifique (ou celui de session par défaut).
        effort: Niveau d'effort de raisonnement (low|medium|high|xhigh|max).
        mode: Mode d'exécution (accept-edits|auto|plan).
        continue_session: Si True, reprend la conversation en cours (-c).
        reset_session_flag: Si True, force le démarrage d'une nouvelle session.
        permission_mode: always-proceed ou request-review.
    """
    agy_path = find_agy()
    if not agy_path:
        return {
            "success": False,
            "output": "",
            "error": "agy introuvable dans PATH et dans les emplacements connus.",
            "session": dict(SESSION_CONFIG),
        }

    if reset_session_flag:
        SESSION_CONFIG["has_active_session"] = False

    # Construction des arguments agy
    cmd = [agy_path]

    # Continuité de conversation (-c)
    if continue_session and SESSION_CONFIG["has_active_session"] and not reset_session_flag:
        cmd.append("-c")

    # Modèle
    active_model = model or SESSION_CONFIG.get("model")
    if active_model:
        cmd.extend(["--model", active_model])

    # Effort de raisonnement
    active_effort = effort or SESSION_CONFIG.get("effort")
    if active_effort:
        cmd.extend(["--effort", active_effort])

    # Mode d'exécution
    active_mode = mode or SESSION_CONFIG.get("mode")
    if active_mode:
        cmd.extend(["--mode", active_mode])

    # Permissions
    perm = permission_mode or SESSION_CONFIG.get("permission_mode", "always-proceed")
    if perm == "always-proceed":
        cmd.append("--dangerously-skip-permissions")

    # Prompt et format
    cmd.extend(["-p", prompt, "--output-format", "text"])

    # Environnement épuré
    env = {
        **os.environ,
        "TERM": "dumb",
        "NO_COLOR": "1",
        "FORCE_COLOR": "0",
        "COLORTERM": "",
    }

    logger.info("Exécution agy: %s (timeout=%ds)", " ".join(cmd[:6]), timeout)

    proc: Optional[asyncio.subprocess.Process] = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )

        out = strip_ansi(stdout_bytes.decode(errors="replace")).strip()
        err = strip_ansi(stderr_bytes.decode(errors="replace")).strip()
        success = proc.returncode == 0

        if success:
            SESSION_CONFIG["has_active_session"] = True
            logger.info("agy terminé avec succès (code 0). Sortie : %d caractères.", len(out))
        else:
            logger.warning(
                "agy terminé avec code %d. stderr : %.200s",
                proc.returncode,
                err or "(vide)",
            )

        return {
            "success": success,
            "output": out,
            "error": err if err else None,
            "session": dict(SESSION_CONFIG),
        }

    except asyncio.TimeoutError:
        logger.error("agy a dépassé le timeout de %ds.", timeout)
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except Exception as kill_exc:  # noqa: BLE001
                logger.debug("Erreur lors du kill du subprocess : %s", kill_exc)
        return {
            "success": False,
            "output": "",
            "error": f"Timeout dépassé ({timeout}s). Le processus a été interrompu.",
            "session": dict(SESSION_CONFIG),
        }

    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur inattendue lors de l'exécution de agy : %s", exc)
        return {
            "success": False,
            "output": "",
            "error": str(exc),
            "session": dict(SESSION_CONFIG),
        }
