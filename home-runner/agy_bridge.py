"""
agy_bridge.py — Module de communication avec Antigravity CLI (agy).

Lance `agy` en subprocess non-interactif, envoie le prompt via stdin,
capture stdout/stderr et retourne un dict structuré.
"""

import asyncio
import contextlib
import datetime
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import time
import uuid
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


# Configuration de sessions en mémoire (isolées par clé de session)
def _default_session() -> dict:
    return {
        "model": "gemini-3.8-flash-low",
        "effort": "low",
        "mode": None,
        "has_active_session": False,
        "conversation_id": None,
        "session_token": secrets.token_hex(16),
        "permission_mode": "default",  # default | request-review | plan | ask | auto_edit | auto
        "turns": 0,
    }

_MAX_SESSIONS = 64
_ALLOWED_PERMISSION_MODES = frozenset({
    "default", "request-review", "plan", "ask", "auto_edit", "auto"
})
_SESSIONS: dict[str, dict] = {}


def _origin_runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        d = Path(base) / "jarvis-origin"
    else:
        d = Path(f"/tmp/jarvis-origin-{os.getuid()}")
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(d, 0o700)
    return d


def _origin_token_file(d: Path, session_key: str = "default") -> Path:
    sk = (session_key or "default").strip()
    h = hashlib.sha256(sk.encode("utf-8")).hexdigest()[:24]
    return d / f"active_token_{h}"


@contextlib.contextmanager
def active_origin_token_scope(token: Optional[str], session_key: str = "default"):
    """Expose temporairement le jeton d'origine 128 bits dans un fichier 0600 dédié à la session
    uniquement pendant l'exécution d'un tour actif, puis le supprime immédiatement.
    """
    clean = (token or "").strip()
    if not clean or len(clean) < 32:
        yield
        return
    d = _origin_runtime_dir()
    target = _origin_token_file(d, session_key)
    legacy = d / "active_token"
    tmp = d / f"active_token.{secrets.token_hex(6)}.tmp"
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(clean)
        os.replace(str(tmp), str(target))
        # Rétrocompatibilité avec les outils lisant encore le chemin historique
        with contextlib.suppress(OSError):
            if not legacy.exists():
                legacy.write_text(clean, encoding="utf-8")
                os.chmod(str(legacy), 0o600)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
            if legacy.exists() and legacy.read_text(encoding="utf-8").strip() == clean:
                legacy.unlink(missing_ok=True)


def _public_session(cfg: dict, include_secret: bool = False) -> dict:
    d = dict(cfg)
    if not include_secret:
        d.pop("session_token", None)
    return d


def _get_session(session_key: str = "default") -> dict:
    key = (session_key or "default").strip()[:120] or "default"
    if key not in _SESSIONS:
        if len(_SESSIONS) >= _MAX_SESSIONS:
            for old_key in list(_SESSIONS.keys()):
                if old_key != "default" and not (_SESSION_LOCKS.get(old_key) and _SESSION_LOCKS[old_key].locked()):
                    _SESSIONS.pop(old_key, None)
                    _SESSION_LOCKS.pop(old_key, None)
                    break
        _SESSIONS[key] = _default_session()
    elif not _SESSIONS[key].get("session_token") or len(str(_SESSIONS[key].get("session_token"))) < 32:
        _SESSIONS[key]["session_token"] = secrets.token_hex(16)
    return _SESSIONS[key]


def get_or_set_origin_token(session_key: str = "default", token: Optional[str] = None) -> str:
    """Retourne ou synchronise le jeton secret 128 bits d'une session (usage interne runner/gateway)."""
    cfg = _get_session(session_key)
    clean = (token or "").strip()
    if len(clean) >= 32 and len(clean) <= 128:
        cfg["session_token"] = clean
    return str(cfg["session_token"])


