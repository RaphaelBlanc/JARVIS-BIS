"""Moodle Client for JARVIS.

Handles retrieval of upcoming assignments, deadlines and enrolled courses via
Moodle REST Web Services API or AJAX session-authenticated API.
"""

from datetime import datetime
import html
import logging
import os
import re
from typing import Any
from dotenv import load_dotenv
import httpx

load_dotenv()

MOODLE_URL = os.getenv("MOODLE_URL", "").rstrip("/")
MOODLE_API_KEY = os.getenv("MOODLE_API_KEY", "").strip()


def is_moodle_configured() -> bool:
  """Checks if Moodle URL and API key or session cookie are set."""
  return bool(MOODLE_URL and MOODLE_API_KEY)


def is_session_cookie(key: str) -> bool:
  """Checks if the key appears to be a MoodleSession cookie rather than a 32-hex wstoken."""
  if len(key) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", key):
    return False
  return True


async def call_moodle_ajax(wsfunction: str, params: dict | None = None) -> Any:
  """Calls Moodle AJAX service.php using MoodleSession cookie."""
  if not is_moodle_configured():
    return None

  moodle_url = MOODLE_URL if MOODLE_URL.startswith(("http://", "https://")) else f"https://{MOODLE_URL}"
  host = httpx.URL(moodle_url).host
  jar = httpx.Cookies()
  jar.set("MoodleSession", MOODLE_API_KEY, domain=host)

  async with httpx.AsyncClient(
      timeout=15.0,
      cookies=jar,
      follow_redirects=True,
  ) as client:
    # 1. Retrieve sesskey from dashboard
    try:
      resp = await client.get(f"{moodle_url}/my/")
      if resp.status_code != 200:
        raise ValueError(f"Impossible de joindre Moodle (code {resp.status_code}).")
    except httpx.HTTPError:
      raise RuntimeError("Erreur réseau lors de la communication avec Moodle.") from None

    m = re.search(r'"sesskey":"([^"]+)"', resp.text)
    if not m:
      raise ValueError("Session Moodle expirée ou invalide. Veuillez renouveler votre cookie MoodleSession dans .env.")
    sesskey = m.group(1)

    # 2. Call AJAX endpoint
    endpoint = f"{moodle_url}/lib/ajax/service.php"
    query_params = {"sesskey": sesskey, "info": wsfunction}
    payload = [{
        "index": 0,
        "methodname": wsfunction,
        "args": params or {},
    }]
    try:
      r = await client.post(endpoint, params=query_params, json=payload)
      r.raise_for_status()
    except httpx.HTTPStatusError as e:
      # Ne jamais exposer l'URL avec sesskey dans le message d'erreur
      raise RuntimeError(f"Erreur HTTP {e.response.status_code} sur le service Moodle AJAX.") from None
    except httpx.RequestError:
      raise RuntimeError("Erreur de connexion au service Moodle AJAX.") from None

    try:
      data = r.json()
    except Exception:
      raise RuntimeError("Réponse Moodle AJAX invalide.") from None

    if isinstance(data, list) and len(data) > 0:
      if data[0].get("error"):
        exc = data[0].get("exception", {})
        err_msg = exc.get("message") or exc.get("errorcode") or "Erreur API Moodle AJAX"
        err_msg = err_msg.replace(sesskey, "[masqué]")
        raise RuntimeError(err_msg)
      return data[0].get("data")
    return data


