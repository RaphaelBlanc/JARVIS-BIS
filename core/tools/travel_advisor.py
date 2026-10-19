"""On-demand travel and departure advisor for JARVIS (/depart).

Calculates transit duration to events or destinations and schedules an alert.
Zero token cost using local geolocation and standard transit estimation.
"""

from datetime import datetime
import json
import logging
from pathlib import Path
import re
import time

DESTINATIONS_PRESETS = {
    "epita": {"name": "EPITA (Kremlin-Bicêtre / Villejuif)", "default_mins": 35},
    "kb": {"name": "EPITA Kremlin-Bicêtre", "default_mins": 35},
    "villejuif": {"name": "EPITA Villejuif", "default_mins": 40},
    "paris": {"name": "Paris Centre", "default_mins": 25},
    "gare de lyon": {"name": "Gare de Lyon", "default_mins": 30},
    "montparnasse": {"name": "Gare Montparnasse", "default_mins": 35},
    "chatelet": {"name": "Châtelet - Les Halles", "default_mins": 25},
}


def parse_depart_request(text: str) -> dict | None:
    """Parses departure request.

    Examples:
    - /depart EPITA à 14h
    - /depart EPITA pour 8h30
    - /depart Gare de Lyon à 17h15
    - /depart chez Pierre à 20h
    """
    clean = re.sub(r"^/depart(?:@[a-zA-Z0-9_]+)?\s*", "", text, flags=re.IGNORECASE).strip()
    if not clean:
        return None

    # Pattern: <destination> [à|pour] <HH>[:<MM>]
    m = re.match(
        r"^(.+?)\s+(?:[àa]|pour)\s+(\d{1,2})(?:h(\d{1,2})?|:(\d{1,2}))?\s*(?:min)?$",
        clean,
        re.IGNORECASE,
    )
    if not m:
        return {"destination": clean, "target_time": None}

    dest = m.group(1).strip()
    hour = int(m.group(2))
    minute = int(m.group(3) or m.group(4) or 0)

    now = datetime.now()
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target_dt.timestamp() <= time.time():
            # If time has passed today, assume tomorrow
            import datetime as dt_module
            target_dt += dt_module.timedelta(days=1)
        return {
            "destination": dest,
            "target_hour": hour,
            "target_minute": minute,
            "target_dt": target_dt,
            "target_timestamp": target_dt.timestamp(),
        }

    return {"destination": clean, "target_time": None}


def calculate_departure_plan(parsed: dict) -> dict:
    dest_raw = parsed.get("destination", "").lower()
    matched_preset = None
    for k, v in DESTINATIONS_PRESETS.items():
        if k in dest_raw:
            matched_preset = v
            break

    transit_mins = matched_preset["default_mins"] if matched_preset else 35
    dest_name = matched_preset["name"] if matched_preset else parsed.get("destination", "Destination")
    margin_mins = 10  # 10 min security buffer

    target_ts = parsed.get("target_timestamp")
    now_ts = time.time()

    if target_ts:
        total_lead_time_secs = (transit_mins + margin_mins) * 60
        depart_ts = target_ts - total_lead_time_secs
        depart_dt = datetime.fromtimestamp(depart_ts)
        mins_until_depart = int((depart_ts - now_ts) // 60)

        target_time_str = f"{parsed.get('target_hour'):02d}h{parsed.get('target_minute'):02d}"
        depart_time_str = depart_dt.strftime("%H:%M")

        lines = [
            f"🧭 <b>Conseiller de Trajet & Départ : {dest_name}</b>",
            f"• <b>Arrivée souhaitée :</b> <code>{target_time_str}</code>",
            f"• <b>Temps de trajet estimé :</b> ~{transit_mins} min (+{margin_mins} min de marge)",
            f"• <b>Heure de départ conseillée :</b> <b>{depart_time_str}</b>\n",
        ]

        if mins_until_depart > 0:
            lines.append(f"⏰ <b>Vous devez partir dans environ {mins_until_depart} minutes.</b>")
            reminder_time = depart_ts
        elif mins_until_depart == 0:
            lines.append("🚨 <b>C'est l'heure ! Vous devriez partir maintenant.</b>")
            reminder_time = None
        else:
            lines.append(f"⚠️ <b>Attention : départ théorique dépassé depuis {-mins_until_depart} min !</b>")
            reminder_time = None

        return {
            "destination": dest_name,
            "transit_mins": transit_mins,
            "depart_ts": depart_ts,
            "reminder_ts": reminder_time,
            "message": "\n".join(lines),
        }
    else:
        return {
            "destination": dest_name,
            "transit_mins": transit_mins,
            "depart_ts": None,
            "reminder_ts": None,
            "message": (
                f"🧭 <b>Trajet vers {dest_name}</b>\n\n"
                f"• Temps de trajet estimé : <b>~{transit_mins} minutes</b>\n\n"
                "💡 <i>Pour programmer une heure d'arrivée et un rappel de départ :</i>\n"
                f"<code>/depart {dest_name} à 14h30</code>"
            ),
        }