def rotate_origin_token(session_key: str = "default") -> str:
    """Fait tourner le jeton secret 128 bits d'une session (ou de toutes les sessions)."""
    if (session_key or "").strip() == "all":
        for s in _SESSIONS.values():
            s["session_token"] = secrets.token_hex(16)
        return str(_get_session("default")["session_token"])
    cfg = _get_session(session_key)
    cfg["session_token"] = secrets.token_hex(16)
    return str(cfg["session_token"])


SESSION_CONFIG = _get_session("default")

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


def get_session_info(session_key: str = "default", include_secret: bool = False) -> dict:
    """Retourne la configuration actuelle de la session spécifiée (sans session_token par défaut)."""
    return _public_session(_get_session(session_key), include_secret=include_secret)


def update_session(
    model: Optional[str] = None,
    effort: Optional[str] = None,
    mode: Optional[str] = None,
    permission_mode: Optional[str] = None,
    session_key: str = "default",
) -> dict:
    """Met à jour les paramètres de session."""
    cfg = _get_session(session_key)
    if model:
        cfg["model"] = model.strip()
        cfg["model"], cfg["effort"] = _normalize_model_effort(
            cfg["model"], cfg.get("effort")
        )

    if effort:
        cfg["effort"] = effort.strip().lower()
        cfg["model"], cfg["effort"] = _normalize_model_effort(
            cfg.get("model"), cfg.get("effort")
        )

    if mode:
        cfg["mode"] = mode.strip().lower()
    if permission_mode:
        clean_perm = permission_mode.strip().lower()
        if clean_perm not in _ALLOWED_PERMISSION_MODES:
            raise ValueError(f"permission_mode non autorisé : {permission_mode}")
        cfg["permission_mode"] = clean_perm
    return _public_session(cfg)


def reset_session(session_key: str = "default") -> dict:
    """Réinitialise la session conversationnelle (démarre une nouvelle conversation vierge)."""
    if (session_key or "").strip() == "all":
        for k in list(_SESSIONS.keys()):
            s = _SESSIONS[k]
            s["has_active_session"] = False
            s["conversation_id"] = None
            s["session_token"] = secrets.token_hex(16)
            s["turns"] = 0
        return _public_session(_get_session("default"))
    cfg = _get_session(session_key)
    cfg["has_active_session"] = False
    cfg["conversation_id"] = None
    cfg["session_token"] = secrets.token_hex(16)
    cfg["turns"] = 0
    return _public_session(cfg)


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


def _note_turn(session_key: str = "default") -> None:
    """Compte un tour conversationnel réussi."""
    cfg = _get_session(session_key)
    cfg["turns"] = int(cfg.get("turns", 0)) + 1


def _auto_compact_due(session_key: str = "default") -> bool:
    """Indique si le seuil de compaction automatique est atteint."""
    cfg = _get_session(session_key)
    return AUTO_COMPACT_TURNS > 0 and int(
        cfg.get("turns", 0)
    ) >= AUTO_COMPACT_TURNS


async def _maybe_auto_compact(session_key: str = "default") -> bool:
    """Réinitialise proprement la conversation si le seuil est atteint."""
    if not _auto_compact_due(session_key):
        return False
    cfg = _get_session(session_key)
    logger.info(
        "Compaction automatique [%s] : %d tours atteints (seuil=%d), "
        "réinitialisation de l'historique.",
        session_key,
        cfg.get("turns", 0),
        AUTO_COMPACT_TURNS,
    )
    await _stop_worker(session_key)
    reset_session(session_key)
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


def resume_conversation(conv_id: str, session_key: str = "default") -> dict:
    """Active une conversation passée par son identifiant pour les futurs échanges."""
    clean_id = conv_id.strip()
    cfg = _get_session(session_key)
    cfg["conversation_id"] = clean_id
    cfg["has_active_session"] = True
    cfg["session_token"] = secrets.token_hex(16)
    cfg["turns"] = 0
    history = get_conversation_history(clean_id)
    return {
        "session": _public_session(cfg),
        "history": history,
    }


