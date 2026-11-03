"""Moodle EPITA Client for JARVIS.

Handles retrieval of upcoming assignments, deadlines and announcements via
Moodle REST Web Services API.
"""

from datetime import datetime
import logging
import os
import re
from typing import Any
from dotenv import load_dotenv
import httpx

load_dotenv()

MOODLE_URL = os.getenv("MOODLE_URL", "https://moodle.epita.fr/").rstrip("/")
MOODLE_API_KEY = os.getenv("MOODLE_API_KEY", "").strip()


def is_moodle_configured() -> bool:
  """Checks if Moodle API key is set."""
  return bool(MOODLE_API_KEY)


def is_session_cookie(key: str) -> bool:
  """Checks if the key appears to be a MoodleSession cookie rather than a 32-hex wstoken."""
  if len(key) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", key):
    return False
  return True


async def call_moodle_ajax(wsfunction: str, params: dict | None = None) -> Any:
  """Calls Moodle AJAX service.php using MoodleSession cookie."""
  if not MOODLE_API_KEY:
    return None

  async with httpx.AsyncClient(
      timeout=15.0,
      headers={"Cookie": f"MoodleSession={MOODLE_API_KEY}"},
      follow_redirects=True,
  ) as client:
    # 1. Retrieve sesskey from dashboard
    resp = await client.get(f"{MOODLE_URL}/my/")
    if resp.status_code != 200:
      raise ValueError(f"Impossible de joindre Moodle EPITA (code {resp.status_code}).")
    m = re.search(r'"sesskey":"([^"]+)"', resp.text)
    if not m:
      raise ValueError("Session Moodle expirée ou invalide. Veuillez renouveler votre cookie MoodleSession dans .env.")
    sesskey = m.group(1)

    # 2. Call AJAX endpoint
    endpoint = f"{MOODLE_URL}/lib/ajax/service.php?sesskey={sesskey}&info={wsfunction}"
    payload = [{
        "index": 0,
        "methodname": wsfunction,
        "args": params or {},
    }]
    r = await client.post(endpoint, json=payload)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list) and len(data) > 0:
      if data[0].get("error"):
        exc = data[0].get("exception", {})
        raise RuntimeError(exc.get("message") or exc.get("errorcode") or "Erreur API Moodle AJAX")
      return data[0].get("data")
    return data


async def call_moodle_ws(wsfunction: str, params: dict | None = None) -> Any:
  """Calls a Moodle WebService REST function, or falls back to AJAX session if cookie is used."""
  if not MOODLE_API_KEY:
    return None

  if is_session_cookie(MOODLE_API_KEY):
    return await call_moodle_ajax(wsfunction, params)

  endpoint = f"{MOODLE_URL}/webservice/rest/server.php"
  data = {
      "wstoken": MOODLE_API_KEY,
      "wsfunction": wsfunction,
      "moodlewsrestformat": "json",
  }
  if params:
    data.update(params)

  async with httpx.AsyncClient(timeout=15.0) as client:
    response = await client.post(endpoint, data=data)
    response.raise_for_status()
    res = response.json()
    if isinstance(res, dict) and res.get("errorcode") == "invalidtoken":
      return await call_moodle_ajax(wsfunction, params)
    return res


