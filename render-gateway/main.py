"""
main.py — Serveur FastAPI principal du render-gateway.

Lance le gateway qui:
  - Expose les routes de santé (/ping, /health)
  - Monte le router Telegram (webhook)
  - Démarre le bot Discord en arrière-plan
  - Configure le webhook Telegram au démarrage
"""

import logging
import os
import sys
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

# Charge les variables d'environnement depuis .env (si présent)
load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ── Imports locaux (après dotenv) ─────────────────────────────────────────────

from bots.telegram_bot import (  # noqa: E402
    get_application as get_telegram_app,
    setup_webhook,
    telegram_router,
    TELEGRAM_WEBHOOK_URL,
)
from bots.discord_bot import start_discord_bot, stop_discord_bot  # noqa: E402

# ── Variables d'environnement ─────────────────────────────────────────────────

TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")
SECRET_KEY: str = os.getenv("SECRET_KEY", "")
HOME_SERVER_URL: str = os.getenv("HOME_SERVER_URL", "")
VERSION: str = "1.0.0"

# ── Validation au démarrage ───────────────────────────────────────────────────


def _check_env() -> None:
    """Vérifie que les variables d'environnement critiques sont définies.

    Loggue des avertissements pour les variables manquantes sans faire planter
    le serveur, afin de permettre un déploiement partiel (ex: sans Discord).
    """
    warnings = []
    if not TELEGRAM_TOKEN:
        warnings.append("TELEGRAM_TOKEN")
    if not SECRET_KEY:
        warnings.append("SECRET_KEY")
    if not HOME_SERVER_URL:
        warnings.append("HOME_SERVER_URL")

    if warnings:
        logger.warning(
            "Variables d'environnement manquantes: %s. "
            "Certaines fonctionnalités peuvent ne pas fonctionner.",
            ", ".join(warnings),
        )
    else:
        logger.info("Toutes les variables d'environnement critiques sont configurées.")


# ── Lifespan FastAPI ──────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gère le cycle de vie de l'application FastAPI.

    Démarrage:
        - Vérifie les variables d'environnement
        - Lance le bot Discord en arrière-plan
        - Configure le webhook Telegram

    Arrêt:
        - Ferme proprement le bot Discord
    """
    logger.info("=== Démarrage du render-gateway v%s ===", VERSION)
    _check_env()

    # Démarre le bot Discord dans un thread daemon
    start_discord_bot()

    # Configure le webhook Telegram si les variables sont présentes
    if TELEGRAM_TOKEN and TELEGRAM_WEBHOOK_URL:
        try:
            tg_app = get_telegram_app()
            webhook_url = f"{TELEGRAM_WEBHOOK_URL.rstrip('/')}/telegram/webhook"
            await setup_webhook(tg_app, webhook_url)
        except Exception as exc:
            logger.error("Impossible de configurer le webhook Telegram: %s", exc, exc_info=True)
    else:
        logger.warning(
            "Webhook Telegram non configuré (TELEGRAM_TOKEN ou TELEGRAM_WEBHOOK_URL manquant)."
        )

    logger.info("render-gateway prêt à recevoir des requêtes.")

    yield  # L'application tourne

    # Arrêt
    logger.info("=== Arrêt du render-gateway ===")
    try:
        await stop_discord_bot()
    except Exception as exc:
        logger.error("Erreur lors de l'arrêt du bot Discord: %s", exc)


# ── Application FastAPI ───────────────────────────────────────────────────────

app = FastAPI(
    title="Assistant Personnel — Render Gateway",
    description=(
        "Gateway HTTP qui reçoit les messages Telegram/Discord "
        "et les transmet au home-runner via Cloudflare Tunnel."
    ),
    version=VERSION,
    lifespan=lifespan,
)

# Monte le router Telegram
app.include_router(telegram_router)


# ── Routes de santé ───────────────────────────────────────────────────────────


@app.get("/ping", tags=["health"], summary="Ping simple pour les uptime bots")
async def ping() -> dict:
    """Retourne un statut OK. Utilisé par les monitors d'uptime (ex: UptimeRobot)."""
    return {"status": "ok"}


@app.get("/health", tags=["health"], summary="Vérification de santé détaillée")
async def health() -> dict:
    """Retourne des informations détaillées sur l'état du service.

    Returns:
        Un dictionnaire avec:
            - ``status``: "ok" ou "degraded"
            - ``version``: version du service
            - ``home_server_configured``: True si HOME_SERVER_URL est défini
            - ``telegram_configured``: True si TELEGRAM_TOKEN est défini
            - ``discord_configured``: True si DISCORD_TOKEN est défini
    """
    discord_token = os.getenv("DISCORD_TOKEN", "")
    all_ok = bool(HOME_SERVER_URL and TELEGRAM_TOKEN and SECRET_KEY)

    return {
        "status": "ok" if all_ok else "degraded",
        "version": VERSION,
        "home_server_configured": bool(HOME_SERVER_URL),
        "telegram_configured": bool(TELEGRAM_TOKEN),
        "discord_configured": bool(discord_token),
        "secret_key_configured": bool(SECRET_KEY),
    }
