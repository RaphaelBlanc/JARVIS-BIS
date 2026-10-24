"""
core/node_hub.py — Hub de gestion des nœuds clients connectés (PC portable, Tour, etc.).

Maintient les connexions WebSocket inversées établies par les agents nœuds
sur les machines clientes (Noctalia, Linux, etc.), sans aucun port ouvert sur les PCs.
Permet au serveur (et aux assistants agy/bots/API) d'exécuter des actions, lire/écrire
des fichiers et exécuter des commandes sur les machines clientes de façon sécurisée.
"""

import asyncio
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

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
    loop: Optional[asyncio.AbstractEventLoop] = field(default=None, repr=False)

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
        self._pending_request_nodes: Dict[str, str] = {}
        self._pending_confirmations: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._confirm_request_callbacks: List[Callable[[str, Dict[str, Any]], Any]] = []
        self._confirm_cancel_callbacks: List[Callable[[str, Dict[str, Any]], Any]] = []
        self._lock = asyncio.Lock()

    def _purge_expired_confirmations(self) -> None:
        now = time.time()
        expired = [k for k, c in self._pending_confirmations.items() if now > c["expires_at"]]
        for k in expired:
            self._pending_confirmations.pop(k, None)

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
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

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
                loop=running_loop,
            )
            self._nodes[node_id] = node
            logger.info("Nœud client enregistré : %s (%s, caps=%s)", node_id, hostname, capabilities)
            return node

    async def unregister_node(self, node_id: str, websocket: Optional[WebSocket] = None) -> None:
        """Désenregistre un nœud déconnecté et purge ses confirmations en attente."""
        async with self._lock:
            node = self._nodes.get(node_id)
            if node and (websocket is None or node.websocket == websocket):
                del self._nodes[node_id]
                for k in [k for k in self._pending_confirmations if k[0] == node_id]:
                    self._pending_confirmations.pop(k, None)
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

    def on_confirm_request(self, callback: Callable[[str, Dict[str, Any]], Any]) -> None:
        """Enregistre un callback appelé lors d'une demande de confirmation distante."""
        self._confirm_request_callbacks.append(callback)

    def on_confirm_cancel(self, callback: Callable[[str, Dict[str, Any]], Any]) -> None:
        """Enregistre un callback appelé lors de l'annulation d'une confirmation."""
        self._confirm_cancel_callbacks.append(callback)

    def handle_incoming_message(self, node_id: str, message: Dict[str, Any]) -> None:
        """Traite un message reçu depuis un nœud (réponse à requête, ping/pong, ou confirm_request)."""
        node = self._nodes.get(node_id)
        if node:
            node.last_seen = time.time()

        msg_type = message.get("type")
        req_id = message.get("req_id")
        self._purge_expired_confirmations()

        if msg_type == "confirm_request":
            if not req_id or not isinstance(req_id, str):
                return
            if not node or "remote_confirm" not in node.capabilities:
                logger.warning(
                    "Requête confirm_request (%s) ignorée : le nœud %s n'a pas annoncé la capacité remote_confirm.",
                    req_id,
                    node_id,
                )
                return

            # Vérifie que le req_id appartient bien à ce nœud (pas d'usurpation inter-nœuds ni de requête non sollicitée)
            expected_owner = self._pending_request_nodes.get(req_id)
            if expected_owner is not None and expected_owner != node_id:
                logger.warning(
                    "Requête confirm_request (%s) refusée : appartient à %s et non à %s.",
                    req_id,
                    expected_owner,
                    node_id,
                )
                return
            if self._pending_request_nodes and expected_owner is None:
                logger.warning(
                    "Requête confirm_request (%s) refusée : aucune action en attente pour %s.",
                    req_id,
                    node_id,
                )
                return
            if any(rid == req_id and nid != node_id for (nid, rid) in self._pending_confirmations):
                logger.warning("Requête confirm_request (%s) refusée : collision req_id sur %s.", req_id, node_id)
                return

            try:
                raw_exp = message.get("expires_in")
                expires_in = max(1, min(int(float(raw_exp if raw_exp is not None else 32)), 120))
            except (ValueError, TypeError, OverflowError):
                expires_in = 32

            self._pending_confirmations[(node_id, req_id)] = {
                "node_id": node_id,
                "req_id": req_id,
                "action": str(message.get("action") or ""),
                "summary": str(message.get("summary") or ""),
                "expires_at": time.time() + expires_in,
                "code_required": bool(message.get("code_required", True)),
            }
            logger.info("Requête confirm_request reçue du nœud %s pour req_id %s (expires_in=%ss)", node_id, req_id, expires_in)

            for cb in self._confirm_request_callbacks:
                try:
                    res = cb(node_id, message)
                    if asyncio.iscoroutine(res):
                        asyncio.create_task(res)
                except Exception as exc:
                    logger.error("Erreur dans callback confirm_request: %s", exc)
            return

        elif msg_type == "confirm_cancel":
            if not req_id or not isinstance(req_id, str):
                return
            removed = self._pending_confirmations.pop((node_id, req_id), None)
            if removed is None:
                return
            logger.info("confirm_cancel reçu du nœud %s pour req_id %s", node_id, req_id)
            for cb in self._confirm_cancel_callbacks:
                try:
                    res = cb(node_id, message)
                    if asyncio.iscoroutine(res):
                        asyncio.create_task(res)
                except Exception as exc:
                    logger.error("Erreur dans callback confirm_cancel: %s", exc)
            return

        if msg_type == "ping":
            logger.debug("Ping reçu du nœud %s", node_id)
            return

        # Traitement de la réponse finale à une action
        if req_id and req_id in self._pending_requests:
            owner = self._pending_request_nodes.get(req_id)
            if owner is not None and owner != node_id:
                logger.warning("Réponse ignorée pour req_id %s : reçue de %s au lieu de %s", req_id, node_id, owner)
                return
            future = self._pending_requests[req_id]
            if not future.done():
                future.set_result(message)
            return

    async def send_confirm_response(
        self,
        req_id: str,
        decision: str,
        code: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> bool:
        """Envoie la décision de confirmation (allow ou deny) au nœud client."""
        self._purge_expired_confirmations()
        conf = None
        if node_id:
            conf = self._pending_confirmations.pop((node_id, req_id), None)
        else:
            for k in list(self._pending_confirmations.keys()):
                if k[1] == req_id:
                    conf = self._pending_confirmations.pop(k, None)
                    break

        if not conf:
            logger.warning("confirm_response ignorée : req_id %s introuvable ou déjà traitée", req_id)
            return False

        if time.time() > conf["expires_at"]:
            logger.warning("confirm_response ignorée : req_id %s est expirée", req_id)
            return False

        target_node_id = conf["node_id"]
        node = self._nodes.get(target_node_id)
        if not node:
            logger.warning("confirm_response ignorée : nœud %s déconnecté", target_node_id)
            return False

        decision_clean = str(decision).strip().lower()
        if decision_clean not in ("allow", "deny"):
            logger.warning("confirm_response : décision inconnue '%s'", decision)
            return False

        payload: Dict[str, Any] = {
            "type": "confirm_response",
            "req_id": req_id,
            "decision": decision_clean,
        }

        if decision_clean == "allow":
            # Ne jamais autoriser sans code TOTP ASCII valide à 6 chiffres
            clean_code = str(code).strip() if code else ""
            if not re.fullmatch(r"[0-9]{6}", clean_code, flags=re.ASCII):
                logger.warning("confirm_response 'allow' refusée : code TOTP manquant ou invalide")
                return False
            payload["code"] = clean_code

        try:
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None

            if node.loop is not None and node.loop.is_running() and node.loop is not current_loop:
                fut = asyncio.run_coroutine_threadsafe(node.websocket.send_json(payload), node.loop)
                await asyncio.wrap_future(fut)
            else:
                await node.websocket.send_json(payload)
            logger.info("confirm_response envoyée avec succès pour req_id %s au nœud %s (decision=%s)", req_id, target_node_id, decision_clean)
            return True
        except Exception as exc:
            logger.error("Erreur transmission confirm_response au nœud %s : %s", target_node_id, exc)
            return False

    def get_pending_confirmation(self, req_id: Optional[str] = None, node_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Retourne la demande de confirmation active correspondante si non expirée."""
        self._purge_expired_confirmations()

        if req_id and node_id:
            return self._pending_confirmations.get((node_id, req_id))
        if req_id:
            for (nid, rid), conf in self._pending_confirmations.items():
                if rid == req_id:
                    return conf
            return None
        if len(self._pending_confirmations) == 1:
            return next(iter(self._pending_confirmations.values()))
        return None

    async def send_action(
        self,
        node_id: Optional[str],
        action: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 80.0,
    ) -> Dict[str, Any]:
        """Envoie une requête d'action au nœud cible et attend la réponse."""
        if action in ("exec", "write_file"):
            if not node_id or str(node_id).strip().lower() in ("", "auto", "default", "any"):
                return {
                    "success": False,
                    "error": f"Sécurité : un identifiant de nœud client (node_id) explicite est obligatoire pour l'action '{action}'.",
                }
        if action == "exec":
            if os.getenv("ENABLE_NODE_EXEC", "0").lower() not in ("1", "true", "yes"):
                return {
                    "success": False,
                    "error": "L'exécution distante de commandes (exec) sur les nœuds clients requiert ENABLE_NODE_EXEC=1 côté serveur.",
                }

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
        self._pending_request_nodes[req_id] = node.node_id

        payload = {
            "req_id": req_id,
            "type": "action",
            "action": action,
            "params": params or {},
            "timeout": timeout,
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
            self._pending_request_nodes.pop(req_id, None)
            self._pending_confirmations.pop((node.node_id, req_id), None)

    async def exec_cmd(
        self,
        command: str,
        node_id: Optional[str] = None,
        timeout: float = 75.0,
    ) -> Dict[str, Any]:
        """Exécute une commande shell sécurisée sur le nœud distant (node_id explicite requis)."""
        if not node_id or str(node_id).strip().lower() in ("", "auto", "default", "any"):
            return {
                "success": False,
                "error": "Sécurité : un identifiant de nœud client (node_id) explicite est obligatoire pour exécuter des commandes.",
            }
        if os.getenv("ENABLE_NODE_EXEC", "0").lower() not in ("1", "true", "yes"):
            return {
                "success": False,
                "error": "L'exécution distante de commandes (exec) sur les nœuds clients requiert ENABLE_NODE_EXEC=1 côté serveur.",
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
        timeout: float = 80.0,
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
        timeout: float = 80.0,
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