def list_recent_conversations(limit: int = 8) -> list[dict]:
    """Scanne le répertoire brain d'Antigravity et retourne les conversations récentes avec leur résumé."""
    brain_dir = Path.home() / ".gemini" / "antigravity-cli" / "brain"
    if not brain_dir.exists() or limit <= 0:
        return []

    # Ignore la session de développement en cours
    excluded_ids = {"0e02609b-e140-4ca5-a760-247d880dfa5c"}

    # 1. Tri rapide par mtime (stat uniquement) avant d'ouvrir les fichiers JSONL
    candidates: list[tuple[float, Path, Path]] = []
    for item in brain_dir.iterdir():
        if not item.is_dir() or item.name in excluded_ids:
            continue
        transcript_file = item / ".system_generated" / "logs" / "transcript.jsonl"
        try:
            st = transcript_file.stat()
            if st.st_size > 0:
                candidates.append((st.st_mtime, item, transcript_file))
        except OSError:
            continue

    candidates.sort(key=lambda c: c[0], reverse=True)

    # 2. Lecture uniquement des fichiers nécessaires jusqu'à atteindre `limit`
    conversations = []
    for mtime, item, transcript_file in candidates:
        if len(conversations) >= limit:
            break
        try:
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

    return conversations


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
        for s_key, s_cfg in list(_SESSIONS.items()):
            s_cfg["session_token"] = secrets.token_hex(16)
            if s_cfg.get("conversation_id") == clean_id:
                reset_session(s_key)
        logger.info("Conversation %s supprimée avec succès.", clean_id)
        return {"success": True, "deleted_id": clean_id}
    except Exception as e:
        logger.error("Erreur suppression conversation %s : %s", clean_id, e)
        return {"success": False, "error": str(e)}


def delete_all_conversations() -> dict:
    """Supprime toutes les conversations passées (sauf la session courante)."""
    brain_dir = Path.home() / ".gemini" / "antigravity-cli" / "brain"
    if not brain_dir.exists():
        reset_session("all")
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

    reset_session("all")
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
        session_key: str = "default",
        session_token: str = "",
    ) -> None:
        self.agy_path = agy_path
        self.model = model
        self.effort = effort
        self.mode = mode
        self.permission_mode = permission_mode
        self.continue_session = continue_session
        self.conversation_id = conversation_id
        self.has_active_session = has_active_session
        self.session_key = session_key
        self.session_token = session_token
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
        session_token: Optional[str] = None,
    ) -> bool:
        """Indique si le worker peut servir la configuration demandée."""
        return (
            self.model == model
            and self.effort == effort
            and self.mode == mode
            and self.permission_mode == permission_mode
            and (session_token is None or self.session_token == session_token)
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
        if self.permission_mode and self.permission_mode != "default":
            cmd.extend(["--permission-mode", self.permission_mode])

        env = {
            **os.environ,
            "TERM": "dumb",
            "NO_COLOR": "1",
            "FORCE_COLOR": "0",
            "COLORTERM": "",
        }
        env["JARVIS_SESSION_KEY"] = self.session_key
        env.pop("JARVIS_SESSION_TOKEN", None)

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


_WORKERS: dict[str, _AgyStreamWorker] = {}
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}
_GLOBAL_SEMAPHORE = asyncio.Semaphore(1)
_WATCHDOG_TASK: Optional[asyncio.Task] = None


def _get_session_lock(session_key: str = "default") -> asyncio.Lock:
    key = (session_key or "default").strip()[:120] or "default"
    if key not in _SESSION_LOCKS:
        if len(_SESSION_LOCKS) >= _MAX_SESSIONS:
            for old_key, old_lock in list(_SESSION_LOCKS.items()):
                if old_key != "default" and not old_lock.locked():
                    _SESSION_LOCKS.pop(old_key, None)
                    break
        _SESSION_LOCKS[key] = asyncio.Lock()
    return _SESSION_LOCKS[key]