async def get_upcoming_assignments() -> dict[str, Any]:
  """Retrieves upcoming assignments and deadlines from Moodle EPITA.

  Returns a dictionary with status, assignments list, and formatted message.
  """
  if not is_moodle_configured():
    return {
        "success": False,
        "message": (
            "⚠️ <b>Clé API Moodle non configurée.</b>\n\n"
            "Pour synchroniser vos devoirs et TPs EPITA :\n"
            "1. Connectez-vous sur <a"
            ' href="https://moodle.epita.fr/">moodle.epita.fr</a>\n'
            "2. Allez dans <b>Préférences > Clés de sécurité</b> (Web Services"
            " token)\n"
            "3. Ajoutez la clé dans <code>Documents/JARVIS/.env</code> sous"
            " <code>MOODLE_API_KEY=...</code>\n\n"
            "Dès qu'elle est renseignée, JARVIS affichera automatiquement vos"
            " devoirs et rappels d'échéances !"
        ),
        "assignments": [],
    }

  try:
    # 1. First attempt: calendar action events (best for upcoming deadlines)
    events_data = await call_moodle_ws(
        "core_calendar_get_action_events_by_timesort",
        {"timesortfrom": int(datetime.now().timestamp()), "limitnum": 20},
    )
    if not events_data or not events_data.get("events"):
      events_data = await call_moodle_ws(
          "core_calendar_get_action_events_by_timesort",
          {"timesortfrom": 0, "limitnum": 20},
      )

    events_list = []
    if isinstance(events_data, dict) and "events" in events_data:
      for ev in events_data.get("events", []):
        name = ev.get("name", "Devoir")
        course_name = ev.get("course", {}).get("fullname", "Cours EPITA")
        timesort = ev.get("timesort", 0)
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
      now_ts = datetime.now().timestamp()
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
      return {
          "success": True,
          "message": (
              "🎉 <b>Aucun devoir ou TP en attente sur Moodle EPITA !</b>\nTout"
              " est à jour."
          ),
          "assignments": [],
      }

    lines = ["🎓 <b>Devoirs & Échéances Moodle EPITA</b>", ""]
    now_ts = datetime.now().timestamp()
    for item in events_list[:8]:
      diff_hours = (item["timestamp"] - now_ts) / 3600
      urgency = (
          "🚨 "
          if diff_hours < 24
          else ("⚠️ " if diff_hours < 72 else "📌 ")
      )
      lines.append(
          f"{urgency}<b><a href=\"{item['url']}\">{item['title']}</a></b>"
      )
      lines.append(f"   📚 <i>{item['course']}</i>")
      lines.append(f"   ⏳ Échéance : <b>{item['due_date']}</b>\n")

    return {
        "success": True,
        "message": "\n".join(lines).strip(),
        "assignments": events_list,
    }

  except Exception as e:
    logging.error(f"Error fetching Moodle assignments: {e}", exc_info=True)
    return {
        "success": False,
        "message": (
            "⚠️ <b>Erreur de connexion à Moodle EPITA :</b>\n"
            f"<code>{str(e)}</code>\nVérifiez votre clé API ou cookie MoodleSession dans .env."
        ),
        "assignments": [],
    }


async def get_enrolled_courses() -> dict[str, Any]:
  """Retrieves list of enrolled courses from Moodle EPITA."""
  if not is_moodle_configured():
    return {
        "success": False,
        "message": (
            "⚠️ <b>Cookie ou clé API Moodle non configuré.</b>\n"
            "Ajoutez votre cookie <code>MoodleSession</code> dans <code>.env</code> sous <code>MOODLE_API_KEY=...</code>"
        ),
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
      return {
          "success": True,
          "message": "📚 <b>Aucun cours trouvé sur Moodle EPITA !</b>",
          "courses": [],
      }

    lines = [f"📚 <b>Mes Cours Moodle EPITA ({len(courses)})</b>", ""]
    for c in courses[:15]:
      fullname = c.get("fullname", "Cours")
      cid = c.get("id")
      url = f"{MOODLE_URL}/course/view.php?id={cid}" if cid else MOODLE_URL
      lines.append(f"• <b><a href=\"{url}\">{fullname}</a></b>")

    if len(courses) > 15:
      lines.append(f"\n<i>... et {len(courses) - 15} autres cours.</i>")

    return {
        "success": True,
        "message": "\n".join(lines).strip(),
        "courses": courses,
    }

  except Exception as e:
    logging.error(f"Error fetching Moodle courses: {e}", exc_info=True)
    return {
        "success": False,
        "message": (
            "⚠️ <b>Erreur de connexion à Moodle EPITA :</b>\n"
            f"<code>{str(e)}</code>"
        ),
        "courses": [],
    }
