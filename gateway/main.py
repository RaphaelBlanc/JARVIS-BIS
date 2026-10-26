"""
main.py — Serveur FastAPI principal de la passerelle Gateway.

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
import tempfile
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
import uuid
import json
import re

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from core.hardware_relay import HARDWARE_RELAY
from core.node_hub import NODE_HUB
from core.tools.base import ToolRegistry
from core.tools.dispatcher import execute_tool_action

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
    init_node_hub_confirm_handlers,
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
from stt import MAX_AUDIO_BYTES, transcribe_audio, warmup_stt  # noqa: E402
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
GATEWAY_PUBLIC_URL: str = os.getenv("GATEWAY_PUBLIC_URL", "").rstrip("/")
VERSION: str = "1.0.0"

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("Valeur invalide pour %s ; valeur par défaut %s.", name, default)
        return default


# Protection de l'API publique (client Noctalia, scripts, etc.).
# 0 désactive la limite. La limite est appliquée par adresse IP sur /api/*.
DESKTOP_RATE_LIMIT: int = _env_int("DESKTOP_RATE_LIMIT_PER_MINUTE", 60)
DESKTOP_RATE_WINDOW: float = 60.0
# Derrière un tunnel (Cloudflare, reverse proxy), l'IP réelle est dans un
# en-tête. Ne l'activer que si le gateway n'est joignable qu'à travers ce proxy.
TRUST_PROXY_HEADERS: bool = os.getenv("TRUST_PROXY_HEADERS", "0").strip() == "1"

_RATE_HITS: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    """Retourne l'adresse du client, en tenant compte d'un proxy de confiance."""
    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("cf-connecting-ip") or request.headers.get(
            "x-forwarded-for", ""
        )
        if forwarded:
            return forwarded.split(",", 1)[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def reset_rate_limit() -> None:
    """Vide le compteur (utile pour les tests et après un redémarrage)."""
    _RATE_HITS.clear()

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
    logger.info("=== Démarrage de la Gateway v%s ===", VERSION)
    _check_env()

    # Démarre le bot Discord dans un thread daemon
    start_discord_bot()

    # Configure Telegram (Webhook si en prod avec URL publique, ou Polling si en local)
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

    # Active les écouteurs de confirmation distante TOTP pour les nœuds clients
    init_node_hub_confirm_handlers()

    # Démarre les tâches de fond (briefing + rappels + préchauffage STT)
    scheduler_task = asyncio.create_task(morning_briefing_scheduler())
    reminder_task = asyncio.create_task(reminder_scheduler())
    warmup_task = asyncio.create_task(warmup_stt())

    logger.info("Gateway prête à recevoir des requêtes.")

    yield  # L'application tourne

    # Arrêt
    logger.info("=== Arrêt de la Gateway ===")
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
    try:
        from gateway import close_http_client
        await close_http_client()
    except Exception as exc:
        logger.error("Erreur lors de la fermeture du client HTTP: %s", exc)



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


@app.middleware("http")
async def limit_desktop_api(request: Request, call_next):
    """Limite les appels à l'API publique pour résister à une exposition Internet.

    Les routes de santé et le webhook Telegram ne sont pas concernés.
    """
    if (
        DESKTOP_RATE_LIMIT > 0
        and request.url.path.startswith("/api/")
        and request.url.path not in {"/api/health"}
    ):
        ip = _client_ip(request)
        now = time.monotonic()
        if len(_RATE_HITS) > 10_000:
            cutoff = now - DESKTOP_RATE_WINDOW
            for ancien in [k for k, v in _RATE_HITS.items() if not v or v[-1] <= cutoff]:
                _RATE_HITS.pop(ancien, None)
        hits = _RATE_HITS[ip]
        while hits and hits[0] <= now - DESKTOP_RATE_WINDOW:
            hits.popleft()
        if len(hits) >= DESKTOP_RATE_LIMIT:
            retry_after = max(1, int(DESKTOP_RATE_WINDOW - (now - hits[0])))
            logger.warning("Limite API atteinte pour %s.", ip)
            return JSONResponse(
                status_code=429,
                content={"detail": "Trop de requêtes. Réessaie dans un instant."},
                headers={"Retry-After": str(retry_after)},
            )
        hits.append(now)
    return await call_next(request)


# ── API Desktop / Client générique (Noctalia, scripts, etc.) ─────────────────


_ASK_SESSION_KEY_RE = re.compile(r'^(?:default|desktop:[\w.@-]{1,180})$', re.ASCII)


class FileAttachment(BaseModel):
    """Fichier joint encodé en base64 pour l'assistant."""

    filename: str = Field(..., max_length=255, description="Nom original du fichier.")
    content_base64: str = Field(..., description="Contenu du fichier encodé en base64.")
    mime_type: Optional[str] = Field(default=None, max_length=128, description="Type MIME optionnel.")


