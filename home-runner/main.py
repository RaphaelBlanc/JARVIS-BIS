"""
main.py — Serveur FastAPI du composant home-runner.

Expose une API HTTP privée qui reçoit des requêtes depuis le serveur Render
(render-gateway), les authentifie via un header secret, puis délègue l'exécution
à Antigravity CLI (agy) via agy_bridge.
"""

import base64
import hmac
import json
import logging
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agy_bridge import (
    KNOWN_MODELS,
    delete_all_conversations,
    delete_conversation,
    find_agy,
    get_conversation_history,
    get_session_info,
    get_token_usage,
    get_worker_info,
    list_recent_conversations,
    reset_session_async,
    resume_conversation,
    run_agy,
    shutdown_agy_worker,
    update_session,
)
from memory import (
    add_fact,
    clear_facts,
    delete_fact,
    delete_profile,
    get_memory,
    memory_version,
    render_memory_prompt,
    set_profile,
)


# ---------------------------------------------------------------------------
# Chargement de la configuration
# ---------------------------------------------------------------------------

load_dotenv()  # Charge .env si présent

SECRET_KEY: str = os.environ.get("SECRET_KEY", "")
PORT: int = int(os.environ.get("PORT", "8080"))
AGY_TIMEOUT: int = int(os.environ.get("AGY_TIMEOUT", "120"))
MAX_PROMPT_CHARS: int = int(os.environ.get("MAX_PROMPT_CHARS", "20000"))
MAX_IMAGE_MB: int = int(os.environ.get("MAX_IMAGE_MB", "10"))
MAX_IMAGE_BYTES: int = max(1, MAX_IMAGE_MB) * 1024 * 1024

if not SECRET_KEY:
    raise RuntimeError(
        "La variable d'environnement SECRET_KEY est obligatoire. "
        "Veuillez la définir dans .env ou dans l'environnement système."
    )

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("home-runner")

# Version mémoire injectée dans la conversation courante. Elle évite de
# répéter le bloc de contexte durable à chaque requête.
_LAST_INJECTED_MEMORY_VERSION: str | None = None


def _reset_memory_injection() -> None:
    """Oublie la version mémoire injectée (nouvelle session)."""
    global _LAST_INJECTED_MEMORY_VERSION
    _LAST_INJECTED_MEMORY_VERSION = None


def _should_inject_memory(version: str) -> bool:
    """Injecte la mémoire au premier tour ou si son contenu a changé."""
    return (
        not get_session_info().get("has_active_session", False)
        or version != _LAST_INJECTED_MEMORY_VERSION
    )


def _format_token_usage() -> str:
    """Résumé lisible du suivi local des tokens pour /usage."""
    stats = get_token_usage()
    if stats.get("tokens_available"):
        return (
            "📊 Suivi local des tokens (aujourd'hui) :\n"
            f"• Requêtes : {stats['requests']}\n"
            f"• Entrée : {stats['input_tokens']} tokens\n"
            f"• Sortie : {stats['output_tokens']} tokens"
        )
    return (
        "📊 Suivi local (aujourd'hui) :\n"
        f"• Requêtes : {stats['requests']}\n"
        f"• Prompts : {stats['prompt_chars']} caractères\n"
        f"• Réponses : {stats['output_chars']} caractères\n"
        "• Tokens : non exposés par agy"
    )


# ---------------------------------------------------------------------------
# Moteurs d'exécution IA (Antigravity agy, Codex, Claude Code, OpenCode)
# ---------------------------------------------------------------------------

from nc_engines import install as install_engines, executable as engine_executable

engines = install_engines(sys.modules[__name__])


# ---------------------------------------------------------------------------
# Application FastAPI
# ---------------------------------------------------------------------------


def _cleanup_old_uploads(max_age_seconds: int = 7 * 24 * 3600) -> None:
    """Supprime les images envoyées il y a plus de 7 jours."""
    upload_dir = Path("/tmp/assistant_uploads")
    if not upload_dir.exists():
        return

    now = time.time()
    removed = 0
    for item in upload_dir.iterdir():
        try:
            if item.is_file() and now - item.stat().st_mtime > max_age_seconds:
                item.unlink()
                removed += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("Impossible de nettoyer %s: %s", item, exc)

    if removed:
        logger.info("%d ancienne(s) image(s) supprimée(s) de %s.", removed, upload_dir)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Arrête proprement le worker agy persistant lors de l'arrêt du service."""
    _cleanup_old_uploads()
    yield
    await shutdown_agy_worker()


