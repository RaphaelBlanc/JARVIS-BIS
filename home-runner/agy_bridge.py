"""
agy_bridge.py — Module de communication avec Antigravity CLI (agy).

Lance `agy` en subprocess non-interactif, envoie le prompt via stdin,
capture stdout/stderr et retourne un dict structuré.
"""

import asyncio
import datetime
import json
import logging
import os
import re
import shutil
from pathlib import Path
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
    "conversation_id": None,
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
    SESSION_CONFIG["conversation_id"] = None
    return dict(SESSION_CONFIG)


def get_conversation_history(conv_id: str, max_turns: int = 15) -> list[dict]:
    """Extrait les échanges passés (prompt utilisateur et réponse finale de l'assistant) du transcript."""
    brain_dir = Path.home() / ".gemini" / "antigravity-cli" / "brain"
    transcript_file = brain_dir / conv_id.strip() / ".system_generated" / "logs" / "transcript.jsonl"
    if not transcript_file.exists():
        return []

    turns = []
    current_prompt = None
    last_response = None

    with open(transcript_file, "r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
                typ = obj.get("type")
                content = obj.get("content", "")

                if typ == "USER_INPUT":
                    if current_prompt and last_response:
                        turns.append({"prompt": current_prompt, "response": last_response})
                    current_prompt = None
                    last_response = None

                    if not content or "<CONTEXT_SUMMARY>" in content:
                        continue
                    m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", content, re.DOTALL)
                    current_prompt = (m.group(1).strip() if m else content.strip())

                elif typ == "PLANNER_RESPONSE" and content:
                    last_response = content.strip()
            except Exception:
                pass

    if current_prompt and last_response:
        turns.append({"prompt": current_prompt, "response": last_response})

    return turns[-max_turns:]


def resume_conversation(conv_id: str) -> dict:
    """Active une conversation passée par son identifiant pour les futurs échanges."""
    clean_id = conv_id.strip()
    SESSION_CONFIG["conversation_id"] = clean_id
    SESSION_CONFIG["has_active_session"] = True
    history = get_conversation_history(clean_id)
    return {
        "session": dict(SESSION_CONFIG),
        "history": history,
    }


def list_recent_conversations(limit: int = 8) -> list[dict]:
    """Scanne le répertoire brain d'Antigravity et retourne les conversations récentes avec leur résumé."""
    brain_dir = Path.home() / ".gemini" / "antigravity-cli" / "brain"
    if not brain_dir.exists():
        return []

    # Ignore la session de développement en cours
    excluded_ids = {"0e02609b-e140-4ca5-a760-247d880dfa5c"}

    conversations = []
    for item in brain_dir.iterdir():
        if not item.is_dir() or item.name in excluded_ids:
            continue
        transcript_file = item / ".system_generated" / "logs" / "transcript.jsonl"
        if not transcript_file.exists():
            continue

        try:
            mtime = transcript_file.stat().st_mtime
            date_str = datetime.datetime.fromtimestamp(mtime).strftime("%d/%m %H:%M")
            title = "Discussion sans titre"
            first_user_text = ""
            with open(transcript_file, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                        if obj.get("type") == "USER_INPUT":
                            raw = obj.get("content", "")
                            if "<CONTEXT_SUMMARY>" in raw:
                                continue
                            m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", raw, re.DOTALL)
                            req = m.group(1).strip() if m else raw.strip()
                            title = req.replace("\n", " ")[:50].strip()
                            first_user_text = title
                            break
                    except Exception:
                        pass

            if not first_user_text or "CONTEXT_SUMMARY" in first_user_text:
                continue

            conversations.append({
                "id": item.name,
                "title": title,
                "date": date_str,
                "timestamp": mtime,
            })
        except Exception:
            continue

    conversations.sort(key=lambda c: c["timestamp"], reverse=True)
    return conversations[:limit]


def delete_conversation(conv_id: str) -> dict:
    """Supprime une conversation passée du répertoire brain."""
    clean_id = conv_id.strip()
    if not clean_id or "/" in clean_id or "\\" in clean_id or ".." in clean_id:
        return {"success": False, "error": "Identifiant de conversation invalide."}

    excluded_ids = {"0e02609b-e140-4ca5-a760-247d880dfa5c"}
    if clean_id in excluded_ids:
        return {"success": False, "error": "Impossible de supprimer la session de travail active."}

    brain_dir = Path.home() / ".gemini" / "antigravity-cli" / "brain"
    target_dir = brain_dir / clean_id
    if not target_dir.exists() or not target_dir.is_dir():
        return {"success": False, "error": "Conversation introuvable."}

    try:
        shutil.rmtree(target_dir)
        if SESSION_CONFIG.get("conversation_id") == clean_id:
            reset_session()
        logger.info("Conversation %s supprimée avec succès.", clean_id)
        return {"success": True, "deleted_id": clean_id}
    except Exception as e:
        logger.error("Erreur suppression conversation %s : %s", clean_id, e)
        return {"success": False, "error": str(e)}


def delete_all_conversations() -> dict:
    """Supprime toutes les conversations passées (sauf la session courante)."""
    brain_dir = Path.home() / ".gemini" / "antigravity-cli" / "brain"
    if not brain_dir.exists():
        return {"success": True, "deleted_count": 0}

    excluded_ids = {"0e02609b-e140-4ca5-a760-247d880dfa5c"}
    count = 0
    for item in brain_dir.iterdir():
        if item.is_dir() and item.name not in excluded_ids:
            try:
                shutil.rmtree(item)
                count += 1
            except Exception as e:
                logger.warning("Impossible de supprimer %s : %s", item.name, e)

    reset_session()
    logger.info("%d conversations passées ont été supprimées.", count)
    return {"success": True, "deleted_count": count}



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
        SESSION_CONFIG["conversation_id"] = None

    # Construction des arguments agy
    cmd = [agy_path]

    # Reprise de conversation ciblée (--conversation) ou continue (-c) UNIQUEMENT si continue_session est True
    if continue_session and not reset_session_flag:
        target_conv = SESSION_CONFIG.get("conversation_id")
        if target_conv:
            cmd.extend(["--conversation", target_conv])
        elif SESSION_CONFIG.get("has_active_session"):
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