class AskRequest(BaseModel):
    """Corps d'une requête de question depuis un client externe."""

    prompt: str = Field(..., min_length=1, max_length=8000, description="La question ou commande")
    platform: str = Field(default="desktop", description="Plateforme d'origine")
    user_id: str = Field(default="desktop", description="Identifiant utilisateur")
    client_id: Optional[str] = Field(default=None, description="Identifiant unique de la machine cliente (laptop, tour)")
    session_key: Optional[str] = Field(default=None, description="Clé de session isolée")
    target_node: Optional[str] = Field(default=None, description="Nœud distant ciblé pour les actions matérielles")
    timeout: int = Field(default=600, ge=5, le=1800, description="Timeout en secondes")
    continue_session: bool = Field(
        default=True, description="Reprendre la session en cours"
    )
    reset_session: bool = Field(
        default=False, description="Réinitialiser la session avant la requête"
    )
    model: Optional[str] = Field(default=None, description="Modèle à utiliser")
    effort: Optional[str] = Field(default=None, description="Niveau d'effort")
    files: Optional[list[FileAttachment]] = Field(default=None, description="Fichiers joints encodés en base64")
    image_base64: Optional[str] = Field(default=None, description="Image encodée en base64 (compatibilité)")
    image_filename: Optional[str] = Field(default=None, description="Nom original de l'image (compatibilité)")


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

    Isole les conversations par machine/utilisateur via effective_session_key.
    Interdit toute usurpation de session Telegram/Discord via HTTP.
    """
    from gateway import forward_to_runner

    if payload.platform and payload.platform.strip().lower() != "desktop":
        raise HTTPException(
            status_code=400,
            detail="Plateforme refusée : seul 'desktop' est autorisé sur cette route",
        )
    if payload.session_key is not None:
        sk = str(payload.session_key).strip()
        if not _ASK_SESSION_KEY_RE.fullmatch(sk):
            raise HTTPException(
                status_code=400,
                detail="Clé de session refusée : seules les clés desktop:... ou default sont autorisées",
            )
        effective_session_key = sk
    else:
        ident = (payload.client_id or payload.user_id or "default").strip()
        ident_clean = re.sub(r"[^\w.@-]", "", ident)[:180] or "default"
        effective_session_key = f"desktop:{ident_clean}"
    logger.info(
        "API /api/ask — platform=%s user=%s client_id=%s session_key=%s prompt=%.60s…",
        payload.platform,
        payload.user_id,
        payload.client_id,
        effective_session_key,
        payload.prompt,
    )

    result = await forward_to_runner(
        prompt=payload.prompt,
        timeout=payload.timeout,
        model=payload.model,
        effort=payload.effort,
        continue_session=payload.continue_session,
        reset_session=payload.reset_session,
        session_key=effective_session_key,
        files=payload.files,
        image_base64=payload.image_base64,
        image_filename=payload.image_filename,
    )

    output = result.get("output", "")
    choices = result.get("choices") or []
    msg_type = result.get("type") or ("question" if choices else "message")

    if not choices and output:
        tag_match = re.search(r"\[(?:CHOIX|CHOICES)\s*:\s*([^\]]+)\]", output, re.IGNORECASE)
        if tag_match:
            items = [c.strip() for c in tag_match.group(1).split("|") if c.strip()]
            if len(items) >= 2:
                choices = items
                msg_type = "question"
                output = (output[:tag_match.start()].rstrip() + " " + output[tag_match.end():].lstrip()).strip()

    return {
        "success": result.get("success", False),
        "output": output,
        "error": result.get("error"),
        "type": msg_type,
        "choices": choices,
    }


@app.post(
    "/api/transcribe",
    tags=["desktop"],
    summary="Transcrire un enregistrement audio (dictée Noctalia)",
    dependencies=[Depends(require_secret)],
)
async def api_transcribe(request: Request, language: Optional[str] = None) -> dict:
    """Reçoit un fichier audio brut (WAV/OGG/MP3/M4A) et renvoie sa transcription.

    Utilisé par la dictée du client Noctalia. Le corps de la requête est l'audio
    lui-même, avec son type MIME dans ``Content-Type``. Aucun multipart n'est
    nécessaire, ce qui évite une dépendance supplémentaire.
    """
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Enregistrement audio vide")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Enregistrement audio trop volumineux")

    content_type = (request.headers.get("content-type") or "audio/wav").split(";", 1)[0].strip().lower()
    suffixes = {
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/wave": ".wav",
        "audio/ogg": ".ogg",
        "audio/opus": ".ogg",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/webm": ".webm",
    }
    suffix = suffixes.get(content_type, ".wav")

    fd, temp_name = tempfile.mkstemp(prefix="nc-dictee-", suffix=suffix, dir="/tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        text, engine = await transcribe_audio(Path(temp_name), language=language)
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass

    if not text:
        return {"success": False, "text": "", "engine": engine, "error": engine}
    return {"success": True, "text": text, "engine": engine, "error": None}


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

    runner_health, session_info = await asyncio.gather(
        fetch_health(), fetch_session()
    )

    runner_alive = runner_health.get("status") == "ok"
    discord_token = os.getenv("DISCORD_TOKEN", "")

    tools_list = [
        {
            "id": t.id,
            "name": t.name,
            "icon": t.icon,
            "enabled": t.is_enabled,
            "requires_local_hardware": t.requires_local_hardware,
        }
        for t in ToolRegistry.list_tools()
    ]

    return {
        "online": runner_alive,
        "version": VERSION,
        "deploy_mode": os.getenv("DEPLOY_MODE", "local"),
        "runner": runner_health,
        "session": session_info if not session_info.get("error") else None,
        "tools": tools_list,
        "gateway": {
            "home_server_configured": bool(HOME_SERVER_URL),
            "telegram_configured": bool(TELEGRAM_TOKEN),
            "discord_configured": bool(discord_token),
            "public_url": GATEWAY_PUBLIC_URL or None,
        },
    }


class ActionRequest(BaseModel):
    """Corps d'une requête pour exécuter une action de module/tool."""

    tool: str = Field(..., description="ID de l'outil (ex: shopping, moodle, expenses, system_pc, obsidian)")
    action: str = Field(..., description="Action à exécuter (ex: list, devoirs, add, screenshot, kb)")
    params: Optional[dict] = Field(default_factory=dict, description="Paramètres spécifiques à l'action")
    platform: str = Field(default="desktop", description="Plateforme appelante (desktop, telegram, discord, noctalia)")