app = FastAPI(
    title="Home Runner",
    description="Passerelle locale qui exécute Antigravity CLI (agy) pour l'assistant personnel.",
    version="1.0.0",
    docs_url=None,   # Désactive Swagger UI (API privée)
    redoc_url=None,  # Désactive ReDoc
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Middleware de logging des requêtes
# ---------------------------------------------------------------------------


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Logue chaque requête entrante avec méthode, chemin et durée."""
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "%s %s → %d (%.1f ms)",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response

# ---------------------------------------------------------------------------
# Schémas Pydantic
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    """Corps de la requête POST /run."""

    prompt: str = Field(
        ...,
        min_length=1,
        max_length=MAX_PROMPT_CHARS,
        description="Le prompt à envoyer à agy.",
    )
    timeout: int = Field(
        default=AGY_TIMEOUT,
        ge=1,
        le=600,
        description="Timeout en secondes (1–600, défaut depuis AGY_TIMEOUT).",
    )
    model: str | None = Field(default=None, description="Modèle spécifique.")
    effort: str | None = Field(default=None, description="Effort de raisonnement.")
    mode: str | None = Field(default=None, description="Mode d'exécution.")
    continue_session: bool = Field(default=True, description="Poursuivre la conversation existante.")
    reset_session: bool = Field(default=False, description="Démarrer une nouvelle conversation vierge.")
    permission_mode: str | None = Field(default=None, description="Politique de permission.")
    image_base64: str | None = Field(
        default=None,
        max_length=MAX_IMAGE_BYTES * 2,
        description="Image encodée en base64 envoyée par l'utilisateur.",
    )
    image_filename: str | None = Field(
        default=None,
        max_length=255,
        description="Nom original du fichier image.",
    )


class RunResponse(BaseModel):
    """Réponse de POST /run."""

    success: bool
    output: str
    error: str | None = None
    session: dict | None = None


class SessionUpdateRequest(BaseModel):
    """Mise à jour des paramètres de session."""

    model: str | None = None
    effort: str | None = None
    mode: str | None = None
    permission_mode: str | None = None


class DeleteConversationRequest(BaseModel):
    """Suppression d'une conversation."""

    conversation_id: str = Field(..., description="ID de la conversation à supprimer ou 'all'.")


class MemoryFactRequest(BaseModel):
    """Ajout d'un souvenir durable."""

    text: str = Field(..., min_length=1, max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=20)


class MemoryProfileRequest(BaseModel):
    """Définition d'une préférence durable."""

    key: str = Field(..., min_length=1, max_length=64)
    value: str = Field(..., min_length=1, max_length=1000)

# ---------------------------------------------------------------------------
# Helpers d'authentification
# ---------------------------------------------------------------------------


def _check_secret(request: Request) -> None:
    """
    Vérifie le header X-Secret-Key.

    Raises:
        HTTPException 401 si le header est absent ou incorrect.
    """
    provided = request.headers.get("X-Secret-Key", "")
    if (
        not provided
        or not SECRET_KEY
        or not hmac.compare_digest(provided, SECRET_KEY)
    ):
        logger.warning(
            "Accès refusé depuis %s — clé incorrecte ou absente.",
            request.client.host if request.client else "inconnu",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Clé secrète manquante ou incorrecte.",
        )

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/ping", summary="Vérification de vie")
async def ping():
    """Retourne un simple accusé de réception. Pas d'authentification requise."""
    return {"status": "ok", "version": "1.0.0"}


@app.get("/health", summary="État de santé détaillé")
async def health():
    """
    Vérifie que le service est opérationnel et que le moteur actif est accessible.
    Pas d'authentification requise.
    """
    engine = engines.session().get("engine", "agy")
    available = bool(engine_executable(engine))

    agy_path = find_agy()
    agy_available = agy_path is not None

    payload = {
        "status": "ok" if available else "degraded",
        "version": "1.0.0",
        "engine": engine,
        "engine_available": available,
        "agy_available": agy_available,
        "agy_path": agy_path,
        "agy_timeout_default": AGY_TIMEOUT,
        "session": engines.session(),
        "runner_alive": True,
    }
    if engine == "agy":
        payload["agy_worker"] = get_worker_info()

    if not available:
        logger.warning("/health : moteur %s introuvable, statut dégradé.", engine)
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload)

    return payload


@app.get("/nc/engines", summary="Réglages et moteurs IA pour Noctalia")
async def nc_get_engines(request: Request):
    _check_secret(request)
    return engines.snapshot()