def get_worker_info(session_key: str = "default") -> dict:
    """Retourne l'état du worker persistant, pour /health et le diagnostic."""
    key = (session_key or "default").strip()
    worker = _WORKERS.get(key)
    cfg = _get_session(key)
    active_count = len([w for w in _WORKERS.values() if w.is_alive()])
    return {
        "persistent": AGY_PERSISTENT,
        "sandbox": AGY_SANDBOX,
        "session_key": key,
        "running": bool(worker and worker.is_alive()),
        "conversation_id": worker.conversation_id if worker else None,
        "idle_seconds": (
            round(time.monotonic() - worker.last_used, 1) if worker else None
        ),
        "idle_timeout": AGY_WORKER_IDLE_TIMEOUT,
        "turns": cfg.get("turns", 0),
        "auto_compact_turns": AUTO_COMPACT_TURNS,
        "active_workers_count": active_count,
    }


async def _stop_worker(session_key: str = "default") -> None:
    """Arrête et oublie le worker pour la session donnée."""
    key = (session_key or "default").strip()
    worker = _WORKERS.pop(key, None)
    if worker is not None:
        await worker.stop()


async def _worker_watchdog() -> None:
    """Arrête les workers après une longue période d'inactivité."""
    while True:
        await asyncio.sleep(30)
        now = time.monotonic()
        for key, worker in list(_WORKERS.items()):
            if (
                worker is not None
                and not worker.busy
                and (now - worker.last_used) > AGY_WORKER_IDLE_TIMEOUT
            ):
                lock = _get_session_lock(key)
                if not lock.locked():
                    try:
                        async with lock:
                            logger.info(
                                "Arrêt du worker agy [%s] inactif depuis %.0fs.",
                                key,
                                now - worker.last_used,
                            )
                            await _stop_worker(key)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Erreur arrêt worker watchdog [%s]: %s", key, exc)


def _ensure_watchdog() -> None:
    global _WATCHDOG_TASK
    if _WATCHDOG_TASK is None or _WATCHDOG_TASK.done():
        _WATCHDOG_TASK = asyncio.create_task(_worker_watchdog())


async def shutdown_agy_worker() -> None:
    """Arrête proprement tous les workers (appelé à l'arrêt du home-runner)."""
    global _WATCHDOG_TASK
    task = _WATCHDOG_TASK
    _WATCHDOG_TASK = None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    for key in list(_WORKERS.keys()):
        await _stop_worker(key)


