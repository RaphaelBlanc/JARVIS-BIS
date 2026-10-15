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

from agy_bridge import find_agy, run_agy

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


class RunResponse(BaseModel):
    """Réponse de POST /run."""

    success: bool
    output: str
    error: str | None = None

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
    }

    if not agy_available:
        logger.warning("/health : agy introuvable, statut dégradé.")
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload)

    return payload


@app.post("/run", response_model=RunResponse, summary="Exécute un prompt via agy")
async def run(request: Request, body: RunRequest):
    """
    Lance Antigravity CLI avec le prompt fourni.

    Authentification : header **X-Secret-Key** obligatoire.

    - **prompt** : texte de la requête à envoyer à agy.
    - **timeout** : délai maximal en secondes avant interruption forcée.
    """
    _check_secret(request)

    logger.info(
        "POST /run — prompt: %.80s… (timeout=%ds)",
        body.prompt,
        body.timeout,
    )

    result = await run_agy(prompt=body.prompt, timeout=body.timeout)

    if not result["success"]:
        logger.warning("run_agy a échoué : %s", result.get("error"))

    return RunResponse(
        success=result["success"],
        output=result["output"],
        error=result.get("error"),
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