@app.post("/nc/engine", summary="Changer le moteur ou modèle IA")
async def nc_post_engine(request: Request):
    _check_secret(request)
    raw = await request.body()
    if len(raw) > 4096:
        raise HTTPException(status_code=413, detail="Payload too large")
    try:
        values = json.loads(raw)
        if not isinstance(values, dict) or any(not isinstance(v, str) or len(v) > 200 for v in values.values()):
            raise ValueError
        return {"status": "updated", "session": engines.choose(**values)}
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=400,
            detail="Réglages moteur invalides, CLI absent ou requête en cours."
        ) from None



@app.get("/usage/stats", summary="Suivi local des tokens")
async def usage_stats(request: Request):
    """Retourne le total quotidien des tokens et des tailles de prompts."""
    _check_secret(request)
    return {"success": True, "usage": get_token_usage()}


@app.get("/models", summary="Liste des modèles disponibles")
async def list_models(request: Request):
    """Retourne la liste des modèles Antigravity supportés et le modèle actif."""
    _check_secret(request)
    return {"models": KNOWN_MODELS, "current": get_session_info().get("model")}


@app.get("/session", summary="Configuration de session active")
async def session_info(request: Request):
    """Retourne l'état courant de la session."""
    _check_secret(request)
    return get_session_info()


@app.post("/session", summary="Mettre à jour la configuration de session")
async def session_update(request: Request, body: SessionUpdateRequest):
    """Change le modèle, l'effort ou le mode d'exécution."""
    _check_secret(request)
    updated = update_session(
        model=body.model,
        effort=body.effort,
        mode=body.mode,
        permission_mode=body.permission_mode,
    )
    return {"status": "updated", "session": updated}


@app.post("/session/reset", summary="Réinitialise la conversation")
async def session_reset(request: Request):
    """Oublie l'historique et démarre une session vierge."""
    _check_secret(request)
    res = await reset_session_async()
    _reset_memory_injection()
    logger.info("Session conversationnelle réinitialisée.")
    return {"status": "reset", "session": res}


@app.get("/conversations", summary="Liste les conversations récentes")
async def get_conversations(request: Request, limit: int = 8):
    """Retourne la liste des sessions passées récentes."""
    _check_secret(request)
    convs = list_recent_conversations(limit=limit)
    return {"conversations": convs}


class ResumeConvRequest(BaseModel):
    conversation_id: str


@app.post("/conversation/resume", summary="Reprendre une conversation spécifique")
async def resume_conv(request: Request, body: ResumeConvRequest):
    """Active une conversation passée par son identifiant et retourne son historique."""
    _check_secret(request)
    res = resume_conversation(body.conversation_id)
    history = res.get("history", [])
    logger.info("Reprise de la conversation %s (%d tours trouvés).", body.conversation_id, len(history))
    return {"status": "resumed", "session": res["session"], "history": history}


@app.get("/conversation/{conversation_id}/history", summary="Historique des échanges d'une conversation")
async def get_conv_history(request: Request, conversation_id: str, limit: int = 15):
    """Retourne l'historique des prompts et réponses d'une conversation."""
    _check_secret(request)
    history = get_conversation_history(conversation_id, max_turns=limit)
    return {"conversation_id": conversation_id, "history": history}


@app.delete("/conversation/{conversation_id}", summary="Supprime une conversation passée")
async def delete_conv(request: Request, conversation_id: str):
    """Supprime une conversation spécifique ou toutes les conversations ('all')."""
    _check_secret(request)
    if conversation_id.lower() == "all":
        res = delete_all_conversations()
        return res
    res = delete_conversation(conversation_id)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Erreur de suppression"))
    return res


@app.post("/conversation/delete", summary="Supprime une conversation passée (POST)")
async def delete_conv_post(request: Request, body: DeleteConversationRequest):
    """Supprime une conversation via POST."""
    _check_secret(request)
    if body.conversation_id.lower() == "all":
        res = delete_all_conversations()
        return res
    res = delete_conversation(body.conversation_id)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Erreur de suppression"))
    return res



@app.get("/memory", summary="Lire la mémoire durable et le profil")
async def memory_get(request: Request):
    """Retourne le profil et les souvenirs durables."""
    _check_secret(request)
    return {"success": True, "memory": get_memory()}


@app.post("/memory/facts", summary="Ajouter un souvenir durable")
async def memory_add_fact(request: Request, body: MemoryFactRequest):
    """Ajoute un souvenir injecté dans les prompts suivants."""
    _check_secret(request)
    try:
        fact = add_fact(body.text, tags=body.tags)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("Souvenir mémoire ajouté : #%s", fact["id"])
    return {"success": True, "fact": fact}