async def reset_session_async(session_key: str = "default") -> dict:
    """Réinitialise la session et arrête le worker qui la détenait."""
    if (session_key or "").strip() == "all":
        for key in list(_WORKERS.keys()):
            await _stop_worker(key)
        return reset_session("all")
    lock = _get_session_lock(session_key)
    async with lock:
        await _stop_worker(session_key)
        return reset_session(session_key)


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
    session_key: str = "default",
) -> dict:
    """
    Lance `agy` en subprocess non-interactif (-p / --print) avec support de continuité conversationnelle.
    """
    cfg = _get_session(session_key)
    agy_path = find_agy()
    if not agy_path:
        return {
            "success": False,
            "output": "",
            "error": "agy introuvable dans PATH et dans les emplacements connus.",
            "session": _public_session(cfg),
        }

    if reset_session_flag:
        cfg["has_active_session"] = False
        cfg["conversation_id"] = None
        cfg["session_token"] = secrets.token_hex(16)

    # Construction des arguments agy
    cmd = [agy_path, *_sandbox_args()]

    # Reprise de conversation ciblée (--conversation) ou continue (-c) UNIQUEMENT si continue_session est True
    if continue_session and not reset_session_flag:
        target_conv = cfg.get("conversation_id")
        if target_conv:
            cmd.extend(["--conversation", target_conv])
        elif cfg.get("has_active_session"):
            cmd.append("-c")

    # Modèle et effort de raisonnement (normalisés pour les modèles Flash)
    active_model = model or cfg.get("model")
    active_effort = effort or cfg.get("effort") or "low"
    active_model, active_effort = _normalize_model_effort(
        active_model, active_effort
    )
    if active_model:
        cmd.extend(["--model", active_model])
    if active_effort:
        cmd.extend(["--effort", active_effort])

    # Mode d'exécution
    active_mode = mode or cfg.get("mode")
    if active_mode:
        cmd.extend(["--mode", active_mode])

    # Permissions
    perm = permission_mode or cfg.get("permission_mode", "default")
    if perm and perm != "default":
        cmd.extend(["--permission-mode", perm])

    # Prompt et format
    cmd.extend(["-p", prompt, "--output-format", "text"])

    # Environnement épuré (aucun jeton secret de session dans l'environnement du processus)
    env = {
        **os.environ,
        "TERM": "dumb",
        "NO_COLOR": "1",
        "FORCE_COLOR": "0",
        "COLORTERM": "",
    }
    env["JARVIS_SESSION_KEY"] = session_key
    env.pop("JARVIS_SESSION_TOKEN", None)

    logger.info("Exécution agy [%s]: %s (timeout=%ds)", session_key, " ".join(cmd[:6]), timeout)

    proc: Optional[asyncio.subprocess.Process] = None
    try:
        with active_origin_token_scope(str(cfg.get("session_token") or ""), session_key):
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
                cfg["has_active_session"] = True
                if prompt.lstrip().startswith("/"):
                    if prompt.strip().startswith("/compact"):
                        cfg["turns"] = 0
                else:
                    _note_turn(session_key)
                    await _maybe_auto_compact(session_key)
            logger.info("agy [%s] terminé avec succès (code 0). Sortie : %d caractères.", session_key, len(out))
        else:
            logger.warning(
                "agy [%s] terminé avec code %d. stderr : %.200s",
                session_key,
                proc.returncode,
                err or "(vide)",
            )

        record_token_usage(prompt, out)

        return {
            "success": success,
            "output": out,
            "error": err if err else None,
            "session": _public_session(cfg),
        }

    except asyncio.TimeoutError:
        logger.error("agy [%s] a dépassé le timeout de %ds.", session_key, timeout)
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
            "session": _public_session(cfg),
        }

    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur inattendue lors de l'exécution de agy [%s]: %s", session_key, exc)
        return {
            "success": False,
            "output": "",
            "error": str(exc),
            "session": _public_session(cfg),
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
    session_key: str = "default",
) -> dict:
    """Exécute un tour via le worker stream-json persistant associé à la session."""
    cfg = _get_session(session_key)
    agy_path = find_agy()
    if not agy_path:
        return {
            "success": False,
            "output": "",
            "error": "agy introuvable dans PATH et dans les emplacements connus.",
            "session": _public_session(cfg),
        }

    if reset_session_flag:
        await _stop_worker(session_key)
        cfg["has_active_session"] = False
        cfg["conversation_id"] = None
        cfg["session_token"] = secrets.token_hex(16)

    active_model = model or cfg.get("model")
    active_effort = effort or cfg.get("effort") or "low"
    active_model, active_effort = _normalize_model_effort(
        active_model, active_effort
    )
    active_mode = mode or cfg.get("mode")
    active_perm = permission_mode or cfg.get(
        "permission_mode", "always-proceed"
    )

    is_slash = prompt.lstrip().startswith("/")
    if not continue_session or is_slash:
        # Ne tue le worker persistant que si la commande slash modifie la session active.
        # Une requête one-shot stateless (continue_session=False) préserve le worker chaud.
        if continue_session:
            await _stop_worker(session_key)
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
            session_key=session_key,
        )

    worker = _WORKERS.get(session_key)
    if worker is not None and not worker.is_alive():
        await _stop_worker(session_key)
        worker = None

    if worker is not None and not cfg.get("has_active_session"):
        await _stop_worker(session_key)
        worker = None

    if worker is not None and not worker.matches(
        active_model,
        active_effort,
        active_mode,
        active_perm,
        session_token=cfg.get("session_token"),
    ):
        await _stop_worker(session_key)
        worker = None

    if worker is not None:
        current_id = cfg.get("conversation_id")
        if (
            current_id
            and worker.conversation_id
            and worker.conversation_id != current_id
        ):
            await _stop_worker(session_key)
            worker = None

    if worker is None:
        worker = _AgyStreamWorker(
            agy_path=agy_path,
            model=active_model,
            effort=active_effort,
            mode=active_mode,
            permission_mode=active_perm,
            continue_session=True,
            conversation_id=cfg.get("conversation_id"),
            has_active_session=cfg.get("has_active_session", False),
            session_key=session_key,
            session_token=str(cfg.get("session_token") or ""),
        )
        try:
            await worker.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Échec du worker agy persistant [%s] (%s) ; repli sur une exécution one-shot.",
                session_key,
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
                session_key=session_key,
            )
        _WORKERS[session_key] = worker
        _ensure_watchdog()

    try:
        with active_origin_token_scope(str(cfg.get("session_token") or ""), session_key):
            result = await worker.send(prompt, timeout)
    except asyncio.TimeoutError:
        logger.error("Worker agy [%s] : timeout de %ds dépassé.", session_key, timeout)
        await _stop_worker(session_key)
        return {
            "success": False,
            "output": "",
            "error": f"Timeout dépassé ({timeout}s). Le processus a été interrompu.",
            "session": _public_session(cfg),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur du worker agy [%s] : %s", session_key, exc)
        await _stop_worker(session_key)
        return {
            "success": False,
            "output": "",
            "error": str(exc),
            "session": _public_session(cfg),
        }

    status = str(result.get("status", "")).upper()
    success = status == "SUCCESS"
    output = result.get("response", "") or ""
    error = result.get("error")

    if success:
        cfg["has_active_session"] = True
        if worker.conversation_id:
            cfg["conversation_id"] = worker.conversation_id
        _note_turn(session_key)
        await _maybe_auto_compact(session_key)
        logger.info(
            "agy (worker) [%s] terminé avec succès. Sortie : %d caractères.", session_key, len(output)
        )
    else:
        logger.warning(
            "agy (worker) [%s] terminé avec statut %s : %s",
            session_key,
            status or "?",
            error or "(sans détail)",
        )

    record_token_usage(prompt, output, result)

    return {
        "success": success,
        "output": output,
        "error": None if success else error,
        "session": _public_session(cfg),
    }


async def run_agy(
    prompt: str,
    timeout: int = 600,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    mode: Optional[str] = None,
    continue_session: bool = True,
    reset_session_flag: bool = False,
    permission_mode: Optional[str] = None,
    session_key: str = "default",
) -> dict:
    """Exécute agy pour la session demandée.

    Chaque session_key possède son propre verrou et sa propre conversation.
    Un sémaphore global (2 requêtes max) empêche de surcharger le processeur
    tout en permettant à plusieurs machines de converser simultanément.
    """
    key = (session_key or "default").strip()
    if not continue_session and not reset_session_flag:
        async with _GLOBAL_SEMAPHORE:
            return await _run_agy_oneshot(
                prompt=prompt,
                timeout=timeout,
                model=model,
                effort=effort,
                mode=mode,
                continue_session=False,
                reset_session_flag=False,
                permission_mode=permission_mode,
                update_session=False,
                session_key=key,
            )

    session_lock = _get_session_lock(key)

    if session_lock.locked():
        logger.warning("Requête agy refusée pour la session '%s' : une exécution est déjà en cours.", key)
        return {
            "success": False,
            "output": "",
            "error": "Une autre requête est déjà en cours sur cette session. Réessayez dans quelques instants.",
            "session": _public_session(_get_session(key)),
        }

    await session_lock.acquire()
    try:
        async with _GLOBAL_SEMAPHORE:
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
                    session_key=key,
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
                session_key=key,
            )
    finally:
        session_lock.release()
