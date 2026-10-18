"""
gateway.py — Client HTTP asynchrone vers le home-runner.

Fournit `forward_to_runner()`, qui envoie une requête POST au serveur maison
exposé via Cloudflare Tunnel et retourne la réponse normalisée.
"""

import httpx
import os
import logging
import time

logger = logging.getLogger(__name__)

HOME_SERVER_URL: str = os.getenv("HOME_SERVER_URL", "")
SECRET_KEY: str = os.getenv("SECRET_KEY", "")
# 0 (ou une valeur négative) désactive la troncature. La valeur par défaut
# laisse passer les réponses longues ; les bots se chargent du découpage.
MAX_RESPONSE_LENGTH: int = int(os.getenv("MAX_RESPONSE_LENGTH", "50000"))


async def forward_to_runner(
    prompt: str,
    timeout: int = 180,
    model: str | None = None,
    effort: str | None = None,
    mode: str | None = None,
    continue_session: bool = True,
    reset_session: bool = False,
    permission_mode: str | None = None,
    image_base64: str | None = None,
    image_filename: str | None = None,
) -> dict:
    """Transmet un prompt au home-runner et retourne sa réponse."""
    if not HOME_SERVER_URL:
        logger.error("HOME_SERVER_URL n'est pas configuré.")
        return {"success": False, "output": "", "error": "HOME_SERVER_URL non configuré"}

    logger.info("Transmission au home-runner: prompt=%.60s… (continue=%s, timeout=%ds, image=%s)", prompt, continue_session, timeout, bool(image_base64))

    # Marge de 15s pour que httpx ne coupe pas avant que le runner ait renvoyé son diagnostic
    client_timeout = float(timeout + 15)

    try:
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            payload = {
                "prompt": prompt,
                "timeout": timeout,
                "continue_session": continue_session,
                "reset_session": reset_session,
            }
            if model:
                payload["model"] = model
            if effort:
                payload["effort"] = effort
            if mode:
                payload["mode"] = mode
            if permission_mode:
                payload["permission_mode"] = permission_mode
            if image_base64:
                payload["image_base64"] = image_base64
                payload["image_filename"] = image_filename or "upload.png"

            resp = await client.post(
                f"{HOME_SERVER_URL}/run",
                json=payload,
                headers={"X-Secret-Key": SECRET_KEY},
            )
            resp.raise_for_status()
            data: dict = resp.json()

            output: str = data.get("output", "")
            if MAX_RESPONSE_LENGTH > 0 and len(output) > MAX_RESPONSE_LENGTH:
                logger.info(
                    "Réponse tronquée: %d → %d chars.", len(output), MAX_RESPONSE_LENGTH
                )
                data["output"] = output[:MAX_RESPONSE_LENGTH] + "\n\n...(réponse tronquée)"

            logger.info("Réponse reçue — succès: %s.", data.get("success"))
            return data

    except httpx.TimeoutException:
        logger.warning("Timeout atteint (%ds) en attendant la réponse du home-runner.", timeout)
        return {
            "success": False,
            "output": "",
            "error": f"Le serveur maison n'a pas répondu à temps ({timeout}s)",
        }

    except httpx.HTTPStatusError as e:
        logger.error("Erreur HTTP %d depuis le home-runner.", e.response.status_code)
        return {
            "success": False,
            "output": "",
            "error": f"Erreur HTTP {e.response.status_code}",
        }

    except Exception as e:
        logger.exception("Erreur inattendue dans forward_to_runner.")
        return {"success": False, "output": "", "error": str(e)}


