#!/usr/bin/env python3
"""Google Tasks MCP Server pour Antigravity.

Permet à l'assistant de consulter, créer, marquer comme terminées
et supprimer des tâches Google Tasks.
"""

import json
import logging
import sys
from pathlib import Path
from typing import Optional

import httpx
from mcp.server.mcpserver import MCPServer

logger = logging.getLogger("google-tasks-mcp")
TOKEN_FILE = Path(__file__).resolve().parent / "token.json"
TASKS_API_BASE = "https://tasks.googleapis.com/tasks/v1"

server = MCPServer(
    name="google-tasks",
    instructions="Serveur MCP pour la gestion de tâches Google Tasks (to-do lists).",
)


def _get_access_token() -> str:
    """Récupère un access_token valide depuis token.json ou le rafraîchit si besoin."""
    if not TOKEN_FILE.exists():
        raise RuntimeError(f"Fichier de jeton introuvable : {TOKEN_FILE}. Veuillez lancer setup_oauth.py.")

    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    client_id = data.get("client_id")
    client_secret = data.get("client_secret")
    refresh_token = data.get("refresh_token")

    if not refresh_token:
        raise RuntimeError("Aucun refresh_token trouvé dans token.json")

    resp = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Échec du rafraîchissement du token : HTTP {resp.status_code} ({resp.text})")

    token_data = resp.json()
    return token_data["access_token"]


def _get_headers() -> dict[str, str]:
    token = _get_access_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _format_error(resp: httpx.Response) -> str:
    """Formate un message d'erreur clair et actionnable selon le retour de Google."""
    if resp.status_code == 403:
        try:
            err_data = resp.json().get("error", {})
            msg = err_data.get("message", "")
            if "has not been used in project" in msg or "is disabled" in msg:
                return (
                    "Google Tasks non accessible : l'API Google Tasks n'est pas encore activée dans votre console Google Cloud. "
                    "Cliquez sur 'Activer' ici : https://console.developers.google.com/apis/api/tasks.googleapis.com/overview?project=516108585524"
                )
        except Exception:
            pass
        return "Google Tasks non accessible : permissions insuffisantes ou API désactivée (HTTP 403)."
    return f"Erreur Google Tasks : HTTP {resp.status_code} ({resp.text})"


@server.tool()
async def tasks_list_tasklists() -> str:
    """Liste toutes les listes de tâches de l'utilisateur."""
    headers = _get_headers()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{TASKS_API_BASE}/users/@me/lists", headers=headers)
        if resp.status_code != 200:
            return _format_error(resp)

        data = resp.json()
        items = data.get("items", [])
        if not items:
            return "Aucune liste de tâches trouvée."

        lines = ["📋 **Vos listes de tâches Google :**"]
        for it in items:
            lines.append(f"• **{it.get('title')}** (ID: `{it.get('id')}`)")
        return "\n".join(lines)


@server.tool()
async def tasks_list_tasks(
    tasklist_id: str = "@default",
    show_completed: bool = False,
    max_results: int = 20,
) -> str:
    """Liste les tâches d'une liste de tâches Google Tasks.

    Args:
        tasklist_id: ID de la liste (utiliser '@default' pour la liste principale).
        show_completed: Si True, inclut les tâches déjà accomplies.
        max_results: Nombre maximal de tâches à renvoyer.
    """
    headers = _get_headers()
    params = {
        "maxResults": max_results,
        "showCompleted": str(show_completed).lower(),
        "showHidden": str(show_completed).lower(),
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{TASKS_API_BASE}/lists/{tasklist_id}/tasks", headers=headers, params=params)
        if resp.status_code != 200:
            return _format_error(resp)

        items = resp.json().get("items", [])
        if not items:
            return "Aucune tâche trouvée dans cette liste."

        lines = [f"📋 **Tâches ({len(items)}) :**"]
        for t in items:
            status = "✅" if t.get("status") == "completed" else "⬜"
            title = t.get("title", "(Sans titre)")
            due = f" (Échéance: {t.get('due')[:10]})" if t.get("due") else ""
            notes = f" — *{t.get('notes')}*" if t.get("notes") else ""
            lines.append(f"{status} **{title}**{due}{notes} `[ID: {t.get('id')}]`")
        return "\n".join(lines)


@server.tool()
async def tasks_create_task(
    title: str,
    notes: Optional[str] = None,
    due_date: Optional[str] = None,
    tasklist_id: str = "@default",
) -> str:
    """Crée une nouvelle tâche dans Google Tasks.

    Args:
        title: Titre de la tâche (ex: "Acheter du pain", "Réviser cours EPITA").
        notes: Description ou notes complémentaires.
        due_date: Date d'échéance au format ISO 8601 (ex: "2026-10-20T00:00:00.000Z" ou "2026-10-20").
        tasklist_id: ID de la liste cible ('@default' pour la liste principale).
    """
    headers = _get_headers()
    body = {"title": title}
    if notes:
        body["notes"] = notes
    if due_date:
        if "T" not in due_date:
            due_date = f"{due_date}T00:00:00.000Z"
        body["due"] = due_date

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(f"{TASKS_API_BASE}/lists/{tasklist_id}/tasks", headers=headers, json=body)
        if resp.status_code in (200, 201):
            created = resp.json()
            return f"✅ Tâche créée avec succès : **{created.get('title')}** (ID: `{created.get('id')}`)"
        return _format_error(resp)


@server.tool()
async def tasks_complete_task(task_id: str, tasklist_id: str = "@default") -> str:
    """Marque une tâche comme terminée dans Google Tasks.

    Args:
        task_id: ID de la tâche à cocher/valider.
        tasklist_id: ID de la liste contenant la tâche.
    """
    headers = _get_headers()
    body = {"status": "completed"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.patch(f"{TASKS_API_BASE}/lists/{tasklist_id}/tasks/{task_id}", headers=headers, json=body)
        if resp.status_code == 200:
            return f"✅ Tâche `{task_id}` marquée comme terminée !"
        return _format_error(resp)


@server.tool()
async def tasks_delete_task(task_id: str, tasklist_id: str = "@default") -> str:
    """Supprime une tâche de Google Tasks.

    Args:
        task_id: ID de la tâche à supprimer.
        tasklist_id: ID de la liste.
    """
    headers = _get_headers()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.delete(f"{TASKS_API_BASE}/lists/{tasklist_id}/tasks/{task_id}", headers=headers)
        if resp.status_code in (200, 204):
            return f"🗑️ Tâche `{task_id}` supprimée."
        return _format_error(resp)


if __name__ == "__main__":
    server.run(transport="stdio")
