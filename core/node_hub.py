"""
core/node_hub.py — Hub de gestion des nœuds clients connectés (PC portable, Tour, etc.).

Maintient les connexions WebSocket inversées établies par les agents nœuds
sur les machines clientes (Noctalia, Linux, etc.), sans aucun port ouvert sur les PCs.
Permet au serveur (et aux assistants agy/bots/API) d'exécuter des actions, lire/écrire
des fichiers et exécuter des commandes sur les machines clientes de façon sécurisée.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from fastapi import WebSocket
else:
    WebSocket = Any

logger = logging.getLogger("core.node_hub")


@dataclass
class ConnectedNode:
    """Représente une machine cliente connectée via WebSocket."""
    node_id: str
    hostname: str
    os_name: str
    websocket: WebSocket
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    capabilities: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "hostname": self.hostname,
            "os_name": self.os_name,
            "connected_at": self.connected_at,
            "last_seen": self.last_seen,
            "idle_seconds": round(time.time() - self.last_seen, 1),
            "capabilities": self.capabilities,
            "metadata": self.metadata,
        }


class NodeHub:
    """Gestionnaire central des connexions WebSocket de nœuds clients."""

    def __init__(self) -> None:
        self._nodes: Dict[str, ConnectedNode] = {}
        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def register_node(
        self,
        node_id: str,
        websocket: WebSocket,
        info: Optional[Dict[str, Any]] = None,
    ) -> ConnectedNode:
        """Enregistre un nœud client connecté."""
        info = info or {}
        hostname = str(info.get("hostname") or node_id)
        os_name = str(info.get("os") or "linux")
        capabilities = list(info.get("capabilities") or [])
        metadata = dict(info.get("metadata") or {})

        async with self._lock:
            # Si un ancien nœud avec le même ID existe, on ferme proprement son ws
            if node_id in self._nodes:
                old = self._nodes[node_id]
                logger.info("Remplacement du nœud existant %s", node_id)
                try:
                    await old.websocket.close(code=1000, reason="Superseeded by new connection")
                except Exception:
                    pass

            node = ConnectedNode(
                node_id=node_id,
                hostname=hostname,
                os_name=os_name,
                websocket=websocket,
                capabilities=capabilities,
                metadata=metadata,
            )
            self._nodes[node_id] = node
            logger.info("Nœud client enregistré : %s (%s, caps=%s)", node_id, hostname, capabilities)
            return node

    async def unregister_node(self, node_id: str, websocket: Optional[WebSocket] = None) -> None:
        """Désenregistre un nœud déconnecté."""
        async with self._lock:
            node = self._nodes.get(node_id)
            if node and (websocket is None or node.websocket == websocket):
                del self._nodes[node_id]
                logger.info("Nœud client déconnecté : %s", node_id)

    def list_nodes(self) -> List[Dict[str, Any]]:
        """Retourne la liste des nœuds connectés."""
        return [node.to_dict() for node in self._nodes.values()]

    def get_node(self, node_identifier: Optional[str] = None) -> Optional[ConnectedNode]:
        """Résout un nœud par son ID, son hostname ou sélectionne l'unique nœud connecté."""
        if not self._nodes:
            return None

        if not node_identifier or node_identifier.lower() in ("default", "auto", "any"):
            # Si exactement un nœud est connecté, on le sélectionne
            if len(self._nodes) == 1:
                return next(iter(self._nodes.values()))
            return None

        clean_id = node_identifier.strip().lower()

        # 1. Correspondance exacte sur node_id
        for nid, node in self._nodes.items():
            if nid.lower() == clean_id:
                return node

        # 2. Correspondance exacte sur hostname
        for node in self._nodes.values():
            if node.hostname.lower() == clean_id:
                return node

        # 3. Correspondance partielle (ex: 'laptop' matche 'xenospacex-laptop')
        for nid, node in self._nodes.items():
            if clean_id in nid.lower() or clean_id in node.hostname.lower():
                return node

        return None

    def handle_incoming_message(self, node_id: str, message: Dict[str, Any]) -> None:
        """Traite un message reçu depuis un nœud (réponse à requête ou ping/pong)."""
        node = self._nodes.get(node_id)
        if node:
            node.last_seen = time.time()

        msg_type = message.get("type")
        req_id = message.get("req_id")

        if req_id and req_id in self._pending_requests:
            future = self._pending_requests[req_id]
            if not future.done():
                future.set_result(message)
            return

        if msg_type == "ping":
            logger.debug("Ping reçu du nœud %s", node_id)

    async def send_action(
        self,
        node_id: Optional[str],
        action: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 35.0,
    ) -> Dict[str, Any]:
        """Envoie une requête d'action au nœud cible et attend la réponse."""
        node = self.get_node(node_id)
        if not node:
            available = [n["node_id"] for n in self.list_nodes()]
            return {
                "success": False,
                "error": f"Nœud '{node_id or 'auto'}' introuvable ou non connecté. Nœuds actifs : {available or 'aucun'}",
            }

        req_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_requests[req_id] = future

        payload = {
            "req_id": req_id,
            "type": "action",
            "action": action,
            "params": params or {},
        }

        try:
            await node.websocket.send_json(payload)
            res = await asyncio.wait_for(future, timeout=timeout)
            node.last_seen = time.time()
            return res
        except asyncio.TimeoutError:
            logger.warning("Timeout (%ss) sur action '%s' envoyée au nœud %s", timeout, action, node.node_id)
            return {
                "success": False,
                "error": f"Le nœud '{node.node_id}' n'a pas répondu dans le délai imparti ({timeout}s).",
            }
        except Exception as exc:
            logger.error("Erreur communication nœud %s : %s", node.node_id, exc)
            return {"success": False, "error": str(exc)}
        finally:
            self._pending_requests.pop(req_id, None)

    async def exec_cmd(
        self,
        command: str,
        node_id: Optional[str] = None,
        timeout: float = 60.0,
    ) -> Dict[str, Any]:
        """Exécute une commande shell sécurisée sur le nœud distant (node_id explicite requis)."""
        if not node_id or str(node_id).strip().lower() in ("", "auto", "default", "any"):
            return {
                "success": False,
                "error": "Sécurité : un identifiant de nœud client (node_id) explicite est obligatoire pour exécuter des commandes.",
            }
        return await self.send_action(
            node_id=node_id,
            action="exec",
            params={"command": command, "timeout": timeout},
            timeout=timeout + 5.0,
        )

    async def read_file(
        self,
        path: str,
        node_id: Optional[str] = None,
        timeout: float = 35.0,
    ) -> Dict[str, Any]:
        """Lit un fichier sur le nœud distant."""
        return await self.send_action(
            node_id=node_id,
            action="read_file",
            params={"path": path},
            timeout=timeout,
        )

    async def write_file(
        self,
        path: str,
        content: str,
        node_id: Optional[str] = None,
        timeout: float = 35.0,
    ) -> Dict[str, Any]:
        """Écrit un fichier sur le nœud distant (node_id explicite requis)."""
        if not node_id or str(node_id).strip().lower() in ("", "auto", "default", "any"):
            return {
                "success": False,
                "error": "Sécurité : un identifiant de nœud client (node_id) explicite est obligatoire pour écrire des fichiers.",
            }
        return await self.send_action(
            node_id=node_id,
            action="write_file",
            params={"path": path, "content": content},
            timeout=timeout,
        )


# Instance globale singleton
NODE_HUB = NodeHub()