def _resolve_request_origin(request: Request) -> Optional[dict]:
    """Résout l'origine authentifiée uniquement à partir de l'en-tête secret X-Jarvis-Origin-Token (≥ 128 bits),
    jamais depuis le corps JSON de la requête.
    """
    token = request.headers.get("X-Jarvis-Origin-Token", "").strip()
    return NODE_HUB.resolve_origin_from_token(token)


def _resolve_request_session_key(request: Request) -> Optional[str]:
    token = request.headers.get("X-Jarvis-Origin-Token", "").strip()
    if token and len(token) >= 32:
        return NODE_HUB._token_to_session_key.get(token)
    return None


@app.post(
    "/api/action",
    tags=["desktop"],
    summary="Exécuter une action d'un module modulaire (style MCP)",
    dependencies=[Depends(require_secret)],
)
async def api_action(request: Request, payload: ActionRequest) -> dict:
    """Permet au bureau Noctalia ou à des scripts d'exécuter directement
    n'importe quelle action de module (courses, devoirs Moodle, finances, capture d'écran, etc.).
    """
    params = dict(payload.params or {})
    params.pop("origin", None)
    params.pop("session_key", None)
    return await execute_tool_action(
        tool_id=payload.tool,
        action=payload.action,
        params=params,
        platform=payload.platform,
        session_key=_resolve_request_session_key(request),
    )


# ── API Node Hub (Gestion des machines clientes distantes) ─────────────────────

class NodeActionRequest(BaseModel):
    action: str = Field(..., description="Action à exécuter (exec, read_file, write_file, screenshot, lock, etc.)")
    params: Optional[dict] = Field(default_factory=dict, description="Paramètres de l'action")
    timeout: float = Field(default=80.0, gt=5.0, le=600.0, allow_inf_nan=False, description="Délai maximum en secondes")

class NodeExecRequest(BaseModel):
    command: str = Field(..., description="Commande shell à exécuter")
    timeout: float = Field(default=75.0, gt=5.0, le=600.0, allow_inf_nan=False, description="Délai maximum en secondes")

