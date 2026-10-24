"""
gateway.py — Client HTTP asynchrone vers le home-runner.

Fournit `forward_to_runner()`, qui envoie une requête POST au serveur maison
exposé via Cloudflare Tunnel et retourne la réponse normalisée.
"""

import asyncio
import httpx
import os
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

HOME_SERVER_URL: str = os.getenv("HOME_SERVER_URL", "")
SECRET_KEY: str = os.getenv("SECRET_KEY", "")
# 0 (ou une valeur négative) désactive la troncature. La valeur par défaut
# laisse passer les réponses longues ; les bots se chargent du découpage.
MAX_RESPONSE_LENGTH: int = int(os.getenv("MAX_RESPONSE_LENGTH", "50000"))

_SHARED_CLIENT: Optional[httpx.AsyncClient] = None
_SHARED_CLIENT_LOOP: Optional[asyncio.AbstractEventLoop] = None
_CLIENTS_BY_LOOP: dict[Optional[asyncio.AbstractEventLoop], httpx.AsyncClient] = {}


def _get_http_client() -> httpx.AsyncClient:
    """Retourne un client HTTP partagé par boucle d'événements avec pool de connexions keep-alive."""
    global _SHARED_CLIENT, _SHARED_CLIENT_LOOP
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    client = _CLIENTS_BY_LOOP.get(current_loop)
    if client is None or client.is_closed:
        if len(_CLIENTS_BY_LOOP) >= 4:
            old_loop, old_client = next(iter(_CLIENTS_BY_LOOP.items()))
            _CLIENTS_BY_LOOP.pop(old_loop, None)
            if old_client is not None and not old_client.is_closed and old_loop is current_loop:
                asyncio.create_task(old_client.aclose())
        client = httpx.AsyncClient(
            timeout=10.0,
            limits=httpx.Limits(
                max_keepalive_connections=10,
                max_connections=25,
                keepalive_expiry=60.0,
            ),
        )
        _CLIENTS_BY_LOOP[current_loop] = client

    _SHARED_CLIENT = client
    _SHARED_CLIENT_LOOP = current_loop
    return client


async def close_http_client() -> None:
    """Ferme proprement les clients HTTP partagés à l'arrêt de la Gateway."""
    global _SHARED_CLIENT, _SHARED_CLIENT_LOOP
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    for loop_key, client in list(_CLIENTS_BY_LOOP.items()):
        if client is not None and not client.is_closed:
            if loop_key is current_loop or loop_key is None:
                await client.aclose()
    _CLIENTS_BY_LOOP.clear()
    if _SHARED_CLIENT is not None and not _SHARED_CLIENT.is_closed:
        await _SHARED_CLIENT.aclose()
    _SHARED_CLIENT = None
    _SHARED_CLIENT_LOOP = None


