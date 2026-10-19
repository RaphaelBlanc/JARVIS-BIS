"""
main.py — Serveur FastAPI principal du render-gateway.

Lance le gateway qui:
  - Expose les routes de santé (/ping, /health)
  - Monte le router Telegram (webhook)
  - Démarre le bot Discord en arrière-plan
  - Configure le webhook Telegram au démarrage
"""

import asyncio
import datetime
import hmac
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

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

# httpx loggue les URLs complètes en INFO, ce qui expose le token du bot
# Telegram dans journalctl. On ne garde que les avertissements/erreurs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ── Imports locaux (après dotenv) ─────────────────────────────────────────────

from bots.telegram_bot import (  # noqa: E402
    get_application as get_telegram_app,
    setup_webhook,
    start_telegram_polling,
    stop_telegram_polling,
    telegram_router,
    TELEGRAM_WEBHOOK_URL,
)
from bots.discord_bot import start_discord_bot, stop_discord_bot  # noqa: E402
from reminders import (  # noqa: E402
    TIMEZONE,
    add_reminder,
    cancel_reminder,
    get_due_reminders,
    init_db,
    list_active_reminders,
    mark_reminder_sent,
    parse_delay_or_time,
)

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


def require_secret(
    x_secret_key: Optional[str] = Header(default=None, alias="X-Secret-Key"),
) -> None:
    """Protège l'API des rappels par la clé partagée du gateway."""
    if not SECRET_KEY or not x_secret_key or not hmac.compare_digest(
        x_secret_key, SECRET_KEY
    ):
        raise HTTPException(status_code=401, detail="Clé API invalide")


# ── Planificateur de briefing matinal ─────────────────────────────────────────

async def morning_briefing_scheduler() -> None:
    """Tâche de fond qui envoie automatiquement le briefing chaque matin à 08:00."""
    logger.info("Planificateur de briefing matinal actif (08:00 chaque matin).")
    last_sent_date = None
    while True:
        try:
            now = datetime.datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            # Déclenchement à 08h00 si pas encore envoyé aujourd'hui
            if now.hour == 8 and now.minute < 5 and last_sent_date != today_str:
                logger.info("Déclenchement automatique du briefing matinal pour le %s...", today_str)
                from bots.telegram_bot import BRIEFING_PROMPT, send_broadcast_message
                from bots.discord_bot import broadcast_discord_message
                from gateway import forward_to_runner

                res = await forward_to_runner(BRIEFING_PROMPT, continue_session=False, timeout=240)
                if res.get("success"):
                    briefing_text = res.get("output", "").strip()
                    if briefing_text:
                        # Diffusion Telegram
                        await send_broadcast_message(briefing_text)
                        # Diffusion Discord
                        await broadcast_discord_message(briefing_text)
                        last_sent_date = today_str
                        logger.info("Briefing matinal automatique diffusé avec succès !")
        except Exception as exc:
            logger.error("Erreur dans le planificateur de briefing matinal: %s", exc)

        await asyncio.sleep(60)


# ── Planificateur de rappels ──────────────────────────────────────────────────

async def distribute_reminder(reminder: dict) -> None:
    """Envoie un rappel sur sa plateforme d'origine (Telegram ou Discord)."""
    platform = (reminder.get("platform") or "").lower()
    chat_id = str(reminder.get("chat_id") or "")
    message = str(reminder.get("message") or "").strip()
    text = f"⏰ Rappel : {message}"

    logger.info(
        "Distribution du rappel #%s (%s -> %s) : %s",
        reminder.get("id"),
        platform,
        chat_id,
        message,
    )

    if platform == "telegram":
        from bots.telegram_bot import send_telegram_message

        await send_telegram_message(chat_id, text)
        return

    if platform == "discord":
        from bots.discord_bot import send_discord_message_to_channel

        await send_discord_message_to_channel(int(chat_id), text)
        return

    raise ValueError(f"Plateforme de rappel inconnue : {platform!r}")


