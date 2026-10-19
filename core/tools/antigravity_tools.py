"""Antigravity CLI and session integration utilities for JARVIS.

Handles:
- Listing and resuming historical conversations from conversation_summaries.db
- Model discovery and dynamic model selection (Flash Low, Flash High, Argon, Claude...)
- Reasoning effort management (low, medium, high, xhigh, max)
- MCP server status checking via 'agy mcp list'
- Direct execution of agy CLI subcommands
"""

import asyncio
from datetime import datetime
import logging
from pathlib import Path
import sqlite3

SUMMARIES_DB = Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db"

AVAILABLE_MODELS = [
    {
        "id": "gemini-3.8-flash-low",
        "label": "⚡ Gemini 3.8 Flash (Low)",
        "desc": "Ultra-rapide, idéal requêtes quotidiennes",
    },
    {
        "id": "gemini-3.8-flash-high",
        "label": "🧠 Gemini 3.8 Flash (High)",
        "desc": "Réflexion poussée, TPs & maths",
    },
    {
        "id": "gemini-4-argon-low",
        "label": "🔬 Gemini 4 Argon (Low)",
        "desc": "Nouvelle génération, effort bas",
    },
    {
        "id": "gemini-4-argon-medium",
        "label": "🚀 Gemini 4 Argon (Medium)",
        "desc": "Puissant et polyvalent",
    },
    {
        "id": "gemini-4-argon-high",
        "label": "🎯 Gemini 4 Argon (High)",
        "desc": "Raisonnement maximal",
    },
    {
        "id": "claude-sonnet-5-5-medium",
        "label": "💡 Claude Sonnet 5.5 (Medium)",
        "desc": "Excellente rédaction et code",
    },
    {
        "id": "claude-opus-5-5-high",
        "label": "👑 Claude Opus 5.5 (High)",
        "desc": "Modèle de pointe complexe",
    },
]

AVAILABLE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def normalize_model_id(input_str: str) -> str | None:
  """Normalizes or resolves a user-entered model string to an exact model ID."""
  if not input_str:
    return None
  s = input_str.strip().lower()

  # 1. Exact match
  for m in AVAILABLE_MODELS:
    if m["id"].lower() == s:
      return m["id"]

  # 2. Friendly aliases
  aliases = {
      "flash": "gemini-3.8-flash-low",
      "flash-low": "gemini-3.8-flash-low",
      "flash-high": "gemini-3.8-flash-high",
      "argon": "gemini-4-argon-medium",
      "argon-low": "gemini-4-argon-low",
      "argon-medium": "gemini-4-argon-medium",
      "argon-high": "gemini-4-argon-high",
      "claude": "claude-sonnet-5-5-medium",
      "sonnet": "claude-sonnet-5-5-medium",
      "opus": "claude-opus-5-5-high",
  }
  if s in aliases:
    return aliases[s]

  # 3. Substring match
  for m in AVAILABLE_MODELS:
    if s in m["id"].lower():
      return m["id"]

  return None


def normalize_effort(input_str: str) -> str | None:
  """Normalizes or resolves an effort level."""
  if not input_str:
    return None
  s = input_str.strip().lower()
  if s in AVAILABLE_EFFORTS:
    return s
  alias_map = {
      "bas": "low",
      "faible": "low",
      "moyen": "medium",
      "haut": "high",
      "eleve": "high",
      "élevé": "high",
      "maximum": "max",
  }
  return alias_map.get(s)