async def call_moodle_ws(wsfunction: str, params: dict | None = None) -> Any:
  """Calls a Moodle WebService REST function, or falls back to AJAX session if cookie is used."""
  if not is_moodle_configured():
    return None

  if is_session_cookie(MOODLE_API_KEY):
    return await call_moodle_ajax(wsfunction, params)

  moodle_url = MOODLE_URL if MOODLE_URL.startswith(("http://", "https://")) else f"https://{MOODLE_URL}"
  endpoint = f"{moodle_url}/webservice/rest/server.php"
  data = {
      "wstoken": MOODLE_API_KEY,
      "wsfunction": wsfunction,
      "moodlewsrestformat": "json",
  }
  if params:
    data.update(params)

  async with httpx.AsyncClient(timeout=15.0) as client:
    try:
      response = await client.post(endpoint, data=data)
      response.raise_for_status()
      res = response.json()
    except httpx.HTTPStatusError as e:
      raise RuntimeError(f"Erreur HTTP {e.response.status_code} sur Moodle REST.") from None
    except httpx.RequestError:
      raise RuntimeError("Erreur réseau lors de la requête Moodle REST.") from None

    if isinstance(res, dict) and res.get("errorcode") == "invalidtoken":
      return await call_moodle_ajax(wsfunction, params)
    return res


async def get_upcoming_assignments() -> dict[str, Any]:
  """Retrieves upcoming assignments and deadlines from Moodle.

  Returns a dictionary with status, assignments list, and formatted message.
  """
  if not is_moodle_configured():
    msg = (
        "⚠️ <b>Moodle non configuré.</b>\n\n"
        "Pour synchroniser vos devoirs et TPs :\n"
        "1. Connectez-vous sur votre espace Moodle\n"
        "2. Renseignez votre token ou cookie <code>MoodleSession</code> dans <code>.env</code> sous <code>MOODLE_API_KEY=...</code>\n\n"
        "Dès qu'il est renseigné, JARVIS affichera automatiquement vos devoirs et rappels d'échéances !"
    )
    return {
        "success": False,
        "message": msg,
        "output": "⚠️ Moodle non configuré (MOODLE_URL ou MOODLE_API_KEY manquant).",
        "assignments": [],
    }

  try:
    now_ts = int(datetime.now().timestamp())
    # 1. First attempt: calendar action events (best for upcoming deadlines)
    events_data = await call_moodle_ws(
        "core_calendar_get_action_events_by_timesort",
        {"timesortfrom": now_ts, "limitnum": 20},
    )

    events_list = []
    if isinstance(events_data, dict) and "events" in events_data:
      for ev in events_data.get("events", []):
        timesort = ev.get("timesort", 0)
        # Ne garder strictement que les devoirs futurs
        if timesort < now_ts:
          continue
        name = ev.get("name", "Devoir")
        course_name = ev.get("course", {}).get("fullname", "Cours")
        due_dt = datetime.fromtimestamp(timesort)
        url = ev.get("url", f"{MOODLE_URL}/calendar/view.php")
        events_list.append({
            "title": name,
            "course": course_name,
            "due_date": due_dt.strftime("%d/%m/%Y à %H:%M"),
            "timestamp": timesort,
            "url": url,
        })

    # 2. If no calendar events or alternative: query mod_assign_get_assignments
    if not events_list:
      assign_data = await call_moodle_ws("mod_assign_get_assignments")
      if isinstance(assign_data, dict) and "courses" in assign_data:
        for course in assign_data.get("courses", []):
          c_name = course.get("fullname", "Cours")
          for assign in course.get("assignments", []):
            duedate = assign.get("duedate", 0)
            if duedate > now_ts:
              due_dt = datetime.fromtimestamp(duedate)
              cmid = assign.get("cmid")
              url = (
                  f"{MOODLE_URL}/mod/assign/view.php?id={cmid}"
                  if cmid
                  else MOODLE_URL
              )
              events_list.append({
                  "title": assign.get("name", "Devoir"),
                  "course": c_name,
                  "due_date": due_dt.strftime("%d/%m/%Y à %H:%M"),
                  "timestamp": duedate,
                  "url": url,
              })

    # Sort chronologically
    events_list.sort(key=lambda x: x["timestamp"])

    if not events_list:
      empty_msg = "🎉 <b>Aucun devoir ou TP en attente sur Moodle !</b>\nTout est à jour."
      return {
          "success": True,
          "message": empty_msg,
          "output": "Aucun devoir ou TP en attente sur Moodle. Tout est à jour.",
          "assignments": [],
      }

    lines = ["🎓 <b>Devoirs & Échéances Moodle</b>", ""]
    for item in events_list[:8]:
      diff_hours = (item["timestamp"] - now_ts) / 3600
      urgency = (
          "🚨 "
          if diff_hours < 24
          else ("⚠️ " if diff_hours < 72 else "📌 ")
      )
      clean_title = html.escape(str(item["title"]))
      clean_course = html.escape(str(item["course"]))
      safe_url = html.escape(str(item["url"]), quote=True)
      lines.append(
          f"{urgency}<b><a href=\"{safe_url}\">{clean_title}</a></b>"
      )
      lines.append(f"   📚 <i>{clean_course}</i>")
      lines.append(f"   ⏳ Échéance : <b>{item['due_date']}</b>\n")

    msg = "\n".join(lines).strip()
    return {
        "success": True,
        "message": msg,
        "output": msg,
        "assignments": events_list,
    }

  except Exception as e:
    logging.error(f"Error fetching Moodle assignments: {e}", exc_info=True)
    clean_err = str(e)
    if "sesskey" in clean_err.lower():
      clean_err = "Erreur de session Moodle."
    err_msg = (
        "⚠️ <b>Erreur de connexion à Moodle :</b>\n"
        f"<code>{html.escape(clean_err)}</code>\nVérifiez votre clé API ou cookie MoodleSession dans .env."
    )
    return {
        "success": False,
        "message": err_msg,
        "output": err_msg,
        "assignments": [],
    }


