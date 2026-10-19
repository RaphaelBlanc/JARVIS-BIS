"""Dynamic Island (nc-ile / Quickshell) & Local IPC Bridge for JARVIS.

Provides:
- Shared state & history between Telegram and hyperland-noctalia (nc-ile)
- Real-time state tracking (idle, listening, thinking, tool_use, confirm_required)
- LocalUpdate / LocalContext proxies so all 54 Telegram slash commands, the 0-token
  NLP router, and the full Antigravity AI pipeline work natively without Telegram Update objects
- synchronization with nc-ile Pomodoro timer & DND (qs -c nc-ile ipc call ile ...)
"""

import asyncio
from datetime import datetime
import html
import json
import logging
from pathlib import Path
import re
import shutil
import time
from typing import Any, Callable
import uuid

from core.config import DATA_DIR
HISTORY_FILE = DATA_DIR / "island_history.json"
MAX_HISTORY = 40


def strip_telegram_html(raw: str) -> str:
    """Converts Telegram HTML message into clean readable plain text for QML / CLI."""
    if not raw:
        return ""
    text = raw
    # Replace links <a href="url">label</a> -> label (url)
    text = re.sub(r'<a\s+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', r"\2 (\1)", text, flags=re.IGNORECASE | re.DOTALL)
    # Remove HTML tags
    text = re.sub(r"</?(?:b|strong|i|em|u|s| strike|del|code|pre|blockquote|tg-spoiler)[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


class JarvisIslandBridge:
    """Central state, history, and event bus connecting agy_bot.py with Quickshell nc-ile."""

    def __init__(self) -> None:
        self.state: str = "idle"  # idle | listening | thinking | tool_use | confirm_required
        self.detail: str = "Prêt"
        self.tts_enabled: bool = False
        self.history: list[dict[str, Any]] = self._load_history()
        self.subscribers: set[asyncio.Queue] = set()
        self.last_updated: float = time.time()

    def _load_history(self) -> list[dict[str, Any]]:
        if HISTORY_FILE.exists():
            try:
                data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data[-MAX_HISTORY:]
            except Exception as e:
                logging.warning(f"Failed to load island history: {e}")
        return []

    def _save_history(self) -> None:
        try:
            HISTORY_FILE.write_text(
                json.dumps(self.history[-MAX_HISTORY:], indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logging.warning(f"Failed to save island history: {e}")

    def set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        if detail:
            self.detail = strip_telegram_html(detail)[:120]
        elif state == "idle":
            self.detail = "Prêt"
        self.last_updated = time.time()
        self.broadcast({"type": "state", "state": self.state, "detail": self.detail})

    def add_message(
        self,
        role: str,
        text: str,
        source: str = "local",
        raw_html: str | None = None,
        buttons: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        clean = strip_telegram_html(text)
        if not clean:
            return {}
        msg = {
            "id": uuid.uuid4().hex[:8],
            "role": role,  # "user" | "jarvis"
            "text": clean,
            "html": raw_html or text,
            "source": source,  # "local" | "telegram" | "spotlight" | "voice"
            "time": datetime.now().strftime("%H:%M"),
            "timestamp": time.time(),
            "buttons": buttons or [],
        }
        self.history.append(msg)
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]
        self._save_history()
        self.last_updated = time.time()
        self.broadcast({"type": "message", "message": msg})
        return msg

    def clear_history(self) -> None:
        self.history = []
        self._save_history()
        self.last_updated = time.time()
        self.broadcast({"type": "clear"})

    def broadcast(self, event: dict[str, Any]) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


BRIDGE = JarvisIslandBridge()


async def sync_nc_ile_focus(active: bool, duration_sec: int = 0) -> None:
    """Synchronizes JARVIS Deep Work Pomodoro with Quickshell nc-ile timer and DND."""
    qs_bin = shutil.which("qs")
    if not qs_bin:
        return
    try:
        if active and duration_sec > 0:
            p1 = await asyncio.create_subprocess_exec(
                qs_bin, "-c", "nc-ile", "ipc", "call", "ile", "minuteur", str(int(duration_sec)),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(p1.wait(), timeout=2.0)
            p2 = await asyncio.create_subprocess_exec(
                qs_bin, "-c", "nc-ile", "ipc", "call", "ile", "concentrationRegler", "true",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(p2.wait(), timeout=2.0)
        else:
            p1 = await asyncio.create_subprocess_exec(
                qs_bin, "-c", "nc-ile", "ipc", "call", "ile", "minuteurStop",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(p1.wait(), timeout=2.0)
            p2 = await asyncio.create_subprocess_exec(
                qs_bin, "-c", "nc-ile", "ipc", "call", "ile", "concentrationRegler", "false",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(p2.wait(), timeout=2.0)
    except Exception as e:
        logging.debug(f"nc-ile IPC sync skipped: {e}")


def extract_buttons_from_markup(reply_markup: Any) -> list[dict[str, str]]:
    """Extracts callback buttons from Telegram InlineKeyboardMarkup for QML rendering."""
    if not reply_markup or not hasattr(reply_markup, "inline_keyboard"):
        return []
    buttons: list[dict[str, str]] = []
    try:
        for row in reply_markup.inline_keyboard:
            for btn in row:
                cb = getattr(btn, "callback_data", None)
                txt = getattr(btn, "text", "")
                if cb and txt:
                    buttons.append({"text": txt, "data": cb})
    except Exception:
        pass
    return buttons


class LocalStatusMessage:
    """Mock Telegram Message returned by reply_text / send_message during local execution."""

    def __init__(self, session: "LocalCaptureSession", text: str = "", reply_markup: Any = None) -> None:
        self.session = session
        self.chat_id = session.user_id
        self.message_id = int(time.time() * 1000) % 1000000
        self.text = text
        self.reply_markup = reply_markup
        self.session._on_status_or_reply(text, reply_markup, is_edit=False)

    async def edit_text(self, text: str, parse_mode: Any = None, reply_markup: Any = None, **kwargs: Any) -> "LocalStatusMessage":
        self.text = text
        if reply_markup is not None:
            self.reply_markup = reply_markup
        self.session._on_status_or_reply(text, self.reply_markup, is_edit=True)
        return self

    async def edit_caption(self, caption: str, parse_mode: Any = None, reply_markup: Any = None, **kwargs: Any) -> "LocalStatusMessage":
        return await self.edit_text(caption, parse_mode=parse_mode, reply_markup=reply_markup, **kwargs)

    async def edit_reply_markup(self, reply_markup: Any = None, **kwargs: Any) -> "LocalStatusMessage":
        self.reply_markup = reply_markup
        return self

    async def reply_text(self, text: str, parse_mode: Any = None, reply_markup: Any = None, **kwargs: Any) -> "LocalStatusMessage":
        return LocalStatusMessage(self.session, text, reply_markup)

    async def reply_photo(self, photo: Any, caption: str = "", **kwargs: Any) -> "LocalStatusMessage":
        return LocalStatusMessage(self.session, caption or "📸 Photo capturée.")

    async def delete(self) -> bool:
        return True


class LocalBotProxy:
    """Wraps the Telegram bot so local executions capture messages without spamming Telegram."""

    def __init__(self, session: "LocalCaptureSession", real_bot: Any = None) -> None:
        self.session = session
        self.real_bot = real_bot

    async def send_message(self, chat_id: int, text: str, parse_mode: Any = None, reply_markup: Any = None, **kwargs: Any) -> LocalStatusMessage:
        return LocalStatusMessage(self.session, text, reply_markup)

    async def send_chat_action(self, chat_id: int, action: Any, **kwargs: Any) -> bool:
        return True

    async def send_document(self, chat_id: int, document: Any, caption: str = "", **kwargs: Any) -> LocalStatusMessage:
        return LocalStatusMessage(self.session, caption or "📄 Document généré.")

    async def send_photo(self, chat_id: int, photo: Any, caption: str = "", **kwargs: Any) -> LocalStatusMessage:
        return LocalStatusMessage(self.session, caption or "📸 Image générée.")

    async def send_voice(self, chat_id: int, voice: Any, caption: str = "", **kwargs: Any) -> LocalStatusMessage:
        return LocalStatusMessage(self.session, caption or "🔊 Audio généré.")


class LocalUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        username = os.getenv("USER", Path.home().name or "user")
        self.first_name = username.capitalize()
        self.username = username


class LocalChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id
        self.type = "private"


class LocalIncomingMessage:
    def __init__(self, session: "LocalCaptureSession", text: str) -> None:
        self.session = session
        self.text = text
        self.caption = ""
        self.chat_id = session.user_id
        self.from_user = LocalUser(session.user_id)
        self.chat = LocalChat(session.user_id)
        self.photo = None
        self.document = None
        self.voice = None
        self.audio = None

    async def reply_text(self, text: str, parse_mode: Any = None, reply_markup: Any = None, **kwargs: Any) -> LocalStatusMessage:
        return LocalStatusMessage(self.session, text, reply_markup)

    async def reply_photo(self, photo: Any, caption: str = "", **kwargs: Any) -> LocalStatusMessage:
        return LocalStatusMessage(self.session, caption or "📸 Capture d'écran enregistrée.")

    async def reply_document(self, document: Any, caption: str = "", **kwargs: Any) -> LocalStatusMessage:
        return LocalStatusMessage(self.session, caption or "📄 Fichier prêt.")

    async def reply_voice(self, voice: Any, caption: str = "", **kwargs: Any) -> LocalStatusMessage:
        return LocalStatusMessage(self.session, caption or "🔊 Message vocal.")

    async def delete(self) -> bool:
        return True


class LocalUpdate:
    def __init__(self, session: "LocalCaptureSession", text: str) -> None:
        self.effective_user = LocalUser(session.user_id)
        self.effective_chat = LocalChat(session.user_id)
        self.message = LocalIncomingMessage(session, text)
        self.callback_query = None


class LocalCallbackQuery:
    def __init__(self, session: "LocalCaptureSession", data: str) -> None:
        self.session = session
        self.data = data
        self.from_user = LocalUser(session.user_id)
        self.message = LocalIncomingMessage(session, "")

    async def answer(self, text: str = "", show_alert: bool = False, **kwargs: Any) -> None:
        if text:
            self.session.callback_toast = text

    async def edit_message_text(self, text: str, parse_mode: Any = None, reply_markup: Any = None, **kwargs: Any) -> LocalStatusMessage:
        return LocalStatusMessage(self.session, text, reply_markup)

    async def edit_message_caption(self, caption: str, parse_mode: Any = None, reply_markup: Any = None, **kwargs: Any) -> LocalStatusMessage:
        return LocalStatusMessage(self.session, caption, reply_markup)

    async def edit_message_reply_markup(self, reply_markup: Any = None, **kwargs: Any) -> None:
        pass


class LocalContext:
    def __init__(self, session: "LocalCaptureSession", args: list[str] | None = None, real_bot: Any = None, bot_data: dict | None = None) -> None:
        self.bot = LocalBotProxy(session, real_bot=real_bot)
        self.args = args or []
        self.bot_data = bot_data if bot_data is not None else {}
        self.user_data = {}
        self.chat_data = {}


class LocalCaptureSession:
    """Captures all progressive status updates and final output from any JARVIS command/pipeline."""

    def __init__(self, user_id: int, stream_callback: Callable[[str, str], None] | None = None) -> None:
        self.user_id = user_id
        self.messages: list[tuple[str, Any]] = []
        self.latest_text: str = ""
        self.latest_markup: Any = None
        self.callback_toast: str = ""
        self.stream_callback = stream_callback

    def _on_status_or_reply(self, text: str, reply_markup: Any = None, is_edit: bool = False) -> None:
        clean = strip_telegram_html(text)
        self.latest_text = text
        if reply_markup is not None:
            self.latest_markup = reply_markup
        if is_edit and self.messages:
            self.messages[-1] = (text, reply_markup)
        else:
            self.messages.append((text, reply_markup))

        # Check if this is an intermediate status message or progressive preview
        is_intermediate = any(
            clean.startswith(prefix)
            for prefix in (
                "⚡ Initialisation",
                "⚡ Analyse",
                "🧠 Mode Flash High",
                "📅 Consultation",
                "🗓️ Modification",
                "✉️ Consultation",
                "✍️ Préparation",
                "📤 Envoi",
                "📁 Recherche",
                "💾 Sauvegarde",
                "✅ Gestion",
                "👥 Recherche",
                "⚙️ Action",
                "💻 Exécution",
                "📄 Accès",
                "🌐 Recherche",
                "✍️ En cours de rédaction...",
                "⏳",
                "🔍",
                "🎓 Synchronisation",
                "✉️ Tri intelligent",
            )
        )
        if is_intermediate:
            first_line = clean.splitlines()[0] if clean else "Traitement..."
            BRIDGE.set_state("tool_use" if "✍️ En cours" not in first_line else "thinking", first_line)
            if self.stream_callback:
                self.stream_callback("status", clean)
        else:
            if self.stream_callback:
                self.stream_callback("final", clean)

    def get_final_output(self) -> tuple[str, str, list[dict[str, str]]]:
        """Returns (clean_text, raw_html, buttons)."""
        if not self.messages and self.callback_toast:
            return self.callback_toast, self.callback_toast, []
        # Combine non-intermediate messages if multiple were sent, else take the last one
        non_empty = [(t, m) for (t, m) in self.messages if strip_telegram_html(t)]
        if not non_empty:
            return "Opération effectuée.", "Opération effectuée.", []
        raw_html, markup = non_empty[-1]
        clean = strip_telegram_html(raw_html)
        buttons = extract_buttons_from_markup(markup)
        return clean, raw_html, buttons