def list_recent_conversations(limit: int = 6) -> list[dict]:
  """Retrieves recent conversation summaries from Antigravity SQLite DB."""
  if not SUMMARIES_DB.exists():
    return []

  try:
    conn = sqlite3.connect(SUMMARIES_DB)
    cur = conn.cursor()
    cur.execute(
        """
            SELECT conversation_id, title, step_count, last_modified_time
            FROM conversation_summaries
            ORDER BY last_modified_time DESC
            LIMIT ?
            """,
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()

    results = []
    for row in rows:
      cid, title, steps, mtime = row
      time_display = "N/A"
      if mtime:
        try:
          dt = datetime.fromisoformat(str(mtime).replace("Z", "+00:00"))
          time_display = dt.strftime("%d/%m à %H:%M")
        except Exception:
          time_display = str(mtime)[:16]

      clean_title = title.strip() if title else "Sans titre"
      results.append({
          "id": cid,
          "short_id": cid[:8],
          "title": clean_title,
          "steps": steps,
          "time": time_display,
      })
    return results
  except Exception as e:
    logging.warning(f"Failed to query conversation_summaries: {e}")
    return []


def get_conversation_info(conv_id: str) -> dict | None:
  """Retrieves metadata for a specific conversation ID."""
  if not SUMMARIES_DB.exists() or not conv_id:
    return None

  try:
    conn = sqlite3.connect(SUMMARIES_DB)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT conversation_id, title, step_count, last_modified_time
        FROM conversation_summaries
        WHERE conversation_id = ?
        LIMIT 1
        """,
        (conv_id,),
    )
    row = cur.fetchone()
    conn.close()

    if row:
      cid, title, steps, mtime = row
      time_display = "N/A"
      if mtime:
        try:
          dt = datetime.fromisoformat(str(mtime).replace("Z", "+00:00"))
          time_display = dt.strftime("%d/%m à %H:%M")
        except Exception:
          time_display = str(mtime)[:16]
      return {
          "id": cid,
          "short_id": cid[:8],
          "title": title.strip() if title else "Sans titre",
          "steps": steps,
          "time": time_display,
      }
    return None
  except Exception as e:
    logging.warning(f"Failed to query conversation info: {e}")
    return None


def get_conversation_by_id_or_prefix(query: str) -> dict | None:
  """Finds a conversation matching an index (1-based), 'last', an ID prefix, or title."""
  if not SUMMARIES_DB.exists() or not query:
    return None

  clean_query = query.strip()

  try:
    conn = sqlite3.connect(SUMMARIES_DB)
    cur = conn.cursor()

    # 1. Special keyword 'last' or 'latest'
    if clean_query.lower() in ("last", "latest", "recent"):
      cur.execute(
          """
          SELECT conversation_id, title, step_count, last_modified_time
          FROM conversation_summaries
          ORDER BY last_modified_time DESC
          LIMIT 1
          """
      )
      row = cur.fetchone()
    # 2. Numeric index (e.g. 1 to 50)
    elif clean_query.isdigit() and 1 <= int(clean_query) <= 50:
      offset = int(clean_query) - 1
      cur.execute(
          """
          SELECT conversation_id, title, step_count, last_modified_time
          FROM conversation_summaries
          ORDER BY last_modified_time DESC
          LIMIT 1 OFFSET ?
          """,
          (offset,),
      )
      row = cur.fetchone()
    # 3. Exact ID or prefix match
    else:
      cur.execute(
          """
          SELECT conversation_id, title, step_count, last_modified_time
          FROM conversation_summaries
          WHERE conversation_id LIKE ? OR title LIKE ?
          ORDER BY last_modified_time DESC
          LIMIT 1
          """,
          (f"{clean_query}%", f"%{clean_query}%"),
      )
      row = cur.fetchone()

    conn.close()

    if row:
      cid, title, steps, mtime = row
      time_display = "N/A"
      if mtime:
        try:
          dt = datetime.fromisoformat(str(mtime).replace("Z", "+00:00"))
          time_display = dt.strftime("%d/%m à %H:%M")
        except Exception:
          time_display = str(mtime)[:16]
      return {
          "id": cid,
          "short_id": cid[:8],
          "title": title.strip() if title else "Sans titre",
          "steps": steps,
          "time": time_display,
      }
    return None
  except Exception as e:
    logging.warning(f"Failed to find conversation: {e}")
    return None


async def run_agy_cli_subcommand(
    subcmd_args: list[str], timeout: int = 45
) -> tuple[int, str, str]:
  """Runs a raw agy subcommand like ['mcp', 'list'] or ['models'] and returns (code, stdout, stderr)."""
  try:
    cmd = ["agy"] + subcmd_args
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()
  except asyncio.TimeoutError:
    return -1, "", f"Temps d'exécution dépassé ({timeout}s)."
  except Exception as e:
    return -1, "", str(e)
