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
import time
from collections import deque
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
    "model": "gemini-3.8-flash-low",
    "effort": "low",
    "mode": None,
    "has_active_session": False,
    "conversation_id": None,
    "permission_mode": "always-proceed",  # always-proceed | request-review
    "turns": 0,
}

# Compaction automatique : au-delà de N tours, le worker est arrêté et la
# conversation repart d'un historique vierge. 0 désactive la fonctionnalité.
AUTO_COMPACT_TURNS = int(os.getenv("AUTO_COMPACT_TURNS", "20"))

KNOWN_MODELS = [
    {"id": "gemini-3.8-flash-low", "name": "Gemini 3.8 Flash (Low)", "desc": "Ultra-rapide et direct (recommandé pour chat)"},
    {"id": "gemini-3.8-flash-medium", "name": "Gemini 3.8 Flash (Medium)", "desc": "Équilibré"},
    {"id": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash (High)", "desc": "Réflexion approfondie"},
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6 (Thinking)", "desc": "Raisonnement avancé et code"},
    {"id": "claude-opus-4-6-thinking", "name": "Claude Opus 4.6 (Thinking)", "desc": "Capacité maximale"},
    {"id": "gpt-oss-120b-medium", "name": "GPT-OSS 120B (Medium)", "desc": "Open Source"},
]

_FLASH_MODEL_PREFIX = "gemini-3.8-flash-"
_FLASH_EFFORTS = {"low", "medium", "high"}


def _normalize_model_effort(
    model: Optional[str], effort: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Aligne le modèle et l'effort pour éviter les conflits CLI.

    - Les modèles ``gemini-3.8-flash-*`` encodent déjà l'effort dans leur nom :
      ``agy`` refuse par exemple ``--model gemini-3.8-flash-low --effort medium``.
    - Les modèles Opus comme ``claude-opus-4-6-thinking`` intègrent déjà la réflexion
      et refusent l'option ``--effort``.
    """
    if model and (model == "claude-opus-4-6-thinking" or ("opus" in model and "thinking" in model)):
        return model, None

    if model and model.startswith(_FLASH_MODEL_PREFIX):
        suffix = model[len(_FLASH_MODEL_PREFIX):]
        if suffix in _FLASH_EFFORTS:
            if not effort:
                effort = suffix
            elif effort != suffix:
                if effort in _FLASH_EFFORTS:
                    model = f"{_FLASH_MODEL_PREFIX}{effort}"
                elif effort in {"xhigh", "max"}:
                    # Ces niveaux n'existent pas dans la gamme Flash : on
                    # utilise le maximum disponible plutôt que de laisser
                    # agy rejeter la combinaison.
                    model = f"{_FLASH_MODEL_PREFIX}high"
                    effort = "high"
                else:
                    effort = suffix
    return model, effort


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
        SESSION_CONFIG["model"], SESSION_CONFIG["effort"] = _normalize_model_effort(
            SESSION_CONFIG["model"], SESSION_CONFIG.get("effort")
        )

    if effort:
        SESSION_CONFIG["effort"] = effort.strip().lower()
        SESSION_CONFIG["model"], SESSION_CONFIG["effort"] = _normalize_model_effort(
            SESSION_CONFIG.get("model"), SESSION_CONFIG.get("effort")
        )

    if mode:
        SESSION_CONFIG["mode"] = mode.strip().lower()
    if permission_mode:
        SESSION_CONFIG["permission_mode"] = permission_mode.strip().lower()
    return dict(SESSION_CONFIG)


def reset_session() -> dict:
    """Réinitialise la session conversationnelle (démarre une nouvelle conversation vierge)."""
    SESSION_CONFIG["has_active_session"] = False
    SESSION_CONFIG["conversation_id"] = None
    SESSION_CONFIG["turns"] = 0
    return dict(SESSION_CONFIG)


# ── Suivi local des tokens ────────────────────────────────────────────────────

_TOKEN_USAGE: dict = {
    "date": "",
    "requests": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "prompt_chars": 0,
    "output_chars": 0,
    "tokens_available": False,
}


def _first_int(source: dict, keys: tuple[str, ...]) -> Optional[int]:
    """Retourne la première valeur entière exploitable parmi ``keys``."""
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _extract_token_counts(result: Optional[dict]) -> Optional[tuple[int, int]]:
    """Extrait (entrée, sortie) depuis les différentes formes d'événement agy."""
    if not isinstance(result, dict):
        return None

    candidates = []
    for key in ("usage", "usageMetadata", "token_usage", "tokens", "stats"):
        value = result.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    candidates.append(result)

    for source in candidates:
        prompt_tokens = _first_int(
            source,
            (
                "input_tokens",
                "prompt_tokens",
                "promptTokenCount",
                "inputTokenCount",
                "inputTokens",
            ),
        )
        output_tokens = _first_int(
            source,
            (
                "output_tokens",
                "completion_tokens",
                "candidatesTokenCount",
                "outputTokenCount",
                "outputTokens",
            ),
        )
        if prompt_tokens is not None or output_tokens is not None:
            return prompt_tokens or 0, output_tokens or 0
    return None


def record_token_usage(
    prompt: str,
    output: str,
    result: Optional[dict] = None,
) -> Optional[tuple[int, int]]:
    """Enregistre les tokens (ou à défaut la taille) d'une requête agy."""
    global _TOKEN_USAGE

    today = datetime.date.today().isoformat()
    if _TOKEN_USAGE["date"] != today:
        _TOKEN_USAGE = {
            "date": today,
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "prompt_chars": 0,
            "output_chars": 0,
            "tokens_available": False,
        }

    _TOKEN_USAGE["requests"] += 1
    _TOKEN_USAGE["prompt_chars"] += len(prompt or "")
    _TOKEN_USAGE["output_chars"] += len(output or "")

    counts = _extract_token_counts(result)
    if counts is not None:
        input_tokens, output_tokens = counts
        _TOKEN_USAGE["input_tokens"] += input_tokens
        _TOKEN_USAGE["output_tokens"] += output_tokens
        _TOKEN_USAGE["tokens_available"] = True
        logger.info(
            "Tokens requête — entrée=%d, sortie=%d.",
            input_tokens,
            output_tokens,
        )
        return counts

    # `agy` n'expose pas toujours l'usage : on trace au moins les tailles.
    logger.info(
        "Usage tokens indisponible — prompt=%d caractères, réponse=%d caractères.",
        len(prompt or ""),
        len(output or ""),
    )
    return None


def get_token_usage() -> dict:
    """Retourne le total quotidien des tokens et des tailles de texte."""
    return dict(_TOKEN_USAGE)


def _note_turn() -> None:
    """Compte un tour conversationnel réussi."""
    SESSION_CONFIG["turns"] = int(SESSION_CONFIG.get("turns", 0)) + 1


def _auto_compact_due() -> bool:
    """Indique si le seuil de compaction automatique est atteint."""
    return AUTO_COMPACT_TURNS > 0 and int(
        SESSION_CONFIG.get("turns", 0)
    ) >= AUTO_COMPACT_TURNS


async def _maybe_auto_compact() -> bool:
    """Réinitialise proprement la conversation si le seuil est atteint."""
    if not _auto_compact_due():
        return False
    logger.info(
        "Compaction automatique : %d tours atteints (seuil=%d), "
        "réinitialisation de l'historique.",
        SESSION_CONFIG.get("turns", 0),
        AUTO_COMPACT_TURNS,
    )
    await _stop_worker()
    reset_session()
    return True


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
    SESSION_CONFIG["turns"] = 0
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



# Verrou global : une seule exécution d'agy à la fois. Les conversations
# partagent le même transcript et le même SESSION_CONFIG ; deux processus
# simultanés corrompraient l'historique et les paramètres de session.
_AGY_LOCK = asyncio.Lock()

# ── Worker persistant (mode stream-json) ─────────────────────────────────────
#
# `agy --input-format stream-json` accepte plusieurs tours dans un même
# processus. On garde donc un worker vivant entre les messages : le démarrage,
# l'authentification, la résolution de modèle et l'initialisation MCP ne sont
# payés qu'une seule fois au lieu d'être refaits à chaque requête.

AGY_PERSISTENT: bool = os.getenv("AGY_PERSISTENT", "1").strip().lower() not in {
    "0", "false", "no", "off"
}
AGY_WORKER_IDLE_TIMEOUT: float = float(os.getenv("AGY_WORKER_IDLE_TIMEOUT", "900"))
AGY_STARTUP_TIMEOUT: float = float(os.getenv("AGY_STARTUP_TIMEOUT", "30"))
# Sandbox terminal d'agy activé par défaut. Peut être désactivé avec AGY_SANDBOX=0.
AGY_SANDBOX: bool = os.getenv("AGY_SANDBOX", "1").strip().lower() not in {
    "0", "false", "no", "off"
}


def _sandbox_args() -> list[str]:
    """Retourne les arguments de sandbox à ajouter à la commande agy."""
    return ["--sandbox"] if AGY_SANDBOX else []


class _AgyStreamWorker:
    """Un processus `agy` stream-json réutilisé pour plusieurs tours."""

    def __init__(
        self,
        agy_path: str,
        model: Optional[str],
        effort: Optional[str],
        mode: Optional[str],
        permission_mode: Optional[str],
        continue_session: bool,
        conversation_id: Optional[str],
        has_active_session: bool,
    ) -> None:
        self.agy_path = agy_path
        self.model = model
        self.effort = effort
        self.mode = mode
        self.permission_mode = permission_mode
        self.continue_session = continue_session
        self.conversation_id = conversation_id
        self.has_active_session = has_active_session
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self.last_used = time.monotonic()
        self.busy = False

    def matches(
        self,
        model: Optional[str],
        effort: Optional[str],
        mode: Optional[str],
        permission_mode: Optional[str],
    ) -> bool:
        """Indique si le worker peut servir la configuration demandée."""
        return (
            self.model == model
            and self.effort == effort
            and self.mode == mode
            and self.permission_mode == permission_mode
        )

    def is_alive(self) -> bool:
        return (
            self.proc is not None
            and self.proc.returncode is None
            and self.proc.stdin is not None
        )

    def _exit_message(self, prefix: str) -> str:
        code = self.proc.returncode if self.proc is not None else None
        details = ""
        if self._stderr_tail:
            details = " Dernière sortie: " + " | ".join(list(self._stderr_tail)[-3:])
        return f"{prefix} (code={code}).{details}"

    async def _drain_stderr(self) -> None:
        """Vide stderr en continu pour éviter de bloquer le processus."""
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = strip_ansi(line.decode(errors="replace")).strip()
                if text:
                    self._stderr_tail.append(text)
                    logger.debug("agy stderr: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("Erreur de lecture de stderr agy: %s", exc)

    async def _read_event(self, timeout: float) -> dict:
        """Lit un événement NDJSON avec un délai maximal."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            try:
                line = await asyncio.wait_for(
                    self.proc.stdout.readline(), timeout=remaining  # type: ignore[union-attr]
                )
            except ValueError as exc:
                raise RuntimeError(
                    self._exit_message("flux stdout agy fermé")
                ) from exc
            if not line:
                raise RuntimeError(self._exit_message("agy s'est arrêté"))
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                logger.debug("Ligne agy non JSON ignorée: %.200s", text)

    async def start(self) -> dict:
        """Démarre le processus et attend l'événement `init`."""
        cmd = [
            self.agy_path,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            *_sandbox_args(),
        ]
        if self.continue_session:
            if self.conversation_id:
                cmd.extend(["--conversation", self.conversation_id])
            elif self.has_active_session:
                cmd.append("-c")
        if self.model:
            cmd.extend(["--model", self.model])
        if self.effort:
            cmd.extend(["--effort", self.effort])
        if self.mode:
            cmd.extend(["--mode", self.mode])
        if self.permission_mode == "always-proceed":
            cmd.append("--dangerously-skip-permissions")

        env = {
            **os.environ,
            "TERM": "dumb",
            "NO_COLOR": "1",
            "FORCE_COLOR": "0",
            "COLORTERM": "",
        }

        logger.info(
            "Démarrage worker agy persistant: %s (conversation=%s)",
            " ".join(cmd[:6]),
            self.conversation_id or ("continue" if self.has_active_session else "nouvelle"),
        )
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        deadline = asyncio.get_running_loop().time() + AGY_STARTUP_TIMEOUT
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError(
                    f"agy n'a pas envoyé d'événement init en {AGY_STARTUP_TIMEOUT}s"
                )
            event = await self._read_event(remaining)
            if event.get("event") == "init":
                self.conversation_id = (
                    event.get("conversation_id") or self.conversation_id
                )
                self.last_used = time.monotonic()
                logger.info(
                    "Worker agy prêt (conversation=%s).", self.conversation_id
                )
                return event

    async def send(self, prompt: str, timeout: int) -> dict:
        """Envoie un tour et attend l'événement `result` correspondant."""
        if not self.is_alive():
            raise RuntimeError(self._exit_message("worker agy non disponible"))

        payload = {"event": "user", "message": {"content": prompt}}
        self.busy = True
        self.last_used = time.monotonic()
        try:
            assert self.proc is not None and self.proc.stdin is not None
            self.proc.stdin.write(
                json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
            )
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self.busy = False
            raise RuntimeError(
                self._exit_message(f"entrée agy fermée: {exc}")
            ) from exc
        except Exception:
            self.busy = False
            raise

        try:
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                event = await self._read_event(remaining)
                if event.get("event") == "result":
                    self.last_used = time.monotonic()
                    return event.get("result", {})
        finally:
            self.busy = False

    async def stop(self) -> None:
        """Arrête proprement le worker, puis le tue si nécessaire."""
        proc = self.proc
        self.proc = None
        if proc is not None:
            try:
                if proc.stdin is not None and not proc.stdin.is_closing():
                    proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass

        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
            self._stderr_task = None
        logger.info("Worker agy arrêté.")


_WORKER: Optional[_AgyStreamWorker] = None
_WATCHDOG_TASK: Optional[asyncio.Task] = None


def get_worker_info() -> dict:
    """Retourne l'état du worker persistant, pour /health et le diagnostic."""
    worker = _WORKER
    return {
        "persistent": AGY_PERSISTENT,
        "sandbox": AGY_SANDBOX,
        "running": bool(worker and worker.is_alive()),
        "conversation_id": worker.conversation_id if worker else None,
        "idle_seconds": (
            round(time.monotonic() - worker.last_used, 1) if worker else None
        ),
        "idle_timeout": AGY_WORKER_IDLE_TIMEOUT,
        "turns": SESSION_CONFIG.get("turns", 0),
        "auto_compact_turns": AUTO_COMPACT_TURNS,
    }


async def _stop_worker() -> None:
    """Arrête et oublie le worker courant."""
    global _WORKER
    worker = _WORKER
    _WORKER = None
    if worker is not None:
        await worker.stop()


async def _worker_watchdog() -> None:
    """Arrête le worker après une longue période d'inactivité."""
    while True:
        await asyncio.sleep(30)
        if _AGY_LOCK.locked():
            continue
        try:
            await _AGY_LOCK.acquire()
        except Exception:  # noqa: BLE001
            continue
        try:
            worker = _WORKER
            if (
                worker is not None
                and not worker.busy
                and time.monotonic() - worker.last_used > AGY_WORKER_IDLE_TIMEOUT
            ):
                logger.info(
                    "Arrêt du worker agy inactif depuis %.0fs.",
                    time.monotonic() - worker.last_used,
                )
                await _stop_worker()
        finally:
            _AGY_LOCK.release()


def _ensure_watchdog() -> None:
    global _WATCHDOG_TASK
    if _WATCHDOG_TASK is None or _WATCHDOG_TASK.done():
        _WATCHDOG_TASK = asyncio.create_task(_worker_watchdog())


async def shutdown_agy_worker() -> None:
    """Arrête proprement le worker (appelé à l'arrêt du home-runner)."""
    global _WATCHDOG_TASK
    task = _WATCHDOG_TASK
    _WATCHDOG_TASK = None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    async with _AGY_LOCK:
        await _stop_worker()


async def reset_session_async() -> dict:
    """Réinitialise la session et arrête le worker qui la détenait."""
    async with _AGY_LOCK:
        await _stop_worker()
        return reset_session()


async def _run_agy_oneshot(
    prompt: str,
    timeout: int = 120,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    mode: Optional[str] = None,
    continue_session: bool = True,
    reset_session_flag: bool = False,
    permission_mode: Optional[str] = None,
    update_session: bool = True,
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
    cmd = [agy_path, *_sandbox_args()]

    # Reprise de conversation ciblée (--conversation) ou continue (-c) UNIQUEMENT si continue_session est True
    if continue_session and not reset_session_flag:
        target_conv = SESSION_CONFIG.get("conversation_id")
        if target_conv:
            cmd.extend(["--conversation", target_conv])
        elif SESSION_CONFIG.get("has_active_session"):
            cmd.append("-c")


    # Modèle et effort de raisonnement (normalisés pour les modèles Flash)
    active_model = model or SESSION_CONFIG.get("model")
    active_effort = effort or SESSION_CONFIG.get("effort") or "low"
    active_model, active_effort = _normalize_model_effort(
        active_model, active_effort
    )
    if active_model:
        cmd.extend(["--model", active_model])
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
            if update_session:
                SESSION_CONFIG["has_active_session"] = True
                if prompt.lstrip().startswith("/"):
                    # `/compact` remet le compteur à zéro ; les autres slash
                    # commands ne comptent pas comme des tours de conversation.
                    if prompt.strip().startswith("/compact"):
                        SESSION_CONFIG["turns"] = 0
                else:
                    _note_turn()
                    await _maybe_auto_compact()
            logger.info("agy terminé avec succès (code 0). Sortie : %d caractères.", len(out))
        else:
            logger.warning(
                "agy terminé avec code %d. stderr : %.200s",
                proc.returncode,
                err or "(vide)",
            )

        record_token_usage(prompt, out)

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


async def _run_agy_persistent(
    prompt: str,
    timeout: int,
    model: Optional[str],
    effort: Optional[str],
    mode: Optional[str],
    continue_session: bool,
    reset_session_flag: bool,
    permission_mode: Optional[str],
) -> dict:
    """Exécute un tour via le worker stream-json persistant.

    Les commandes sans continuité (`continue_session=False`) et les commandes
    slash d'agy utilisent l'ancien mode one-shot : elles ne doivent pas
    polluer la conversation principale, et certaines slash commands ne sont
    pas disponibles en stream-json.
    """
    global _WORKER

    agy_path = find_agy()
    if not agy_path:
        return {
            "success": False,
            "output": "",
            "error": "agy introuvable dans PATH et dans les emplacements connus.",
            "session": dict(SESSION_CONFIG),
        }

    if reset_session_flag:
        await _stop_worker()
        SESSION_CONFIG["has_active_session"] = False
        SESSION_CONFIG["conversation_id"] = None

    active_model = model or SESSION_CONFIG.get("model")
    active_effort = effort or SESSION_CONFIG.get("effort") or "low"
    active_model, active_effort = _normalize_model_effort(
        active_model, active_effort
    )
    active_mode = mode or SESSION_CONFIG.get("mode")
    active_perm = permission_mode or SESSION_CONFIG.get(
        "permission_mode", "always-proceed"
    )

    is_slash = prompt.lstrip().startswith("/")
    if not continue_session or is_slash:
        # Un seul processus agy à la fois : on arrête le worker principal
        # avant de lancer une commande one-shot.
        await _stop_worker()
        return await _run_agy_oneshot(
            prompt=prompt,
            timeout=timeout,
            model=model,
            effort=effort,
            mode=mode,
            continue_session=continue_session,
            reset_session_flag=False,
            permission_mode=permission_mode,
            update_session=continue_session,
        )

    worker = _WORKER
    if worker is not None and not worker.is_alive():
        await _stop_worker()
        worker = None

    # `/session/reset` remet has_active_session à False sans forcément arrêter
    # le worker : on ne doit alors surtout pas réutiliser l'ancienne conversation.
    if worker is not None and not SESSION_CONFIG.get("has_active_session"):
        await _stop_worker()
        worker = None

    if worker is not None and not worker.matches(
        active_model, active_effort, active_mode, active_perm
    ):
        await _stop_worker()
        worker = None

    if worker is not None:
        current_id = SESSION_CONFIG.get("conversation_id")
        if (
            current_id
            and worker.conversation_id
            and worker.conversation_id != current_id
        ):
            await _stop_worker()
            worker = None

    if worker is None:
        worker = _AgyStreamWorker(
            agy_path=agy_path,
            model=active_model,
            effort=active_effort,
            mode=active_mode,
            permission_mode=active_perm,
            continue_session=True,
            conversation_id=SESSION_CONFIG.get("conversation_id"),
            has_active_session=SESSION_CONFIG.get("has_active_session", False),
        )
        try:
            await worker.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Échec du worker agy persistant (%s) ; repli sur une exécution one-shot.",
                exc,
            )
            await worker.stop()
            return await _run_agy_oneshot(
                prompt=prompt,
                timeout=timeout,
                model=model,
                effort=effort,
                mode=mode,
                continue_session=continue_session,
                reset_session_flag=False,
                permission_mode=permission_mode,
                update_session=True,
            )
        _WORKER = worker
        _ensure_watchdog()

    try:
        result = await worker.send(prompt, timeout)
    except asyncio.TimeoutError:
        logger.error("Worker agy : timeout de %ds dépassé.", timeout)
        await _stop_worker()
        return {
            "success": False,
            "output": "",
            "error": f"Timeout dépassé ({timeout}s). Le processus a été interrompu.",
            "session": dict(SESSION_CONFIG),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur du worker agy : %s", exc)
        await _stop_worker()
        return {
            "success": False,
            "output": "",
            "error": str(exc),
            "session": dict(SESSION_CONFIG),
        }

    status = str(result.get("status", "")).upper()
    success = status == "SUCCESS"
    output = result.get("response", "") or ""
    error = result.get("error")

    if success:
        SESSION_CONFIG["has_active_session"] = True
        if worker.conversation_id:
            SESSION_CONFIG["conversation_id"] = worker.conversation_id
        _note_turn()
        await _maybe_auto_compact()
        logger.info(
            "agy (worker) terminé avec succès. Sortie : %d caractères.", len(output)
        )
    else:
        logger.warning(
            "agy (worker) terminé avec statut %s : %s",
            status or "?",
            error or "(sans détail)",
        )

    record_token_usage(prompt, output, result)

    return {
        "success": success,
        "output": output,
        "error": None if success else error,
        "session": dict(SESSION_CONFIG),
    }


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
    """Exécute agy en garantissant qu'une seule requête tourne à la fois.

    Si une requête est déjà en cours, on répond immédiatement par une erreur
    explicite plutôt que de lancer un second processus sur la même session.
    """
    if _AGY_LOCK.locked():
        logger.warning("Requête agy refusée : une exécution est déjà en cours.")
        return {
            "success": False,
            "output": "",
            "error": "Une autre requête est déjà en cours. Réessayez dans quelques instants.",
            "session": dict(SESSION_CONFIG),
        }

    await _AGY_LOCK.acquire()
    try:
        if AGY_PERSISTENT:
            return await _run_agy_persistent(
                prompt=prompt,
                timeout=timeout,
                model=model,
                effort=effort,
                mode=mode,
                continue_session=continue_session,
                reset_session_flag=reset_session_flag,
                permission_mode=permission_mode,
            )
        return await _run_agy_oneshot(
            prompt=prompt,
            timeout=timeout,
            model=model,
            effort=effort,
            mode=mode,
            continue_session=continue_session,
            reset_session_flag=reset_session_flag,
            permission_mode=permission_mode,
            update_session=continue_session,
        )
    finally:
        _AGY_LOCK.release()
