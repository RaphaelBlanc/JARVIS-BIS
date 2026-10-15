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


async def run_agy(prompt: str, timeout: int = 120) -> dict:
    """
    Lance `agy` en subprocess asynchrone et lui envoie le prompt via stdin.

    Args:
        prompt:  Texte de la requête à envoyer à agy.
        timeout: Délai maximal d'attente en secondes (défaut : 120).

    Returns:
        Dict avec les clés :
            - success (bool)  : True si le processus s'est terminé avec code 0.
            - output  (str)   : Contenu de stdout nettoyé des séquences ANSI.
            - error   (str|None) : Contenu de stderr, ou message d'erreur interne.
    """
    agy_path = find_agy()
    if not agy_path:
        return {
            "success": False,
            "output": "",
            "error": "agy introuvable dans PATH et dans les emplacements connus.",
        }

    # Environnement épuré : désactive les couleurs et les effets interactifs
    env = {
        **os.environ,
        "TERM": "dumb",
        "NO_COLOR": "1",
        "FORCE_COLOR": "0",
        "COLORTERM": "",
    }

    logger.info("Lancement de agy (timeout=%ds) — prompt: %.80s…", timeout, prompt)

    proc: Optional[asyncio.subprocess.Process] = None
    try:
        proc = await asyncio.create_subprocess_exec(
            agy_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Envoie le prompt puis ferme stdin (EOF) pour signifier la fin de l'entrée
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=(prompt + "\n").encode("utf-8")),
            timeout=timeout,
        )

        out = strip_ansi(stdout_bytes.decode(errors="replace")).strip()
        err = strip_ansi(stderr_bytes.decode(errors="replace")).strip()
        success = proc.returncode == 0

        if success:
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
        }

    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur inattendue lors de l'exécution de agy : %s", exc)
        return {
            "success": False,
            "output": "",
            "error": str(exc),
        }