class NodeReadFileRequest(BaseModel):
    path: str = Field(..., description="Chemin absolu ou relatif du fichier à lire")
    timeout: float = Field(default=80.0, gt=5.0, le=600.0, allow_inf_nan=False, description="Délai maximum en secondes")

class NodeWriteFileRequest(BaseModel):
    path: str = Field(..., description="Chemin du fichier à écrire")
    content: str = Field(..., description="Contenu du fichier")
    timeout: float = Field(default=80.0, gt=5.0, le=600.0, allow_inf_nan=False, description="Délai maximum en secondes")


@app.get(
    "/api/nodes",
    tags=["nodes"],
    summary="Lister les machines clientes connectées (Laptop, Tour, etc.)",
    dependencies=[Depends(require_secret)],
)
async def api_list_nodes() -> dict:
    nodes = NODE_HUB.list_nodes()
    return {"nodes": nodes, "count": len(nodes)}


@app.post(
    "/api/node/{node_id}/action",
    tags=["nodes"],
    summary="Envoyer une action à une machine cliente",
    dependencies=[Depends(require_secret)],
)
async def api_node_action(node_id: str, request: Request, payload: NodeActionRequest) -> dict:
    return await NODE_HUB.send_action(
        node_id=node_id,
        action=payload.action,
        params=payload.params,
        timeout=payload.timeout,
        origin=_resolve_request_origin(request),
    )


@app.post(
    "/api/node/{node_id}/exec",
    tags=["nodes"],
    summary="Exécuter une commande shell sur une machine cliente",
    dependencies=[Depends(require_secret)],
)
async def api_node_exec(node_id: str, request: Request, payload: NodeExecRequest) -> dict:
    return await NODE_HUB.exec_cmd(
        command=payload.command,
        node_id=node_id,
        timeout=payload.timeout,
        origin=_resolve_request_origin(request),
    )


@app.post(
    "/api/node/{node_id}/read_file",
    tags=["nodes"],
    summary="Lire un fichier sur une machine cliente",
    dependencies=[Depends(require_secret)],
)
async def api_node_read_file(node_id: str, request: Request, payload: NodeReadFileRequest) -> dict:
    return await NODE_HUB.read_file(
        path=payload.path,
        node_id=node_id,
        timeout=payload.timeout,
        origin=_resolve_request_origin(request),
    )


@app.post(
    "/api/node/{node_id}/write_file",
    tags=["nodes"],
    summary="Écrire un fichier sur une machine cliente",
    dependencies=[Depends(require_secret)],
)
async def api_node_write_file(node_id: str, request: Request, payload: NodeWriteFileRequest) -> dict:
    return await NODE_HUB.write_file(
        path=payload.path,
        content=payload.content,
        node_id=node_id,
        timeout=payload.timeout,
        origin=_resolve_request_origin(request),
    )


@app.post(
    "/api/node/{node_id}/session_end",
    tags=["nodes"],
    summary="Fermer immédiatement la session de confiance active sur une machine cliente (ou 'all')",
    dependencies=[Depends(require_secret)],
)
async def api_node_session_end(node_id: str) -> dict:
    closed = await NODE_HUB.send_session_end(node_id=node_id)
    return {"success": True, "closed_nodes": closed}


@app.websocket("/api/node/ws")
async def node_websocket_endpoint(websocket: WebSocket):
    """Endpoint WebSocket pour la connexion permanente des agents clients JARVIS.
    Authentification stricte par en-tête X-Secret-Key (aucun secret dans l'URL)."""
    # Refus strict si le secret est passé dans l'URL pour éviter les fuites dans les logs et proxies
    if "secret" in websocket.query_params:
        logger.warning("Connexion WebSocket refusée : passage du secret en query param interdit.")
        await websocket.close(code=1008, reason="Secret dans l'URL interdit, utilisez le header X-Secret-Key")
        return

    header_secret = websocket.headers.get("X-Secret-Key")
    if not SECRET_KEY or not header_secret or not hmac.compare_digest(header_secret, SECRET_KEY):
        logger.warning("Connexion WebSocket refusée pour agent nœud : clé invalide ou absente.")
        await websocket.close(code=1008, reason="Clé secrète invalide ou absente (header X-Secret-Key requis)")
        return

    await websocket.accept()
    node_id = None
    try:
        # Attente du message d'enregistrement initial
        init_data = await asyncio.wait_for(websocket.receive_json(), timeout=15.0)
        if init_data.get("type") != "register":
            await websocket.close(code=1002, reason="Premier message doit être 'register'")
            return

        node_id = str(init_data.get("node_id") or uuid.uuid4().hex[:8])
        await NODE_HUB.register_node(node_id=node_id, websocket=websocket, info=init_data)
        logger.info("Nœud client connecté et enregistré : %s", node_id)

        # Boucle d'écoute
        while True:
            msg = await websocket.receive_json()
            NODE_HUB.handle_incoming_message(node_id=node_id, message=msg)

    except WebSocketDisconnect:
        logger.info("Nœud client déconnecté (WebSocketDisconnect) : %s", node_id)
    except asyncio.TimeoutError:
        logger.warning("Nœud client timeout lors de l'enregistrement.")
    except Exception as exc:
        logger.warning("Erreur connexion WebSocket nœud client %s : %s", node_id, exc)
    finally:
        if node_id:
            await NODE_HUB.unregister_node(node_id=node_id, websocket=websocket)


