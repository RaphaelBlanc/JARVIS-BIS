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
    mark_reminder_failure,
    mark_reminder_sent,
    next_occurrence_after,
    parse_delay_or_time,
    reschedule_reminder,
)
from stt import warmup_stt  # noqa: E402
from briefing import (  # noqa: E402
    BRIEFING_TIMEZONE,
    get_briefing_text,
    mark_platform_sent,
    pending_platforms,
    schedule_description,
    should_send_now,
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
    """Envoie le briefing matinal selon la configuration, plateforme par plateforme."""
    logger.info(
        "Planificateur de briefing matinal actif : %s.", schedule_description()
    )
    last_failed_attempt: Optional[datetime.datetime] = None

    while True:
        try:
            now = datetime.datetime.now(BRIEFING_TIMEZONE)
            today_key = now.strftime("%Y-%m-%d")
            if should_send_now(now):
                pending = pending_platforms(today_key)
                if pending and (
                    last_failed_attempt is None
                    or (now - last_failed_attempt).total_seconds() >= 60
                ):
                    logger.info(
                        "Déclenchement du briefing matinal %s (en attente : %s)...",
                        today_key,
                        ", ".join(pending),
                    )
                    from bots.telegram_bot import send_broadcast_message
                    from bots.discord_bot import broadcast_discord_message

                    # Le scheduler force un rafraîchissement : le cache sert
                    # aux commandes /briefing manuelles rapprochées.
                    res = await get_briefing_text(force_refresh=True, timeout=240)
                    briefing_text = res.get("output", "").strip()
                    if not res.get("success") or not briefing_text:
                        last_failed_attempt = now
                        logger.error(
                            "Briefing non généré : %s",
                            res.get("error", "sortie vide"),
                        )
                    else:
                        if "telegram" in pending:
                            try:
                                if await send_broadcast_message(briefing_text):
                                    mark_platform_sent(today_key, "telegram")
                                    logger.info("Briefing diffusé sur Telegram.")
                                else:
                                    last_failed_attempt = now
                            except Exception as exc:
                                last_failed_attempt = now
                                logger.error("Erreur diffusion briefing Telegram: %s", exc)

                        if "discord" in pending:
                            try:
                                if await broadcast_discord_message(briefing_text):
                                    mark_platform_sent(today_key, "discord")
                                    logger.info("Briefing diffusé sur Discord.")
                                else:
                                    last_failed_attempt = now
                            except Exception as exc:
                                last_failed_attempt = now
                                logger.error("Erreur diffusion briefing Discord: %s", exc)

                        if not pending_platforms(today_key):
                            last_failed_attempt = None
                            logger.info("Briefing matinal %s diffusé partout.", today_key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Erreur dans le planificateur de briefing matinal: %s", exc)

        await asyncio.sleep(30)


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
        from bots.telegram_bot import send_telegram_reminder

        await send_telegram_reminder(reminder)
        return

    if platform == "discord":
        from bots.discord_bot import send_discord_reminder

        await send_discord_reminder(int(chat_id), reminder)
        return

    if platform == "desktop":
        logger.info("Rappel bureau #%s traité pour %s : %s", reminder.get("id"), chat_id, message)
        return

    raise ValueError(f"Plateforme de rappel inconnue : {platform!r}")


async def reminder_scheduler() -> None:
    """Tâche de fond qui distribue les rappels arrivés à échéance."""
    init_db()
    logger.info(
        "Planificateur de rappels actif (vérification toutes les 5s, récurrences et backoff)."
    )
    while True:
        try:
            for reminder in get_due_reminders():
                reminder_id = reminder.get("id")
                try:
                    await distribute_reminder(reminder)

                    recurrence = reminder.get("recurrence")
                    if recurrence:
                        current = datetime.datetime.fromisoformat(
                            reminder["remind_at"]
                        )
                        next_at = next_occurrence_after(
                            current,
                            recurrence,
                            datetime.datetime.now(TIMEZONE),
                        )
                        if next_at is not None:
                            reschedule_reminder(reminder_id, next_at)
                            logger.info(
                                "Rappel récurrent #%s reprogrammé pour %s.",
                                reminder_id,
                                next_at.strftime("%d/%m à %H:%M"),
                            )
                            continue

                    mark_reminder_sent(reminder_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(
                        "Erreur distribution rappel #%s: %s",
                        reminder_id,
                        exc,
                    )
                    retry_at = mark_reminder_failure(reminder_id, str(exc))
                    logger.info(
                        "Rappel #%s : nouvelle tentative à %s.",
                        reminder_id,
                        retry_at.strftime("%H:%M:%S"),
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

    # Démarre les tâches de fond (briefing + rappels + préchauffage STT)
    scheduler_task = asyncio.create_task(morning_briefing_scheduler())
    reminder_task = asyncio.create_task(reminder_scheduler())
    warmup_task = asyncio.create_task(warmup_stt())

    logger.info("render-gateway prêt à recevoir des requêtes.")

    yield  # L'application tourne

    # Arrêt
    logger.info("=== Arrêt du render-gateway ===")
    for task in (scheduler_task, reminder_task, warmup_task):
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


# ── API Desktop / Client générique (Noctalia, scripts, etc.) ─────────────────


class AskRequest(BaseModel):
    """Corps d'une requête de question depuis un client externe."""

    prompt: str = Field(..., min_length=1, description="La question ou commande")
    platform: str = Field(default="desktop", description="Plateforme d'origine")
    user_id: str = Field(default="desktop", description="Identifiant utilisateur")
    timeout: int = Field(default=180, ge=5, le=600, description="Timeout en secondes")
    continue_session: bool = Field(
        default=True, description="Reprendre la session en cours"
    )
    reset_session: bool = Field(
        default=False, description="Réinitialiser la session avant la requête"
    )
    model: Optional[str] = Field(default=None, description="Modèle à utiliser")
    effort: Optional[str] = Field(default=None, description="Niveau d'effort")


class BriefingRequest(BaseModel):
    """Corps d'une requête de briefing."""

    force_refresh: bool = Field(
        default=False, description="Ignorer le cache et régénérer"
    )


@app.post(
    "/api/ask",
    tags=["desktop"],
    summary="Poser une question à l'assistant (client générique)",
    dependencies=[Depends(require_secret)],
)
async def api_ask(payload: AskRequest) -> dict:
    """Endpoint universel pour interroger l'assistant depuis n'importe quel
    client HTTP authentifié (bureau Noctalia, scripts, applications tierces).

    Réutilise ``forward_to_runner()`` — même logique que les bots Telegram et
    Discord — mais sans passer par un bot. La session est partagée avec les
    autres plateformes (même ``home-runner``).
    """
    from gateway import forward_to_runner

    logger.info(
        "API /api/ask — platform=%s user=%s prompt=%.60s…",
        payload.platform,
        payload.user_id,
        payload.prompt,
    )

    result = await forward_to_runner(
        prompt=payload.prompt,
        timeout=payload.timeout,
        model=payload.model,
        effort=payload.effort,
        continue_session=payload.continue_session,
        reset_session=payload.reset_session,
    )

    return {
        "success": result.get("success", False),
        "output": result.get("output", ""),
        "error": result.get("error"),
    }


@app.get(
    "/api/status",
    tags=["desktop"],
    summary="État complet du gateway et du runner (pour les clients bureau)",
    dependencies=[Depends(require_secret)],
)
async def api_status() -> dict:
    """Agrège l'état du gateway et du home-runner en un seul appel.

    Utilisé par ``nc-assistant etat`` pour afficher l'indicateur en ligne /
    hors ligne dans l'île Quickshell.
    """
    from gateway import fetch_health, fetch_session

    runner_health = await fetch_health()
    session_info = await fetch_session()

    runner_alive = runner_health.get("status") == "ok"
    discord_token = os.getenv("DISCORD_TOKEN", "")

    return {
        "online": runner_alive,
        "version": VERSION,
        "runner": runner_health,
        "session": session_info if not session_info.get("error") else None,
        "gateway": {
            "home_server_configured": bool(HOME_SERVER_URL),
            "telegram_configured": bool(TELEGRAM_TOKEN),
            "discord_configured": bool(discord_token),
        },
    }


@app.post(
    "/api/briefing",
    tags=["desktop"],
    summary="Générer ou récupérer le briefing matinal",
    dependencies=[Depends(require_secret)],
)
async def api_briefing(payload: BriefingRequest) -> dict:
    """Retourne le briefing matinal (depuis le cache ou régénéré).

    Utilisé par ``nc-assistant briefing`` pour afficher un résumé quotidien
    dans une notification ou dans le chat bureau.
    """
    result = await get_briefing_text(
        force_refresh=payload.force_refresh, timeout=240
    )
    return {
        "success": result.get("success", False),
        "output": result.get("output", ""),
        "cached": result.get("cached", False),
        "error": result.get("error"),
    }


# ── API de gestion des rappels ────────────────────────────────────────────────


class ReminderCreateRequest(BaseModel):
    """Corps de création d'un rappel (utilisé par l'API et les intégrations)."""

    platform: str = Field(
        default="telegram", description="telegram, discord ou desktop"
    )
    chat_id: str = Field(..., description="ID du chat Telegram, salon Discord ou identifiant desktop")
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
    recurrence: Optional[str] = Field(
        default=None,
        description="Récurrence : daily, weekdays, weekly:0-6, monthly, every:days:N",
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
    """Crée un rappel persistant pour Telegram, Discord ou Desktop."""
    platform = payload.platform.strip().lower()
    if platform not in {"telegram", "discord", "desktop"}:
        raise HTTPException(
            status_code=400,
            detail="platform doit être telegram, discord ou desktop",
        )

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
        recurrence=payload.recurrence,
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