async def fetch_health() -> dict:
    """Récupère l'état de santé détaillé du home-runner."""
    if not HOME_SERVER_URL:
        return {"status": "error", "error": "HOME_SERVER_URL non configuré"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{HOME_SERVER_URL}/health")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("Erreur fetch_health: %s", e)
        return {"status": "error", "error": str(e)}


async def fetch_models() -> dict:
    """Récupère la liste des modèles disponibles auprès du home-runner."""
    if not HOME_SERVER_URL:
        return {"success": False, "models": [], "error": "HOME_SERVER_URL non configuré"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{HOME_SERVER_URL}/models",
                headers={"X-Secret-Key": SECRET_KEY},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("Erreur fetch_models: %s", e)
        return {"success": False, "models": [], "error": str(e)}


async def fetch_session() -> dict:
    """Récupère les informations de session du home-runner."""
    if not HOME_SERVER_URL:
        return {"error": "HOME_SERVER_URL non configuré"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{HOME_SERVER_URL}/session",
                headers={"X-Secret-Key": SECRET_KEY},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("Erreur fetch_session: %s", e)
        return {"error": str(e)}


async def update_session_remote(
    model: str | None = None,
    effort: str | None = None,
    mode: str | None = None,
    permission_mode: str | None = None,
) -> dict:
    """Met à jour les paramètres de session du home-runner."""
    if not HOME_SERVER_URL:
        return {"error": "HOME_SERVER_URL non configuré"}
    try:
        payload = {}
        if model:
            payload["model"] = model
        if effort:
            payload["effort"] = effort
        if mode:
            payload["mode"] = mode
        if permission_mode:
            payload["permission_mode"] = permission_mode

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{HOME_SERVER_URL}/session",
                json=payload,
                headers={"X-Secret-Key": SECRET_KEY},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("Erreur update_session_remote: %s", e)
        return {"error": str(e)}


_CONV_CACHE: list[dict] = []
_CONV_CACHE_EXPIRY: float = 0.0


def invalidate_conversations_cache() -> None:
    """Invalide le cache local des conversations."""
    global _CONV_CACHE, _CONV_CACHE_EXPIRY
    _CONV_CACHE = []
    _CONV_CACHE_EXPIRY = 0.0


async def reset_session_remote() -> dict:
    """Réinitialise la session active sur le home-runner."""
    invalidate_conversations_cache()
    if not HOME_SERVER_URL:
        return {"error": "HOME_SERVER_URL non configuré"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{HOME_SERVER_URL}/session/reset",
                headers={"X-Secret-Key": SECRET_KEY},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("Erreur reset_session_remote: %s", e)
        return {"error": str(e)}


async def fetch_conversations(limit: int = 8, use_cache: bool = True) -> list[dict]:
    """Récupère la liste des conversations passées auprès du home-runner (avec cache 15s)."""
    global _CONV_CACHE, _CONV_CACHE_EXPIRY
    now = time.time()
    if use_cache and _CONV_CACHE and now < _CONV_CACHE_EXPIRY:
        return _CONV_CACHE[:limit]

    if not HOME_SERVER_URL:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{HOME_SERVER_URL}/conversations?limit={max(limit, 25)}",
                headers={"X-Secret-Key": SECRET_KEY},
            )
            resp.raise_for_status()
            data = resp.json()
            convs = data.get("conversations", [])
            _CONV_CACHE = convs
            _CONV_CACHE_EXPIRY = now + 15.0
            return convs[:limit]
    except Exception as e:
        logger.error("Erreur fetch_conversations: %s", e)
        return _CONV_CACHE[:limit] if _CONV_CACHE else []


async def resume_conversation_remote(conversation_id: str) -> dict:
    """Demande au home-runner de reprendre une conversation spécifique."""
    invalidate_conversations_cache()
    if not HOME_SERVER_URL:
        return {"error": "HOME_SERVER_URL non configuré"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{HOME_SERVER_URL}/conversation/resume",
                json={"conversation_id": conversation_id},
                headers={"X-Secret-Key": SECRET_KEY},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("Erreur resume_conversation_remote: %s", e)
        return {"error": str(e)}


async def delete_conversation_remote(conversation_id: str) -> dict:
    """Demande au home-runner de supprimer une conversation passée."""
    invalidate_conversations_cache()
    if not HOME_SERVER_URL:
        return {"success": False, "error": "HOME_SERVER_URL non configuré"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(
                f"{HOME_SERVER_URL}/conversation/{conversation_id}",
                headers={"X-Secret-Key": SECRET_KEY},
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        detail = "Erreur de suppression"
        try:
            detail = e.response.json().get("detail", detail)
        except Exception:
            pass
        return {"success": False, "error": detail}
    except Exception as e:
        logger.error("Erreur delete_conversation_remote: %s", e)
        return {"success": False, "error": str(e)}


