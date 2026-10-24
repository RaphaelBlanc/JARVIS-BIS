#!/usr/bin/env python3
"""nodes_mcp.py — Serveur MCP pour la supervision et le contrôle des nœuds clients (Tour, Laptop, etc.).

Expose à l'agent Antigravity des outils directs (MCP) pour :
- Vérifier l'état de connexion des machines (ex: savoir si la tour est allumée et en ligne)
- Créer ou écrire des fichiers sur une machine distante
- Lire un fichier distant
- Exécuter une commande distante
- Prendre une capture d'écran
"""

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

import httpx
from mcp.server.mcpserver import MCPServer

logger = logging.getLogger("jarvis-nodes-mcp")

server = MCPServer(
    name="jarvis-nodes",
    instructions="Serveur MCP pour superviser et interagir avec les machines clientes connectées à JARVIS (Tour, Laptop).",
)

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8000")


def _get_secret_key() -> str:
    key = os.getenv("SECRET_KEY", "").strip()
    if key:
        return key
    env_paths = [
        Path(__file__).resolve().parent.parent.parent / "gateway" / ".env",
        Path("/home/pi/assistant-unifie/gateway/.env"),
    ]
    for p in env_paths:
        if p.is_file():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    if line.startswith("SECRET_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
            except Exception:
                pass
    return ""


def _headers() -> dict:
    key = _get_secret_key()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-Secret-Key"] = key
    return headers


@server.tool()
async def nodes_list() -> str:
    """Vérifie l'état et liste toutes les machines clientes (Tour, PC portable, etc.) connectées à JARVIS.
    Permet de savoir immédiatement si une machine (comme la tour) est allumée et prête à recevoir des ordres.
    """
    headers = _headers()
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(f"{GATEWAY_URL}/api/nodes", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                nodes = data.get("nodes", [])
                if not nodes:
                    return "Aucune machine n'est connectée au hub JARVIS actuellement (machines éteintes ou nc-noeud inactif)."

                lines = [f"🖥️ **Machines connectées à JARVIS ({len(nodes)}) :**"]
                for n in nodes:
                    idle = n.get("idle_seconds", 0)
                    caps = ", ".join(n.get("capabilities", []))
                    lines.append(
                        f"• **{n.get('node_id')}** ({n.get('hostname')}) — État : 🟢 En ligne / Connecté (inactivité: {idle:.0f}s)\n"
                        f"  Capacités : `{caps}`"
                    )
                return "\n".join(lines)
            return f"Erreur serveur JARVIS : HTTP {resp.status_code} ({resp.text})"
        except Exception as exc:
            return f"Impossible de joindre le hub JARVIS ({GATEWAY_URL}) : {exc}"


@server.tool()
async def node_write_file(node_id: str, path: str, content: str) -> str:
    """Crée ou modifie un fichier sur une machine cliente connectée (ex: node_id='tour').
    Une carte de confirmation apparaîtra sur l'écran de la machine dans l'île Noctalia demandant à l'utilisateur d'autoriser.

    Args:
        node_id: Identifiant de la machine cible (ex: 'tour', 'laptop').
        path: Chemin du fichier relatif au dossier personnel ou absolu (ex: 'Bureau/test.txt').
        content: Contenu texte à écrire dans le fichier.
    """
    headers = _headers()
    payload = {"path": path, "content": content}
    async with httpx.AsyncClient(timeout=90.0) as client:
        try:
            resp = await client.post(f"{GATEWAY_URL}/api/node/{node_id}/write_file", headers=headers, json=payload)
            data = resp.json() if resp.status_code in (200, 400, 403, 404, 500) else {}
            if resp.status_code == 200 and data.get("success"):
                return f"✅ Fichier `{path}` créé avec succès sur `{node_id}` (validé par l'utilisateur)."
            err = data.get("error") or data.get("detail") or resp.text
            return f"❌ Échec de l'écriture sur `{node_id}` : {err}"
        except Exception as exc:
            return f"Erreur lors de l'appel à la machine `{node_id}` : {exc}"


@server.tool()
async def node_read_file(node_id: str, path: str) -> str:
    """Lit un fichier sur une machine cliente connectée (ex: node_id='tour').
    Une confirmation peut être demandée sur l'écran de la machine.

    Args:
        node_id: Identifiant de la machine cible (ex: 'tour', 'laptop').
        path: Chemin du fichier (ex: 'Bureau/test.txt').
    """
    headers = _headers()
    payload = {"path": path}
    async with httpx.AsyncClient(timeout=90.0) as client:
        try:
            resp = await client.post(f"{GATEWAY_URL}/api/node/{node_id}/read_file", headers=headers, json=payload)
            data = resp.json() if resp.status_code in (200, 400, 403, 404, 500) else {}
            if resp.status_code == 200 and data.get("success"):
                return f"📄 Contenu de `{path}` sur `{node_id}` :\n\n{data.get('content', '')}"
            err = data.get("error") or data.get("detail") or resp.text
            return f"❌ Impossible de lire `{path}` sur `{node_id}` : {err}"
        except Exception as exc:
            return f"Erreur lors de la lecture sur `{node_id}` : {exc}"


@server.tool()
async def node_exec(node_id: str, command: str) -> str:
    """Exécute une commande shell sur une machine cliente connectée (ex: node_id='tour').
    IMPORTANT : Rédige une commande claire, lisible par l'utilisateur lors de la confirmation,
    sans détachement opaque (`nohup` / `disown`) et sans micro-sondages préalables inutiles.

    Args:
        node_id: Identifiant de la machine cible (ex: 'tour', 'laptop').
        command: Commande shell à exécuter.
    """
    headers = _headers()
    payload = {"command": command}
    async with httpx.AsyncClient(timeout=90.0) as client:
        try:
            resp = await client.post(f"{GATEWAY_URL}/api/node/{node_id}/exec", headers=headers, json=payload)
            data = resp.json() if resp.status_code in (200, 400, 403, 404, 500) else {}
            if resp.status_code == 200 and data.get("success"):
                out = data.get("stdout", "").strip()
                err = data.get("stderr", "").strip()
                msg = f"⚙️ Résultat commande sur `{node_id}` (code {data.get('returncode', 0)}) :\n"
                if out:
                    msg += f"```\n{out}\n```\n"
                if err:
                    msg += f"Erreurs :\n```\n{err}\n```"
                return msg
            err = data.get("error") or data.get("detail") or resp.text
            return f"❌ Échec de la commande sur `{node_id}` : {err}"
        except Exception as exc:
            return f"Erreur lors de l'exécution sur `{node_id}` : {exc}"


@server.tool()
async def node_screenshot(node_id: str) -> str:
    """Prend une capture d'écran de l'affichage actuel de la machine cliente (ex: node_id='tour').

    Args:
        node_id: Identifiant de la machine cible (ex: 'tour', 'laptop').
    """
    headers = _headers()
    async with httpx.AsyncClient(timeout=90.0) as client:
        try:
            resp = await client.post(f"{GATEWAY_URL}/api/node/{node_id}/action", headers=headers, json={"action": "screenshot", "params": {}})
            data = resp.json() if resp.status_code in (200, 400, 403, 404, 500) else {}
            if resp.status_code == 200 and data.get("success"):
                return f"📸 Capture d'écran effectuée sur `{node_id}` avec succès."
            err = data.get("error") or data.get("detail") or resp.text
            return f"❌ Échec capture d'écran sur `{node_id}` : {err}"
        except Exception as exc:
            return f"Erreur lors de la capture sur `{node_id}` : {exc}"


if __name__ == "__main__":
    server.run(transport="stdio")