async def reminder_scheduler() -> None:
    """Tâche de fond qui distribue les rappels arrivés à échéance."""
    init_db()
    logger.info("Planificateur de rappels actif (vérification toutes les 5s).")
    while True:
        try:
            for reminder in get_due_reminders():
                try:
                    await distribute_reminder(reminder)
                    mark_reminder_sent(reminder["id"])
                except Exception as exc:
                    logger.error(
                        "Erreur distribution rappel #%s: %s",
                        reminder.get("id"),
                        exc,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Erreur dans le planificateur de rappels: %s", exc)

        await asyncio.sleep(5)


# ── Lifespan FastAPI ──────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gère le cycle de vie de l'application FastAPI.

    Démarrage:
        - Vérifie les variables d'environnement
        - Lance le bot Discord en arrière-plan
        - Configure le webhook Telegram (si URL fournie) ou démarre le polling (en local)
        - Lance le planificateur de briefing matinal

    Arrêt:
        - Ferme proprement les bots Discord et Telegram
    """
    logger.info("=== Démarrage du render-gateway v%s ===", VERSION)
    _check_env()

    # Démarre le bot Discord dans un thread daemon
    start_discord_bot()

    # Configure Telegram (Webhook si en prod sur Render, ou Polling si en local)
    if TELEGRAM_TOKEN and TELEGRAM_WEBHOOK_URL:
        try:
            tg_app = get_telegram_app()
            webhook_url = f"{TELEGRAM_WEBHOOK_URL.rstrip('/')}/telegram/webhook"
            await setup_webhook(tg_app, webhook_url)
        except Exception as exc:
            logger.error("Impossible de configurer le webhook Telegram: %s", exc, exc_info=True)
    elif TELEGRAM_TOKEN:
        logger.info("TELEGRAM_WEBHOOK_URL non défini — Démarrage du bot Telegram en mode Polling local...")
        start_telegram_polling()
    else:
        logger.warning("TELEGRAM_TOKEN manquant — bot Telegram inactif.")

    # Démarre les tâches de fond (briefing + rappels)
    scheduler_task = asyncio.create_task(morning_briefing_scheduler())
    reminder_task = asyncio.create_task(reminder_scheduler())

    logger.info("render-gateway prêt à recevoir des requêtes.")

    yield  # L'application tourne

    # Arrêt
    logger.info("=== Arrêt du render-gateway ===")
    for task in (scheduler_task, reminder_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Erreur lors de l'arrêt d'une tâche de fond: %s", exc)
    try:
        stop_telegram_polling()
    except Exception as exc:
        logger.error("Erreur lors de l'arrêt du polling Telegram: %s", exc)
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


# ── API de gestion des rappels ────────────────────────────────────────────────


class ReminderCreateRequest(BaseModel):
    """Corps de création d'un rappel (utilisé par l'API et les intégrations)."""

    platform: str = Field(default="telegram", description="telegram ou discord")
    chat_id: str = Field(..., description="ID du chat Telegram ou du salon Discord")
    target_id: Optional[str] = Field(default=None, description="ID de l'utilisateur")
    target_name: str = Field(default="", description="Nom de l'utilisateur")
    message: str = Field(..., min_length=1, description="Texte du rappel")
    remind_at: Optional[str] = Field(
        default=None,
        description="Date/heure ISO 8601. Ex: 2026-10-19T18:30:00+02:00",
    )
    delay: Optional[str] = Field(
        default=None,
        description="Durée relative. Ex: 15m, 1h30, 2h",
    )


def _parse_iso_datetime(value: str) -> datetime.datetime:
    """Parse une date ISO en ajoutant le fuseau Europe/Paris si nécessaire."""
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TIMEZONE)
    return parsed


@app.post(
    "/api/reminders",
    tags=["reminders"],
    summary="Créer un rappel",
    dependencies=[Depends(require_secret)],
)
async def create_reminder(payload: ReminderCreateRequest) -> dict:
    """Crée un rappel persistant pour Telegram ou Discord."""
    platform = payload.platform.strip().lower()
    if platform not in {"telegram", "discord"}:
        raise HTTPException(status_code=400, detail="platform doit être telegram ou discord")

    if payload.delay:
        # Un champ "delay" est par définition une durée relative : "1h30"
        # doit signifier 1h30 plus tard, pas 01h30 du matin.
        remind_at, error = parse_delay_or_time(payload.delay, force_relative=True)
        if remind_at is None:
            raise HTTPException(status_code=400, detail=error or "Durée invalide")
    elif payload.remind_at:
        try:
            remind_at = _parse_iso_datetime(payload.remind_at)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"remind_at invalide : {exc}"
            ) from exc
    else:
        raise HTTPException(
            status_code=400, detail="Fournissez delay ou remind_at"
        )

    reminder = add_reminder(
        platform=platform,
        chat_id=payload.chat_id,
        target_id=payload.target_id,
        message=payload.message,
        remind_at=remind_at,
        target_name=payload.target_name,
    )
    return {"success": True, "reminder": reminder}


@app.get(
    "/api/reminders",
    tags=["reminders"],
    summary="Lister les rappels actifs",
    dependencies=[Depends(require_secret)],
)
async def get_reminders(
    chat_id: Optional[str] = None,
    platform: Optional[str] = None,
) -> dict:
    """Liste les rappels en attente, éventuellement filtrés."""
    reminders = list_active_reminders(chat_id)
    if platform:
        wanted = platform.strip().lower()
        reminders = [r for r in reminders if r["platform"] == wanted]
    return {"reminders": reminders, "count": len(reminders)}


@app.delete(
    "/api/reminders/{reminder_id}",
    tags=["reminders"],
    summary="Annuler un rappel",
    dependencies=[Depends(require_secret)],
)
async def delete_reminder(
    reminder_id: int,
    chat_id: Optional[str] = None,
) -> dict:
    """Annule un rappel en attente."""
    if not cancel_reminder(reminder_id, chat_id):
        raise HTTPException(status_code=404, detail="Rappel introuvable ou déjà traité")
    return {"success": True, "cancelled_id": reminder_id}


@app.post(
    "/api/reminders/{reminder_id}/cancel",
    tags=["reminders"],
    summary="Annuler un rappel (alias)",
    dependencies=[Depends(require_secret)],
)
async def cancel_reminder_endpoint(
    reminder_id: int,
    chat_id: Optional[str] = None,
) -> dict:
    """Alias POST pour annuler un rappel."""
    return await delete_reminder(reminder_id, chat_id)


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