@app.get(
    "/api/stream",
    tags=["desktop"],
    summary="Streaming SSE pour réponses en temps réel dans Noctalia / Bureau",
)
async def api_stream(
    prompt: str,
    x_secret_key: Optional[str] = Header(default=None, alias="X-Secret-Key"),
):
    """Fournit un flux Server-Sent Events (SSE) pour afficher les réponses en temps réel."""
    if not SECRET_KEY or not x_secret_key or not hmac.compare_digest(x_secret_key, SECRET_KEY):
        raise HTTPException(status_code=401, detail="Clé API invalide")

    async def event_generator():
        from gateway import forward_to_runner
        res = await forward_to_runner(prompt=prompt, continue_session=True)
        output = res.get("output", "")
        # Envoi progressif par blocs pour l'effet temps réel
        words = output.split(" ")
        for i in range(0, len(words), 4):
            chunk = " ".join(words[i:i + 4]) + " "
            yield f"data: {json.dumps({'chunk': chunk})}\n\n"
            await asyncio.sleep(0.01)
        yield f"data: {json.dumps({'done': True, 'output': output})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post(
    "/api/apple-pay",
    tags=["integrations"],
    summary="Webhook pour enregistrement direct Apple Pay / Raccourcis iOS",
)
async def api_apple_pay(request: Request) -> dict:
    """Webhook pour recevoir les dépenses Apple Pay depuis Raccourcis iOS."""
    webhook_secret = os.getenv("JARVIS_WEBHOOK_SECRET", "")
    token = request.headers.get("X-Jarvis-Token") or request.query_params.get("token")
    if not webhook_secret or not token or not hmac.compare_digest(token, webhook_secret):
        raise HTTPException(status_code=401, detail="Token webhook invalide ou manquant")

    try:
        data = await request.json()
    except Exception:
        form = await request.form()
        data = dict(form)

    raw_amt = str(data.get("amount", "")).replace("€", "").replace(",", ".").strip()
    if not raw_amt:
        raise HTTPException(status_code=400, detail="Montant manquant")

    amount = float(raw_amt)
    merchant = str(data.get("merchant", data.get("description", "Achat Apple Pay"))).strip()
    category = data.get("category")

    return await execute_tool_action(
        "expenses",
        "add",
        {"amount": amount, "description": merchant, "category": category},
        platform="apple-pay",
    )



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
        "public_url_configured": bool(GATEWAY_PUBLIC_URL),
    }


# ── API Noctalia Management ───────────────────────────────────────────────────

@app.post(
    "/api/nc/manage",
    tags=["desktop"],
    summary="Adaptateur d'administration et de gestion pour Noctalia (Cockpit Super+Alt+A)",
    dependencies=[Depends(require_secret)],
)
async def nc_manage(request: Request):
    """Point d'entrée du protocole de gestion Noctalia (Jarvis Cockpit)."""
    raw = await request.body()
    if len(raw) > 65536:
        raise HTTPException(status_code=413, detail="Requête trop volumineuse")
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or not isinstance(body.get("params", {}), dict):
            raise ValueError
        try:
            from gateway.nc_management import dispatch
        except ImportError:
            from nc_management import dispatch
        s_key = body.get("session_key") or body.get("params", {}).get("session_key") or "default"
        return await dispatch(body.get("op"), body.get("params", {}), body.get("confirmed") is True, session_key=s_key)
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=400, detail="Paramètres invalides") from None