async def forward_to_runner(
    prompt: str,
    timeout: int = 600,
    model: str | None = None,
    effort: str | None = None,
    mode: str | None = None,
    continue_session: bool = True,
    reset_session: bool = False,
    permission_mode: str | None = None,
    image_base64: str | None = None,
    image_filename: str | None = None,
    session_key: str | None = None,
) -> dict:
    """Transmet un prompt au home-runner et retourne sa réponse."""
    if not HOME_SERVER_URL:
        logger.error("HOME_SERVER_URL n'est pas configuré.")
        return {"success": False, "output": "", "error": "HOME_SERVER_URL non configuré"}

    eff_key = (session_key or "default").strip() or "default"
    if reset_session or prompt.strip() in ("/reset", "/new", "/clear"):
        try:
            from core.node_hub import NODE_HUB
            NODE_HUB.rotate_conversation_token(eff_key)
            await NODE_HUB.send_session_end()
        except Exception:
            pass

    logger.info("Transmission au home-runner [%s]: prompt=%.60s… (continue=%s, timeout=%ds, image=%s)", eff_key, prompt, continue_session, timeout, bool(image_base64))

    # Marge de 15s pour que httpx ne coupe pas avant que le runner ait renvoyé son diagnostic
    client_timeout = float(timeout + 15)

    try:
        client = _get_http_client()
        payload = {
            "prompt": prompt,
            "timeout": timeout,
            "continue_session": continue_session,
            "reset_session": reset_session,
        }
        if session_key:
            payload["session_key"] = session_key
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
            timeout=client_timeout,
        )
        resp.raise_for_status()
        data: dict = resp.json()

        sess_obj = data.get("session")
        if isinstance(sess_obj, dict) and sess_obj.get("session_token"):
            try:
                from core.node_hub import NODE_HUB
                NODE_HUB.set_conversation_token(eff_key, str(sess_obj["session_token"]))
            except Exception:
                pass

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
        client = _get_http_client()
        resp = await client.get(f"{HOME_SERVER_URL}/health", timeout=10.0)
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
        client = _get_http_client()
        resp = await client.get(
            f"{HOME_SERVER_URL}/models",
            headers={"X-Secret-Key": SECRET_KEY},
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Erreur fetch_models: %s", e)
        return {"success": False, "models": [], "error": str(e)}


async def fetch_session(session_key: str = "default") -> dict:
    """Récupère les informations de session du home-runner."""
    if not HOME_SERVER_URL:
        return {"error": "HOME_SERVER_URL non configuré"}
    try:
        client = _get_http_client()
        resp = await client.get(
            f"{HOME_SERVER_URL}/session",
            params={"session_key": session_key},
            headers={"X-Secret-Key": SECRET_KEY},
            timeout=10.0,
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
    session_key: str = "default",
) -> dict:
    """Met à jour les paramètres de session du home-runner."""
    if not HOME_SERVER_URL:
        return {"error": "HOME_SERVER_URL non configuré"}
    try:
        payload = {"session_key": session_key}
        if model:
            payload["model"] = model
        if effort:
            payload["effort"] = effort
        if mode:
            payload["mode"] = mode
        if permission_mode:
            payload["permission_mode"] = permission_mode

        client = _get_http_client()
        resp = await client.post(
            f"{HOME_SERVER_URL}/session",
            json=payload,
            headers={"X-Secret-Key": SECRET_KEY},
            timeout=10.0,
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


async def reset_session_remote(session_key: str = "default") -> dict:
    """Réinitialise la session active sur le home-runner."""
    invalidate_conversations_cache()
    try:
        from core.node_hub import NODE_HUB
        NODE_HUB.rotate_conversation_token(session_key)
        await NODE_HUB.send_session_end()
    except Exception:
        pass
    if not HOME_SERVER_URL:
        return {"error": "HOME_SERVER_URL non configuré"}
    try:
        client = _get_http_client()
        resp = await client.post(
            f"{HOME_SERVER_URL}/session/reset",
            params={"session_key": session_key},
            headers={"X-Secret-Key": SECRET_KEY},
            timeout=10.0,
        )
        resp.raise_for_status()
        res = resp.json()
        sess_obj = res.get("session") if isinstance(res, dict) else None
        if isinstance(sess_obj, dict) and sess_obj.get("session_token"):
            try:
                from core.node_hub import NODE_HUB
                NODE_HUB.set_conversation_token(session_key, str(sess_obj["session_token"]))
            except Exception:
                pass
        return res
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
        client = _get_http_client()
        resp = await client.get(
            f"{HOME_SERVER_URL}/conversations",
            params={"limit": max(limit, 25)},
            headers={"X-Secret-Key": SECRET_KEY},
            timeout=10.0,
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


async def resume_conversation_remote(conversation_id: str, session_key: str = "default") -> dict:
    """Demande au home-runner de reprendre une conversation spécifique."""
    invalidate_conversations_cache()
    try:
        from core.node_hub import NODE_HUB
        NODE_HUB.rotate_conversation_token(session_key)
        await NODE_HUB.send_session_end()
    except Exception:
        pass
    if not HOME_SERVER_URL:
        return {"error": "HOME_SERVER_URL non configuré"}
    try:
        client = _get_http_client()
        resp = await client.post(
            f"{HOME_SERVER_URL}/conversation/resume",
            json={"conversation_id": conversation_id, "session_key": session_key},
            headers={"X-Secret-Key": SECRET_KEY},
            timeout=10.0,
        )
        resp.raise_for_status()
        res = resp.json()
        sess_obj = res.get("session") if isinstance(res, dict) else None
        if isinstance(sess_obj, dict) and sess_obj.get("session_token"):
            try:
                from core.node_hub import NODE_HUB
                NODE_HUB.set_conversation_token(session_key, str(sess_obj["session_token"]))
            except Exception:
                pass
        return res
    except Exception as e:
        logger.error("Erreur resume_conversation_remote: %s", e)
        return {"error": str(e)}


async def delete_conversation_remote(conversation_id: str) -> dict:
    """Demande au home-runner de supprimer une conversation passée."""
    invalidate_conversations_cache()
    if not HOME_SERVER_URL:
        return {"success": False, "error": "HOME_SERVER_URL non configuré"}
    try:
        client = _get_http_client()
        resp = await client.delete(
            f"{HOME_SERVER_URL}/conversation/{conversation_id}",
            headers={"X-Secret-Key": SECRET_KEY},
            timeout=10.0,
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


async def _memory_request(
    method: str,
    path: str,
    json_body: Optional[dict] = None,
) -> dict:
    """Appelle l'API mémoire du home-runner et normalise les erreurs."""
    if not HOME_SERVER_URL:
        return {"success": False, "error": "HOME_SERVER_URL non configuré"}
    try:
        client = _get_http_client()
        resp = await client.request(
            method,
            f"{HOME_SERVER_URL}{path}",
            headers={"X-Secret-Key": SECRET_KEY},
            json=json_body,
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        detail = "Erreur mémoire"
        try:
            detail = exc.response.json().get("detail", detail)
        except Exception:
            pass
        return {"success": False, "error": detail}
    except Exception as exc:  # noqa: BLE001
        logger.error("Erreur API mémoire (%s %s): %s", method, path, exc)
        return {"success": False, "error": str(exc)}


async def fetch_memory() -> dict:
    """Récupère le profil et les souvenirs durables."""
    return await _memory_request("GET", "/memory")


async def add_memory_fact(text: str, tags: Optional[list[str]] = None) -> dict:
    """Ajoute un souvenir durable."""
    return await _memory_request(
        "POST", "/memory/facts", {"text": text, "tags": tags or []}
    )


async def delete_memory_fact(fact_id: str) -> dict:
    """Supprime un souvenir durable par son identifiant."""
    return await _memory_request("DELETE", f"/memory/facts/{fact_id}")


async def clear_memory_facts() -> dict:
    """Supprime tous les souvenirs durables."""
    return await _memory_request("DELETE", "/memory/facts")


async def set_memory_profile(key: str, value: str) -> dict:
    """Définit une préférence durable."""
    return await _memory_request(
        "POST", "/memory/profile", {"key": key, "value": value}
    )


async def delete_memory_profile(key: str) -> dict:
    """Supprime une préférence durable."""
    return await _memory_request("DELETE", f"/memory/profile/{key}")


# ── Météo directe 0-token (Open-Meteo + cache 5 min) ──────────────────────────

_WEATHER_CACHE: dict[str, tuple[float, str]] = {}
_WEATHER_CACHE_TTL: float = 300.0

_KNOWN_CITIES: dict[str, tuple[float, float, str, str]] = {
    "paris": (48.8566, 2.3522, "Paris", "France"),
    "toulouse": (43.6047, 1.4442, "Toulouse", "France"),
    "lyon": (45.7640, 4.8357, "Lyon", "France"),
    "marseille": (43.2965, 5.3698, "Marseille", "France"),
    "bordeaux": (44.8378, -0.5792, "Bordeaux", "France"),
    "lille": (50.6292, 3.0573, "Lille", "France"),
    "nantes": (47.2184, -1.5536, "Nantes", "France"),
    "nice": (43.7102, 7.2620, "Nice", "France"),
    "strasbourg": (48.5734, 7.7521, "Strasbourg", "France"),
    "montpellier": (43.6108, 3.8767, "Montpellier", "France"),
    "rennes": (48.1173, -1.6778, "Rennes", "France"),
    "grenoble": (45.1885, 5.7245, "Grenoble", "France"),
    "tokyo": (35.6762, 139.6503, "Tokyo", "Japon"),
    "london": (51.5074, -0.1278, "Londres", "Royaume-Uni"),
    "londres": (51.5074, -0.1278, "Londres", "Royaume-Uni"),
    "new york": (40.7128, -74.0060, "New York", "États-Unis"),
}

_WMO_CODES: dict[int, tuple[str, str]] = {
    0: ("☀️", "Ciel dégagé"),
    1: ("🌤️", "Principalement dégagé"),
    2: ("⛅", "Partiellement nuageux"),
    3: ("☁️", "Couvert"),
    45: ("🌫️", "Brouillard"),
    48: ("🌫️", "Brouillard givrant"),
    51: ("🌦️", "Bruine légère"),
    53: ("🌦️", "Bruine modérée"),
    55: ("🌧️", "Bruine dense"),
    56: ("🌧️", "Bruine verglaçante légère"),
    57: ("🌧️", "Bruine verglaçante dense"),
    61: ("🌦️", "Pluie faible"),
    63: ("🌧️", "Pluie modérée"),
    65: ("🌧️", "Pluie forte"),
    66: ("🌨️", "Pluie verglaçante légère"),
    67: ("🌨️", "Pluie verglaçante forte"),
    71: ("🌨️", "Chute de neige faible"),
    73: ("❄️", "Chute de neige modérée"),
    75: ("❄️", "Chute de neige forte"),
    77: ("❄️", "Grains de neige"),
    80: ("🌦️", "Averses de pluie faibles"),
    81: ("🌧️", "Averses de pluie modérées"),
    82: ("⛈️", "Averses de pluie violentes"),
    85: ("🌨️", "Averses de neige faibles"),
    86: ("❄️", "Averses de neige fortes"),
    95: ("⛈️", "Orage"),
    96: ("⛈️", "Orage avec grêle faible"),
    99: ("⛈️", "Orage avec forte grêle"),
}


def _weather_advice(temp: float, precip: float, wind: float, code: int) -> str:
    tips: list[str] = []
    if code >= 51 or precip > 0.2:
        tips.append("prenez un parapluie ☂️")
    elif code in (0, 1) and temp >= 22:
        tips.append("lunettes de soleil conseillées 😎")
    if temp < 6:
        tips.append("couvrez-vous bien (manteau chaud) 🧥")
    elif temp < 14:
        tips.append("prévoyez une veste ou un pull 🧥")
    elif temp >= 28:
        tips.append("hydratez-vous régulièrement 💧")
    if wind >= 45:
        tips.append("attention aux rafales de vent 💨")
    if not tips:
        return "conditions agréables pour sortir 👍"
    return ", ".join(tips).capitalize() + "."


async def fetch_weather_direct(city: str) -> Optional[str]:
    """Récupère la météo Open-Meteo en direct (0 token LLM, ~80ms, cache 5 min).

    Retourne None en cas d'erreur ou de ville introuvable pour laisser le
    fallback LLM prendre le relais.
    """
    clean_city = (city or "").strip()
    if not clean_city:
        return None

    key = clean_city.lower()
    now = time.time()
    cached = _WEATHER_CACHE.get(key)
    if cached and now < cached[0]:
        return cached[1]

    client = _get_http_client()
    try:
        if key in _KNOWN_CITIES:
            lat, lon, display_name, country = _KNOWN_CITIES[key]
        else:
            geo_resp = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": clean_city, "count": 1, "language": "fr", "format": "json"},
                timeout=5.0,
            )
            geo_resp.raise_for_status()
            results = (geo_resp.json() or {}).get("results") or []
            if not results:
                return None
            top = results[0]
            lat = float(top["latitude"])
            lon = float(top["longitude"])
            display_name = top.get("name") or clean_city
            country = top.get("country") or ""
            _KNOWN_CITIES[key] = (lat, lon, display_name, country)

        wx_resp = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto",
                "forecast_days": 2,
            },
            timeout=5.0,
        )
        wx_resp.raise_for_status()
        wx = wx_resp.json() or {}
        cur = wx.get("current") or {}
        daily = wx.get("daily") or {}

        temp = float(cur.get("temperature_2m", 0.0))
        feels = float(cur.get("apparent_temperature", temp))
        hum = int(cur.get("relative_humidity_2m", 0))
        precip = float(cur.get("precipitation", 0.0))
        wind = float(cur.get("wind_speed_10m", 0.0))
        code = int(cur.get("weather_code", 0))
        emoji, desc = _WMO_CODES.get(code, ("🌤️", "Conditions variables"))

        loc_label = f"{display_name} ({country})" if country else display_name
        lines = [
            f"{emoji} **Météo à {loc_label}**",
            f"• **Actuellement :** `{temp:.1f}°C` (ressenti `{feels:.1f}°C`) — {desc}",
            f"• **Humidité :** `{hum}%` | **Vent :** `{wind:.0f} km/h` | **Précipitations :** `{precip:.1f} mm`",
        ]

        t_max_list = daily.get("temperature_2m_max") or []
        t_min_list = daily.get("temperature_2m_min") or []
        p_prob_list = daily.get("precipitation_probability_max") or []
        d_codes = daily.get("weather_code") or []

        if t_max_list and t_min_list:
            p0 = f" (pluie `{p_prob_list[0]}%`)" if p_prob_list and p_prob_list[0] is not None else ""
            lines.append(
                f"• **Aujourd'hui :** min `{t_min_list[0]:.1f}°C` / max `{t_max_list[0]:.1f}°C`{p0}"
            )
        if len(t_max_list) > 1 and len(t_min_list) > 1:
            c1 = int(d_codes[1]) if len(d_codes) > 1 and d_codes[1] is not None else 0
            e1, d1 = _WMO_CODES.get(c1, ("🌤️", ""))
            p1 = f", pluie `{p_prob_list[1]}%`" if len(p_prob_list) > 1 and p_prob_list[1] is not None else ""
            lines.append(
                f"• **Demain :** {e1} {d1} — min `{t_min_list[1]:.1f}°C` / max `{t_max_list[1]:.1f}°C`{p1}"
            )

        lines.append(f"💡 **Conseil :** {_weather_advice(temp, precip, wind, code)}")
        report = "\n".join(lines)
        _WEATHER_CACHE[key] = (now + _WEATHER_CACHE_TTL, report)
        return report
    except Exception as exc:
        logger.debug("Météo directe indisponible pour %r (%s), repli sur runner.", clean_city, exc)
        return None
