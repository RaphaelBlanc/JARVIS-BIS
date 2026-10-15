"""
main.py — Serveur FastAPI du composant home-runner.

Expose une API HTTP privée qui reçoit des requêtes depuis le serveur Render
(render-gateway), les authentifie via un header secret, puis délègue l'exécution
à Antigravity CLI (agy) via agy_bridge.
"""

import logging
import os
import time

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agy_bridge import (
    KNOWN_MODELS,
    find_agy,
    get_conversation_history,
    get_session_info,
    list_recent_conversations,
    reset_session as bridge_reset_session,
    resume_conversation,
    run_agy,
    update_session,
)


# ---------------------------------------------------------------------------
# Chargement de la configuration
# ---------------------------------------------------------------------------

load_dotenv()  # Charge .env si présent

SECRET_KEY: str = os.environ.get("SECRET_KEY", "")
PORT: int = int(os.environ.get("PORT", "8080"))
AGY_TIMEOUT: int = int(os.environ.get("AGY_TIMEOUT", "120"))

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

# ---------------------------------------------------------------------------
# Application FastAPI
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Home Runner",
    description="Passerelle locale qui exécute Antigravity CLI (agy) pour l'assistant personnel.",
    version="1.0.0",
    docs_url=None,   # Désactive Swagger UI (API privée)
    redoc_url=None,  # Désactive ReDoc
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

    prompt: str = Field(..., min_length=1, description="Le prompt à envoyer à agy.")
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
    if not provided or provided != SECRET_KEY:
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
    Vérifie que le service est opérationnel et qu'agy est accessible.
    Pas d'authentification requise.
    """
    agy_path = find_agy()
    agy_available = agy_path is not None

    payload = {
        "status": "ok" if agy_available else "degraded",
        "version": "1.0.0",
        "agy_available": agy_available,
        "agy_path": agy_path,
        "agy_timeout_default": AGY_TIMEOUT,
        "session": get_session_info(),
    }

    if not agy_available:
        logger.warning("/health : agy introuvable, statut dégradé.")
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload)

    return payload


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
    res = bridge_reset_session()
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

    result = await run_agy(
        prompt=body.prompt,
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

    return RunResponse(
        success=result["success"],
        output=result["output"],
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