async def get_enrolled_courses() -> dict[str, Any]:
  """Retrieves list of enrolled courses from Moodle."""
  if not is_moodle_configured():
    msg = (
        "⚠️ <b>Moodle non configuré.</b>\n"
        "Ajoutez votre cookie <code>MoodleSession</code> ou clé dans <code>.env</code> sous <code>MOODLE_API_KEY=...</code>"
    )
    return {
        "success": False,
        "message": msg,
        "output": "⚠️ Moodle non configuré (MOODLE_URL ou MOODLE_API_KEY manquant).",
        "courses": [],
    }

  try:
    data = await call_moodle_ws(
        "core_course_get_enrolled_courses_by_timeline_classification",
        {"classification": "all", "limit": 30, "offset": 0, "sort": "fullname"},
    )
    courses = []
    if isinstance(data, dict) and "courses" in data:
      courses = data["courses"]
    elif isinstance(data, list):
      courses = data

    if not courses:
      empty_msg = "📚 <b>Aucun cours trouvé sur Moodle !</b>"
      return {
          "success": True,
          "message": empty_msg,
          "output": "Aucun cours trouvé sur Moodle.",
          "courses": [],
      }

    lines = [f"📚 <b>Mes Cours Moodle ({len(courses)})</b>", ""]
    for c in courses[:15]:
      fullname = html.escape(str(c.get("fullname", "Cours")))
      cid = c.get("id")
      raw_url = f"{MOODLE_URL}/course/view.php?id={cid}" if cid else MOODLE_URL
      safe_url = html.escape(raw_url, quote=True)
      lines.append(f"• <b><a href=\"{safe_url}\">{fullname}</a></b>")

    if len(courses) > 15:
      lines.append(f"\n<i>... et {len(courses) - 15} autres cours.</i>")

    msg = "\n".join(lines).strip()
    return {
        "success": True,
        "message": msg,
        "output": msg,
        "courses": courses,
    }

  except Exception as e:
    logging.error(f"Error fetching Moodle courses: {e}", exc_info=True)
    clean_err = str(e)
    if "sesskey" in clean_err.lower():
      clean_err = "Erreur de session Moodle."
    err_msg = (
        "⚠️ <b>Erreur de connexion à Moodle :</b>\n"
        f"<code>{html.escape(clean_err)}</code>"
    )
    return {
        "success": False,
        "message": err_msg,
        "output": err_msg,
        "courses": [],
    }
