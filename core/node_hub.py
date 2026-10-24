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


def normalize_origin(origin: Any) -> Optional[Dict[str, Any]]:
    """Valide et normalise l'origine d'une action ({"via": ..., "chat": ..., "conversation": ...}).
    Retourne None si l'origine est absente ou mal formée (la commande sera alors confirmée normalement).
    """
    if not isinstance(origin, dict) or not origin:
        return None
    via = origin.get("via")
    chat = origin.get("chat")
    conversation = origin.get("conversation")
    if not isinstance(via, str) or not via.strip() or len(via.strip()) > 64:
        return None
    if isinstance(chat, bool) or chat is None:
        return None
    if isinstance(chat, int):
        clean_chat: int | str = chat
    elif isinstance(chat, str) and chat.strip() and len(chat.strip()) <= 128:
        clean_chat = chat.strip()
    else:
        return None
    if isinstance(conversation, bool) or conversation is None:
        return None
    if isinstance(conversation, int):
        clean_conv: int | str = conversation
    elif isinstance(conversation, str) and conversation.strip() and len(conversation.strip()) <= 128:
        clean_conv = conversation.strip()
    else:
        return None
    return {
        "via": via.strip().lower(),
        "chat": clean_chat,
        "conversation": clean_conv,
    }


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
        self._session_update_callbacks: List[Callable[[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]]], Any]] = []
        self._conversation_tokens: Dict[str, str] = {}
        self._active_sessions: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def get_conversation_token(self, session_key: str) -> str:
        """Retourne l'identifiant stable de conversation pour session_key (le crée si absent)."""
        key = (session_key or "default").strip() or "default"
        token = self._conversation_tokens.get(key)
        if not token:
            token = uuid.uuid4().hex[:16]
            self._conversation_tokens[key] = token
        return token

    def set_conversation_token(self, session_key: str, token: str) -> str:
        """Synchronise l'identifiant de conversation avec celui du runner."""
        key = (session_key or "default").strip() or "default"
        clean_token = str(token).strip()
        if clean_token:
            self._conversation_tokens[key] = clean_token
        return self.get_conversation_token(key)

    def rotate_conversation_token(self, session_key: Optional[str] = None) -> str:
        """Génère un nouvel identifiant de conversation (ex: /reset, /new, /resume)."""
        if not session_key or session_key == "all":
            self._conversation_tokens.clear()
            return uuid.uuid4().hex[:16]
        key = session_key.strip() or "default"
        new_token = uuid.uuid4().hex[:16]
        self._conversation_tokens[key] = new_token
        return new_token

    def build_origin(
        self,
        session_key: Optional[str] = None,
        via: Optional[str] = None,
        chat: Any = None,
        conversation: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Construit un dictionnaire origin normalisé à partir d'une session_key ou de champs explicites."""
        if session_key and (not via or chat is None):
            if ":" in session_key:
                prefix, rest = session_key.split(":", 1)
                via = via or prefix
                if chat is None:
                    chat = int(rest) if rest.isdigit() else rest
            else:
                via = via or session_key
                if chat is None:
                    chat = session_key
        if not conversation and session_key:
            conversation = self.get_conversation_token(session_key)
        return normalize_origin({"via": via, "chat": chat, "conversation": conversation})

    def on_session_update(
        self,
        callback: Callable[[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]]], Any],
    ) -> None:
        """Enregistre un callback appelé à l'ouverture, la mise à jour ou la fermeture d'une session."""
        self._session_update_callbacks.append(callback)

    def _notify_session_update(
        self,
        node_id: str,
        session_info: Optional[Dict[str, Any]],
        origin: Optional[Dict[str, Any]] = None,
    ) -> None:
        for cb in self._session_update_callbacks:
            try:
                res = cb(node_id, session_info, origin)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception as exc:
                logger.error("Erreur dans callback session_update: %s", exc)

    def get_active_session(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Retourne l'état de la session de confiance active sur ce nœud (ou None si expirée/absente)."""
        sess = self._active_sessions.get(node_id)
        if not sess:
            return None
        now = time.time()
        expires_at = float(sess.get("expires_at", 0))
        reste_actions = int(sess.get("reste_actions", 0))
        if now >= expires_at or reste_actions <= 0:
            self._active_sessions.pop(node_id, None)
            return None
        return {
            "reste_s": max(0, int(expires_at - now)),
            "reste_actions": reste_actions,
            "origin": sess.get("origin"),
        }

    async def send_session_end(self, node_id: Optional[str] = None) -> List[str]:
        """Envoie {"type": "session_end"} au nœud cible (ou à tous les nœuds si node_id est None/'all')
        pour fermer immédiatement toute session de confiance active.
        """
        targets: List[ConnectedNode] = []
        if not node_id or str(node_id).strip().lower() in ("", "all", "*"):
            targets = list(self._nodes.values())
        else:
            resolved = self.get_node(node_id)
            if resolved:
                targets = [resolved]

        closed_nodes: List[str] = []
        payload = {"type": "session_end"}
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        for node in targets:
            prev_sess = self._active_sessions.pop(node.node_id, None)
            prev_origin = prev_sess.get("origin") if isinstance(prev_sess, dict) else None
            try:
                if node.loop is not None and node.loop.is_running() and node.loop is not current_loop:
                    fut = asyncio.run_coroutine_threadsafe(node.websocket.send_json(payload), node.loop)
                    await asyncio.wrap_future(fut)
                else:
                    await node.websocket.send_json(payload)
                closed_nodes.append(node.node_id)
                logger.info("session_end envoyé au nœud %s", node.node_id)
            except Exception as exc:
                logger.error("Erreur envoi session_end au nœud %s : %s", node.node_id, exc)
            self._notify_session_update(node.node_id, None, prev_origin)

        return closed_nodes

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
                self._active_sessions.pop(node_id, None)
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
        prev_sess = None
        async with self._lock:
            node = self._nodes.get(node_id)
            if node and (websocket is None or node.websocket == websocket):
                del self._nodes[node_id]
                for k in [k for k in self._pending_confirmations if k[0] == node_id]:
                    self._pending_confirmations.pop(k, None)
                prev_sess = self._active_sessions.pop(node_id, None)
                logger.info("Nœud client déconnecté : %s", node_id)
        if prev_sess is not None:
            self._notify_session_update(node_id, None, prev_sess.get("origin"))

    def list_nodes(self) -> List[Dict[str, Any]]:
        """Retourne la liste des nœuds connectés."""
        out: List[Dict[str, Any]] = []
        for node in self._nodes.values():
            d = node.to_dict()
            active_sess = self.get_active_session(node.node_id)
            if active_sess:
                d["active_session"] = active_sess
            out.append(d)
        return out

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
        else:
            prev_sess = self._active_sessions.pop(target_node_id, None)
            if prev_sess is not None:
                self._notify_session_update(target_node_id, None, prev_sess.get("origin"))

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
        origin: Optional[Dict[str, Any]] = None,
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

        clean_origin = normalize_origin(origin)
        payload: Dict[str, Any] = {
            "req_id": req_id,
            "type": "action",
            "action": action,
            "params": params or {},
            "timeout": timeout,
        }
        if clean_origin is not None:
            payload["origin"] = clean_origin

        try:
            await node.websocket.send_json(payload)
            res = await asyncio.wait_for(future, timeout=timeout)
            node.last_seen = time.time()
            if isinstance(res, dict):
                sess = res.get("session")
                if isinstance(sess, dict):
                    try:
                        reste_s = max(0, int(sess.get("reste_s", 0)))
                        reste_actions = max(0, int(sess.get("reste_actions", 0)))
                    except (ValueError, TypeError):
                        reste_s = 0
                        reste_actions = 0
                    if reste_s > 0 and reste_actions > 0:
                        self._active_sessions[node.node_id] = {
                            "expires_at": time.time() + reste_s,
                            "reste_s": reste_s,
                            "reste_actions": reste_actions,
                            "origin": clean_origin,
                        }
                        self._notify_session_update(node.node_id, sess, clean_origin)
                    else:
                        self._active_sessions.pop(node.node_id, None)
                        self._notify_session_update(node.node_id, None, clean_origin)
                elif action == "exec" and node.node_id in self._active_sessions:
                    prev_sess = self._active_sessions.pop(node.node_id, None)
                    self._notify_session_update(
                        node.node_id,
                        None,
                        clean_origin or (prev_sess.get("origin") if isinstance(prev_sess, dict) else None),
                    )
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
        origin: Optional[Dict[str, Any]] = None,
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
            origin=origin,
        )

    async def read_file(
        self,
        path: str,
        node_id: Optional[str] = None,
        timeout: float = 80.0,
        origin: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Lit un fichier sur le nœud distant."""
        return await self.send_action(
            node_id=node_id,
            action="read_file",
            params={"path": path},
            timeout=timeout,
            origin=origin,
        )

    async def write_file(
        self,
        path: str,
        content: str,
        node_id: Optional[str] = None,
        timeout: float = 80.0,
        origin: Optional[Dict[str, Any]] = None,
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
            origin=origin,
        )


# Instance globale singleton
NODE_HUB = NodeHub()