@app.delete("/memory/facts/{fact_id}", summary="Supprimer un souvenir")
async def memory_delete_fact(request: Request, fact_id: str):
    """Supprime un souvenir par son identifiant."""
    _check_secret(request)
    if not delete_fact(fact_id):
        raise HTTPException(status_code=404, detail="Souvenir introuvable.")
    return {"success": True, "deleted_id": fact_id}


@app.delete("/memory/facts", summary="Supprimer tous les souvenirs")
async def memory_clear_facts(request: Request):
    """Vide la liste des souvenirs, sans toucher au profil."""
    _check_secret(request)
    count = clear_facts()
    return {"success": True, "deleted_count": count}


@app.post("/memory/profile", summary="Définir une préférence durable")
async def memory_set_profile(request: Request, body: MemoryProfileRequest):
    """Ajoute ou remplace une préférence dans le profil."""
    _check_secret(request)
    try:
        set_profile(body.key, body.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "memory": get_memory()}


@app.delete("/memory/profile/{key}", summary="Supprimer une préférence durable")
async def memory_delete_profile(request: Request, key: str):
    """Supprime une préférence du profil."""
    _check_secret(request)
    if not delete_profile(key):
        raise HTTPException(status_code=404, detail="Préférence introuvable.")
    return {"success": True, "deleted_key": key}


@app.post("/run", response_model=RunResponse, summary="Exécute un prompt via agy")
async def run(request: Request, body: RunRequest):
    """
    Lance Antigravity CLI avec le prompt fourni et les options de session.
    """
    _check_secret(request)

    logger.info(
        "POST /run — prompt: %.80s… (timeout=%ds, continue=%s)",
        body.prompt,
        body.timeout,
        body.continue_session,
    )

    active_prompt = body.prompt
    if body.image_base64:
        try:
            image_bytes = base64.b64decode(body.image_base64, validate=True)
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail="Image base64 invalide."
            ) from exc
        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Image trop volumineuse ({len(image_bytes) / 1024 / 1024:.1f} Mo, "
                    f"maximum {MAX_IMAGE_MB} Mo)."
                ),
            )

        try:
            upload_dir = Path("/tmp/assistant_uploads")
            upload_dir.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(
                r"[^a-zA-Z0-9_\.-]", "_", body.image_filename or "image.png"
            )
            unique_filename = f"{uuid.uuid4().hex[:8]}_{safe_name}"
            saved_img_path = upload_dir / unique_filename
            with open(saved_img_path, "wb") as img_file:
                img_file.write(image_bytes)
            logger.info(
                "Image utilisateur enregistrée: %s (%d octets)",
                saved_img_path,
                len(image_bytes),
            )
            active_prompt = (
                f"[Image fournie par l'utilisateur: {saved_img_path}]\n\n"
                f"{active_prompt}"
            )
        except Exception as exc:
            logger.error("Erreur lors de l'enregistrement de l'image utilisateur: %s", exc)
            raise HTTPException(
                status_code=500, detail="Impossible d'enregistrer l'image."
            ) from exc

    # Injecte la mémoire durable dans les prompts normaux, uniquement pour une
    # nouvelle session ou lorsque son contenu a changé. Les commandes slash
    # d'agy ne doivent pas être préfixées par du contexte.
    memory_version_value: str | None = None
    if not active_prompt.lstrip().startswith("/"):
        memory_version_value = memory_version()
        if _should_inject_memory(memory_version_value):
            memory_context = render_memory_prompt()
            if memory_context:
                active_prompt = f"{memory_context}\n\n{active_prompt}"
                logger.info(
                    "Contexte mémoire injecté (%d caractères, version=%s).",
                    len(memory_context),
                    memory_version_value,
                )
        else:
            logger.debug(
                "Mémoire inchangée (version=%s) : pas de réinjection.",
                memory_version_value,
            )

    result = await run_agy(
        prompt=active_prompt,
        timeout=body.timeout,
        model=body.model,
        effort=body.effort,
        mode=body.mode,
        continue_session=body.continue_session,
        reset_session_flag=body.reset_session,
        permission_mode=body.permission_mode,
    )

    if not result["success"]:
        logger.warning("run_agy a échoué : %s", result.get("error"))
    elif memory_version_value is not None:
        # Mémorise la version effectivement injectée pour les tours suivants.
        global _LAST_INJECTED_MEMORY_VERSION
        _LAST_INJECTED_MEMORY_VERSION = memory_version_value

    output = result["output"]
    if result["success"] and body.prompt.strip() == "/usage":
        output = f"{output}\n\n{_format_token_usage()}".strip()

    return RunResponse(
        success=result["success"],
        output=output,
        error=result.get("error"),
        session=result.get("session"),
    )

# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )
