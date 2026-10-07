"""
bot.py — Whispry: inline whispers with a Direct-Link Mini App READER.

READ FLOW (the only reveal path — no DM, no /start, no callback popups):

    GROUP CARD  [🔐 Read Whisper]  (url button)
        -> https://t.me/<bot>/<READER_APP_SHORT_NAME>?startapp=<token>
        -> Telegram opens the Mini App WebView OVER THE CURRENT CHAT
        -> page POSTs {token, Telegram.WebApp.initData} to /api/whisper/open
        -> backend validates initData (official HMAC-SHA256 scheme, bot token)
        -> VERIFIED Telegram user id compared with stored target
        -> match  -> content returned (text, or media metadata for streaming)
           mismatch -> "denied" (content never leaves the server)

Notes:
* web_app inline-keyboard buttons are only allowed in private bot chats, so
  the reader uses a Direct-Link Mini App URL button — Telegram's supported
  way to open a Mini App over any chat with verified initData and WITHOUT
  the user having started the bot.
* The startapp token is cryptographically random, maps server-side to the
  whisper, carries no whisper/target IDs or content, and dies with the
  whisper (TTL = whisper TTL + 60 s). The token alone is useless.
* Username targets are resolved to Telegram user IDs when the bot has
  legitimately seen the account (registry); the reader additionally BINDS
  the verified user id on first open. ID match is always the primary check.
* Legacy cards (callback 🔐 from before this deploy): pressing verifies the
  target and swaps the button to the reader URL — no text is ever shown.

CREATION (unchanged syntax):
    @Bot <text> @username      @Bot <text> 123456789
Recent-recipient suggestions, six view modes (Normal/3s/5s/10s/30s/Once),
media whispers (photo/video/audio/document) and logging are all preserved.
LONG TEXT (>256 chars can't be typed into inline queries — Telegram limit):
send the text to the bot's PM first; it becomes "pending long text" and is
attached automatically when whispering with just a target.

Threading (Render) — unchanged: python live.py -> bot thread (explicit
event loop, run_polling(stop_signals=None, close_loop=False)) + Flask main
thread (serves /reader and the open/media/deliver APIs).
"""

import asyncio
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl

from telegram import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, InvalidToken, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChosenInlineResultHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

import storage
from config import (
    BOT_TOKEN,
    GAME_URL,
    LOG_CHANNEL_DEST,
    LONG_TEXT_PENDING_THRESHOLD,
    OWNER_IDS,
    READER_APP_SHORT_NAME,
    SESSION_TTL_SECONDS,
    WHISPER_MAX_LENGTH,
    log_channel_problem,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory state (no database required)
# ---------------------------------------------------------------------------

USERS_KEY = "user_registry"          # telegram user id (as str) -> profile
PENDING_MEDIA_KEY = "pending_media"  # sender id (as str) -> pending media dict
PENDING_TEXT_KEY = "pending_text"    # sender id (as str) -> {"text": ...}

# Whisper sessions live at MODULE level so the Flask thread (reader APIs)
# shares the same store. Every reader mutation happens under this lock.
_whisper_lock = threading.RLock()
WHISPER_SESSIONS: Dict[str, Dict[str, Any]] = {}  # whisper_id -> session dict

# Reader access tokens: token -> {"wid", "expires_at"}.
READER_TOKEN_TTL_SECONDS = SESSION_TTL_SECONDS + 60
_webapp_tokens: Dict[str, Dict[str, Any]] = {}

# How old initData may be (Telegram recommends checking auth_date).
WEBAPP_INITDATA_MAX_AGE_SECONDS = 86400

# Bot runtime handles used by the Flask thread (set during bot startup).
_bot_loop: Optional[asyncio.AbstractEventLoop] = None
_bot_ref: Optional[Any] = None

CALLBACK_ANSWER_MAX = 200
TARGET_ID_MIN_DIGITS = 4

MEDIA_VIEW_MODES: Tuple[str, ...] = ("normal", "3s", "5s", "10s", "30s", "once")
MEDIA_VIEW_DELETE_DELAYS: Dict[str, int] = {"3s": 3, "5s": 5, "10s": 10, "30s": 30}
ONCE_VIEW_DELETE_DELAY_SECONDS = 2  # grace window for DM-delivered media

MEDIA_VIEW_LABELS: Dict[str, str] = {
    "normal": "Normal", "3s": "3 seconds", "5s": "5 seconds",
    "10s": "10 seconds", "30s": "30 seconds", "once": "Once view",
}
MEDIA_RESULT_TITLES: Dict[str, str] = {
    "normal": "🔓 Normal", "3s": "⏳ 3 seconds", "5s": "⏳ 5 seconds",
    "10s": "⏳ 10 seconds", "30s": "⏳ 30 seconds", "once": "👁 Once view",
}
MEDIA_TYPE_LABELS: Dict[str, str] = {
    "photo": "Photo", "video": "Video", "document": "Document/File", "audio": "Audio",
}
MEDIA_ICONS: Dict[str, str] = {
    "photo": "🖼 Photo", "video": "🎬 Video", "document": "📄 File", "audio": "🎵 Audio",
}
MAX_MEDIA_CAPTION_CHARS = 1000

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,64}$")

GMUTE_FILE = "gmute_users.json"
AUTH_FILE = "auth_users.json"
HISTORY_FILE = "recipient_history.json"

_gmute_raw = storage.load_json(GMUTE_FILE, [])
GMUTE_USERS: set = {int(x) for x in _gmute_raw} if isinstance(_gmute_raw, list) else set()
_auth_raw = storage.load_json(AUTH_FILE, [])
AUTH_USERS: set = {int(x) for x in _auth_raw} if isinstance(_auth_raw, list) else set()
_history_raw = storage.load_json(HISTORY_FILE, {})
RECIPIENT_HISTORY: Dict[str, List[Dict[str, Any]]] = (
    _history_raw if isinstance(_history_raw, dict) else {}
)

HISTORY_LIMIT = 15
RECIPIENT_SUGGESTION_LIMIT = 8
RECIPIENT_SUGGESTION_MEDIA_LIMIT = 3

_gmute_failed_chats: set = set()
_used_whisper_ids: set = set()
_username_index: Dict[str, int] = {}  # lower-case username -> telegram id (seen users)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def generate_whisper_id() -> str:
    while True:
        whisper_id = "W-" + secrets.token_hex(3).upper()
        if whisper_id not in _used_whisper_ids:
            _used_whisper_ids.add(whisper_id)
            return whisper_id


def format_time(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(ts) if ts is not None else datetime.now()
    return dt.strftime("%d %b %Y, %I:%M %p")


def fmt_display_name(name: Optional[str], username: Optional[str]) -> str:
    safe_name = html.escape(name or "Unknown")
    if username:
        return f"{safe_name} (@{html.escape(username)})"
    return safe_name


def normalize_username(raw: Optional[str]) -> str:
    """'@Name ' -> 'name' (remove @, trim, lowercase) — the ONE normalization."""
    return (raw or "").strip().lstrip("@").lower()


def _log_username(username: Optional[str]) -> str:
    return f"@{username}" if username else "None"


def _html_fit(raw: Optional[str], budget: int) -> str:
    esc = html.escape(raw or "")
    return esc if len(esc) <= budget else esc[: budget - 1] + "…"


def parse_inline_query(raw: str) -> Tuple[Optional[str], Optional[int], str]:
    """'@Bot <text> <target>' -> (target_username, target_id, text). Target LAST."""
    text = (raw or "").strip()
    if not text:
        return None, None, ""
    parts = text.rsplit(maxsplit=1)
    if len(parts) == 2:
        head, last = parts[0], parts[1]
        if last.startswith("@"):
            candidate = last[1:]
            if candidate and _USERNAME_RE.match(candidate):
                return candidate, None, head.strip()
        elif last.isdigit() and len(last) >= TARGET_ID_MIN_DIGITS:
            return None, int(last), head.strip()
    return None, None, text


# ---------------------------------------------------------------------------
# State access
# ---------------------------------------------------------------------------

def get_sessions(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Dict[str, Any]]:
    return WHISPER_SESSIONS


def get_users(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Dict[str, Any]]:
    return context.application.bot_data.setdefault(USERS_KEY, {})


def get_pending_media(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Dict[str, Any]]:
    return context.application.bot_data.setdefault(PENDING_MEDIA_KEY, {})


def get_pending_text(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Dict[str, Any]]:
    return context.application.bot_data.setdefault(PENDING_TEXT_KEY, {})


def register_user(context: ContextTypes.DEFAULT_TYPE, user: Any) -> None:
    """Bookkeeping registry from Telegram updates only (never user-typed text)."""
    if user is None:
        return
    get_users(context)[str(user.id)] = {
        "id": user.id,
        "username": user.username,
        "name": user.first_name or user.full_name or "Unknown",
    }
    if user.username:
        _username_index[user.username.lower()] = user.id


def prune_sessions(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = time.time()
    with _whisper_lock:
        for wid in [w for w, s in WHISPER_SESSIONS.items() if now > s.get("expires_at", 0)]:
            WHISPER_SESSIONS.pop(wid, None)
        for tk in [t for t, e in _webapp_tokens.items() if now > e["expires_at"]]:
            _webapp_tokens.pop(tk, None)


# ---------------------------------------------------------------------------
# Whisper authorization — ONE rule (callback press AND reader API)
# ---------------------------------------------------------------------------

def is_target_user(session: Dict[str, Any], user: Any) -> bool:
    """
    Stored target_user_id is ALWAYS the primary check (exact Telegram ID).
    Only when no ID is known does the normalized-username fallback apply
    (username reported by Telegram itself — initData or the callback presser).
    """
    if session.get("target_user_id") is not None:
        return user.id == session["target_user_id"]
    presser = normalize_username(getattr(user, "username", None))
    return bool(presser) and presser == session["target_username"]


def _maybe_bind_target_id(session: Dict[str, Any], verified: Any) -> None:
    """
    Upgrade a username-target whisper to an ID-bound one the first time the
    account opens it via verified initData (usernames are unique, so a match
    proves it is the same account). The stored ID then becomes primary.
    """
    if session.get("target_user_id") is None and session["target_type"] == "username":
        vu = normalize_username(getattr(verified, "username", None))
        if vu and vu == session["target_username"]:
            session["target_user_id"] = verified.id
            logger.info(
                "READER TARGET ID BOUND: whisper_id=%s user=%s",
                session["whisper_id"], verified.id,
            )


# ---------------------------------------------------------------------------
# Owner / trusted-auth checks
# ---------------------------------------------------------------------------

def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


def is_authorized(user_id: int) -> bool:
    return is_owner(user_id) or user_id in AUTH_USERS


def _persist_gmute() -> None:
    if not storage.save_json(GMUTE_FILE, sorted(GMUTE_USERS)):
        logger.error("Failed to save %s — mute change is in memory only.", GMUTE_FILE)


def _persist_auth() -> None:
    if not storage.save_json(AUTH_FILE, sorted(AUTH_USERS)):
        logger.error("Failed to save %s — auth change is in memory only.", AUTH_FILE)


def _persist_history() -> None:
    if not storage.save_json(HISTORY_FILE, RECIPIENT_HISTORY):
        logger.error("Failed to save %s — history change is in memory only.", HISTORY_FILE)


# ---------------------------------------------------------------------------
# Recent-recipient history (per sender, private, persisted)
# ---------------------------------------------------------------------------

def _recipient_key(entry: Dict[str, Any]) -> str:
    if entry.get("target_type") == "user_id":
        return f"u{entry.get('user_id')}"
    return "@" + (entry.get("username") or "")


def _history_display(context: ContextTypes.DEFAULT_TYPE, session: Dict[str, Any]) -> str:
    if session["target_type"] == "user_id":
        known = get_users(context).get(str(session.get("target_user_id")))
        if known and known.get("name"):
            return str(known["name"])
        return f"user ID {session.get('target_user_id')}"
    return session["target_display"]


def remember_recipient(
    context: ContextTypes.DEFAULT_TYPE, sender_id: int, session: Dict[str, Any]
) -> None:
    try:
        entry = {
            "target_type": session["target_type"],
            "user_id": session.get("target_user_id"),
            "username": session.get("target_username"),
            "display": _history_display(context, session),
        }
        key = _recipient_key(entry)
        hist = RECIPIENT_HISTORY.setdefault(str(sender_id), [])
        hist[:] = [e for e in hist if _recipient_key(e) != key]
        hist.insert(0, entry)
        del hist[HISTORY_LIMIT:]
        _persist_history()
    except Exception:
        logger.exception("Failed to record recipient history for sender %s", sender_id)


# ---------------------------------------------------------------------------
# Message builders (all user input is HTML-escaped)
# ---------------------------------------------------------------------------

def build_whisper_card(session: Dict[str, Any]) -> str:
    """Public card — NO whisper text, NO media, exact target as entered."""
    if session["target_type"] == "user_id":
        target = f"user ID <b>{session['target_user_id']}</b>"
    else:
        target = "<b>" + html.escape(session["target_display"]) + "</b>"
    return (
        f"🔐 A whisper message to {target}.\n"
        "Only they can read the message.\n\n"
        "　　🔐"
    )


def build_media_caption(session: Dict[str, Any]) -> str:
    """Caption for privately delivered media (verified target only)."""
    sender = fmt_display_name(session["sender_name"], session["sender_username"])
    header = f"🤫 <b>Whisper #{html.escape(session['whisper_id'])}</b>\n👤 From: {sender}"
    body_parts = []
    if session.get("text"):
        body_parts.append("💬 " + _html_fit(session["text"], 600))
    if session.get("media_caption"):
        body_parts.append("📎 " + _html_fit(session["media_caption"], 100))
    mode = session.get("view_mode") or "normal"
    if mode == "once":
        footer = "👁 <i>Once view — this media cannot be opened again.</i>"
    elif mode in MEDIA_VIEW_DELETE_DELAYS:
        footer = f"⏳ <i>This media will be deleted in {MEDIA_VIEW_DELETE_DELAYS[mode]} seconds.</i>"
    else:
        footer = ""
    caption = header
    if body_parts:
        caption += "\n\n" + "\n".join(body_parts)
    if footer:
        caption += "\n\n" + footer
    if len(caption) > MAX_MEDIA_CAPTION_CHARS:
        caption = caption[: MAX_MEDIA_CAPTION_CHARS - 1] + "…"
    return caption


def build_reader_media_caption(session: Dict[str, Any]) -> str:
    """Plain-text caption shown inside the reader for media whispers."""
    parts = []
    if session.get("text"):
        parts.append(session["text"])
    if session.get("media_caption"):
        parts.append(session["media_caption"])
    return "\n\n".join(parts)


def build_log_text(session: Dict[str, Any]) -> str:
    """Complete moderation log — private log channel ONLY."""
    target_type_label = "User ID" if session["target_type"] == "user_id" else "Username"
    text = (
        "🕵️ <b>WHISPER LOG</b>\n\n"
        f"🆔 <b>Whisper ID:</b>\n#{html.escape(session['whisper_id'])}\n\n"
        "👤 <b>Sender:</b>\n"
        f"Name: {html.escape(session.get('sender_name') or 'Unknown')}\n"
        f"Username: {_log_username(session.get('sender_username'))}\n"
        f"ID: <code>{session['sender_id']}</code>\n\n"
        "🎯 <b>Target:</b>\n"
        f"{html.escape(session['target_display'])}\n\n"
        "<b>Target type:</b>\n"
        f"{target_type_label}\n\n"
        "<b>Target ID:</b>\n"
        f"{session.get('target_user_id') or 'Unknown'}\n\n"
    )
    if session.get("media"):
        media_label = MEDIA_TYPE_LABELS.get(session["media"]["type"], "Unknown")
        mode_label = MEDIA_VIEW_LABELS.get(session.get("view_mode") or "normal", "Normal")
        text += (
            "🖼 <b>Media:</b>\n"
            f"{media_label}\n\n"
            "👁 <b>View mode:</b>\n"
            f"{mode_label}\n\n"
        )
        if session.get("media_caption"):
            text += f"📎 <b>Media caption:</b>\n{_html_fit(session['media_caption'], 300)}\n\n"
    elif session.get("view_mode") and session["view_mode"] != "normal":
        text += (
            "👁 <b>View mode:</b>\n"
            f"{MEDIA_VIEW_LABELS.get(session['view_mode'], 'Normal')}\n\n"
        )
    text += (
        "💬 <b>Whisper:</b>\n"
        f"{html.escape(session['text'])}\n\n"
        "🕐 <b>Time:</b>\n"
        f"{format_time(session.get('posted_at') or session['created_at'])}"
    )
    return text


def build_log_media_caption(session: Dict[str, Any]) -> str:
    mode_label = MEDIA_VIEW_LABELS.get(session.get("view_mode") or "normal", "Normal")
    lines = [
        f"🆔 Whisper ID: #{html.escape(session['whisper_id'])}",
        f"🎯 Target: {html.escape(session['target_display'])}",
        f"👁 View mode: {mode_label}",
    ]
    if session.get("text"):
        lines.append("💬 " + _html_fit(session["text"], 500))
    if session.get("media_caption"):
        lines.append("📎 " + _html_fit(session["media_caption"], 100))
    caption = "\n".join(lines)
    if len(caption) > MAX_MEDIA_CAPTION_CHARS:
        caption = caption[: MAX_MEDIA_CAPTION_CHARS - 1] + "…"
    return caption


# ---------------------------------------------------------------------------
# Media helpers (Telegram file IDs only — bot token never leaves the server)
# ---------------------------------------------------------------------------

async def _send_media(
    bot: Any, chat_id: int, media: Dict[str, Any], caption: str
) -> Message:
    media_type = media["type"]
    if media_type == "photo":
        return await bot.send_photo(
            chat_id=chat_id, photo=media["file_id"], caption=caption,
            parse_mode=ParseMode.HTML,
        )
    if media_type == "video":
        return await bot.send_video(
            chat_id=chat_id, video=media["file_id"], caption=caption,
            parse_mode=ParseMode.HTML,
        )
    if media_type == "audio":
        return await bot.send_audio(
            chat_id=chat_id, audio=media["file_id"], caption=caption,
            parse_mode=ParseMode.HTML,
        )
    return await bot.send_document(
        chat_id=chat_id, document=media["file_id"], caption=caption,
        parse_mode=ParseMode.HTML,
    )


def _media_mime_and_name(media: Dict[str, Any]) -> Tuple[str, str]:
    t = media["type"]
    if t == "photo":
        return "image/jpeg", "whisper-photo.jpg"
    if t == "video":
        return (media.get("mime_type") or "video/mp4"), (media.get("file_name") or "whisper-video.mp4")
    if t == "audio":
        return (media.get("mime_type") or "audio/mpeg"), (media.get("file_name") or "whisper-audio.mp3")
    return (
        media.get("mime_type") or "application/octet-stream",
        media.get("file_name") or "whisper-file",
    )


async def _download_media_bytes(bot: Any, media: Dict[str, Any]) -> bytes:
    """Download via the bot's own API client (token stays server-side)."""
    file = await bot.get_file(media["file_id"])
    data = await file.download_as_bytearray()
    return bytes(data)


def schedule_delete(chat_id: int, message_id: int, delay_seconds: int, whisper_id: str) -> None:
    """Schedule a message deletion on the bot's loop (any thread may call)."""
    logger.info(
        "MEDIA WHISPER DELETE REQUESTED: whisper_id=%s delay=%ss", whisper_id, delay_seconds
    )

    async def _task() -> None:
        await asyncio.sleep(delay_seconds)
        bot = _bot_ref
        if bot is None:
            return
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            logger.info("MEDIA WHISPER DELETED: whisper_id=%s", whisper_id)
        except TelegramError as exc:
            logger.warning(
                "MEDIA WHISPER DELETE FAILED (whisper continues): whisper_id=%s error=%s",
                whisper_id, exc,
            )
        except Exception:
            logger.exception("MEDIA WHISPER DELETE FAILED (unexpected): whisper_id=%s", whisper_id)

    loop = _bot_loop
    if loop is not None and not loop.is_closed():
        asyncio.run_coroutine_threadsafe(_task(), loop)


# ---------------------------------------------------------------------------
# THE centralized whisper-log sender (exactly one function, exactly one log)
# ---------------------------------------------------------------------------

async def send_whisper_log(context: ContextTypes.DEFAULT_TYPE, session: Dict[str, Any]) -> None:
    wid = session["whisper_id"]
    if session.get("logged"):
        return
    if LOG_CHANNEL_DEST is None:
        logger.warning("Whisper #%s NOT logged — %s", wid, log_channel_problem())
        return
    logger.info("Sending whisper log: whisper_id=%s target=%s", wid, session["target_display"])
    try:
        await context.bot.send_message(
            chat_id=LOG_CHANNEL_DEST, text=build_log_text(session), parse_mode=ParseMode.HTML
        )
    except Exception:
        logger.exception("WHISPER LOG SEND FAILED: whisper_id=%s", wid)
        return
    session["logged"] = True
    logger.info("WHISPER LOG SENT: whisper_id=%s", wid)
    if session.get("media"):
        await send_media_log_attachment(context, session)


async def send_media_log_attachment(
    context: ContextTypes.DEFAULT_TYPE, session: Dict[str, Any]
) -> None:
    wid = session["whisper_id"]
    try:
        await _send_media(
            context.bot, LOG_CHANNEL_DEST, session["media"], build_log_media_caption(session)
        )
        logger.info("MEDIA WHISPER LOG SENT: whisper_id=%s", wid)
    except Exception:
        logger.exception("MEDIA WHISPER LOG FAILED: whisper_id=%s", wid)


# ---------------------------------------------------------------------------
# Reader tokens + initData validation (official Telegram scheme)
# ---------------------------------------------------------------------------

def _issue_reader_token(wid: str) -> str:
    """Random server-mapped reader token; TTL = whisper TTL (+60 s buffer)."""
    now = time.time()
    with _whisper_lock:
        for tk in [t for t, e in _webapp_tokens.items() if now > e["expires_at"]]:
            _webapp_tokens.pop(tk, None)
        token = secrets.token_urlsafe(16)
        _webapp_tokens[token] = {"wid": wid, "expires_at": now + READER_TOKEN_TTL_SECONDS}
    return token


def build_reader_url(bot_username: str, token: str) -> str:
    """Direct-Link Mini App URL — opens the WebView over ANY chat, no /start."""
    return f"https://t.me/{bot_username}/{READER_APP_SHORT_NAME}?startapp={token}"


def validate_webapp_init_data(init_data: str) -> Optional[SimpleNamespace]:
    """
    Official Telegram Mini App initData validation:

        secret_key        = HMAC_SHA256(key="WebAppData", message=BOT_TOKEN)
        data_check_string = every field except 'hash', sorted, joined 'k=v' by '\\n'
        calculated_hash   = HMAC_SHA256(key=secret_key, message=data_check_string)

    plus an auth_date freshness check. Returns the VERIFIED user (id comes
    only from signed data — never from URL params or typed text).
    """
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        logger.warning("READER INITDATA REJECTED: HMAC mismatch.")
        return None

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if auth_date <= 0 or time.time() - auth_date > WEBAPP_INITDATA_MAX_AGE_SECONDS:
        logger.warning("READER INITDATA REJECTED: auth_date too old/invalid.")
        return None

    try:
        user = json.loads(pairs.get("user", "{}"))
        uid = int(user["id"])
    except Exception:
        logger.warning("READER INITDATA REJECTED: missing/invalid user payload.")
        return None

    return SimpleNamespace(
        id=uid,
        username=user.get("username"),
        name=user.get("first_name") or user.get("full_name") or "Unknown",
    )


def _reader_resolve_token(token: str, now: float) -> Optional[str]:
    """Lock-held: resolve token -> whisper_id (None if invalid/expired)."""
    entry = _webapp_tokens.get(token)
    if entry is None or now > entry["expires_at"]:
        _webapp_tokens.pop(token, None)
        return None
    return entry["wid"]


# ---------------------------------------------------------------------------
# Reader APIs (called synchronously by the Flask thread; thread-safe)
# ---------------------------------------------------------------------------

def open_whisper_via_webapp(token: str, init_data: str) -> Dict[str, Any]:
    """
    THE reader open API. Order (all server-side):
      token -> initData HMAC -> verified user -> target check -> expiry
      -> once/timed state -> content (never before every check passes).
    """
    now = time.time()
    token = (token or "").strip()
    logger.info("READER OPEN REQUEST")

    with _whisper_lock:
        wid = _reader_resolve_token(token, now)
    if wid is None:
        return {"status": "invalid_token"}

    verified = validate_webapp_init_data(init_data)
    if verified is None:
        return {"status": "unauthorized"}
    logger.info("READER USER VERIFIED: user=%s", verified.id)

    with _whisper_lock:
        session = WHISPER_SESSIONS.get(wid)
        if session is None:
            logger.info("WHISPER EXPIRED (reader): whisper_id=%s", wid)
            return {"status": "whisper_expired"}
        if now > session.get("expires_at", 0):
            WHISPER_SESSIONS.pop(wid, None)
            logger.info("WHISPER EXPIRED (reader): whisper_id=%s", wid)
            return {"status": "whisper_expired"}

        _maybe_bind_target_id(session, verified)
        if not is_target_user(session, verified):
            logger.info(
                "READER ACCESS DENIED: whisper_id=%s user=%s", wid, verified.id
            )
            return {"status": "denied"}

        mode = session.get("view_mode") or "normal"
        state = session.get("delivery_state")
        remaining: Optional[int] = None
        first_reveal = not session.get("opened_logged")

        # Media whispers: authorize here, stream/fetch via /api/whisper/media.
        if session.get("media"):
            mime, filename = _media_mime_and_name(session["media"])
            logger.info("READER ACCESS GRANTED (media): whisper_id=%s mode=%s", wid, mode)
            return {
                "status": "ok_media",
                "whisper_id": wid,
                "media_type": session["media"]["type"],
                "caption": build_reader_media_caption(session),
                "mode": mode,
                "mode_label": MEDIA_VIEW_LABELS.get(mode, "Normal"),
                "sender": (
                    (session.get("sender_name") or "Unknown")
                    + (f" (@{session['sender_username']})" if session.get("sender_username") else "")
                ),
                "created": format_time(session.get("created_at")),
                "expires_in": max(0, int(session.get("expires_at", 0) - now)),
            }

        if mode == "once":
            if state == "consumed":
                logger.info("WHISPER ALREADY VIEWED: whisper_id=%s", wid)
                return {"status": "already_viewed"}
            session["delivery_state"] = "consumed"  # atomic check-and-set
        elif mode in MEDIA_VIEW_DELETE_DELAYS:
            delay = MEDIA_VIEW_DELETE_DELAYS[mode]
            if state == "consumed":
                return {"status": "timed_expired"}
            if state == "timed":
                consumes_at = session.get("consumes_at", 0)
                if now >= consumes_at:
                    session["delivery_state"] = "consumed"
                    logger.info("WHISPER EXPIRED (timed): whisper_id=%s", wid)
                    return {"status": "timed_expired"}
                remaining = int(consumes_at - now)
            else:
                session["revealed_at"] = now          # timer starts at first reveal
                session["consumes_at"] = now + delay
                session["delivery_state"] = "timed"
                remaining = delay
        else:
            session["delivery_state"] = "delivered"

        if first_reveal:
            session["opened_logged"] = True

        payload = {
            "status": "ok",
            "whisper_id": wid,
            "text": session["text"],
            "mode": mode,
            "mode_label": MEDIA_VIEW_LABELS.get(mode, "Normal"),
            "remaining": remaining,
            "expires_in": max(0, int(session.get("expires_at", 0) - now)),
            "sender": (
                (session.get("sender_name") or "Unknown")
                + (f" (@{session['sender_username']})" if session.get("sender_username") else "")
            ),
            "created": format_time(session.get("created_at")),
        }

    logger.info("READER ACCESS GRANTED: whisper_id=%s mode=%s user=%s", wid, mode, verified.id)
    if first_reveal:
        _notify_opened(session, verified, wid)
    return payload


def reader_media_request(token: str, init_data: str) -> Dict[str, Any]:
    """
    Stream the whisper media to the VERIFIED target. Bytes are returned to
    live.py (raw Response) — the bot token never appears in any URL.

    State: timer/once consumption is applied AFTER a successful download, so
    a failed fetch stays retryable while a served byte stream counts as read.
    """
    now = time.time()
    token = (token or "").strip()
    logger.info("READER OPEN REQUEST (media)")

    verified = validate_webapp_init_data(init_data)
    if verified is None:
        return {"status": "unauthorized"}
    logger.info("READER USER VERIFIED: user=%s", verified.id)

    with _whisper_lock:
        wid = _reader_resolve_token(token, now)
        if wid is None:
            return {"status": "invalid_token"}
        session = WHISPER_SESSIONS.get(wid)
        if session is None or now > session.get("expires_at", 0):
            if session is not None:
                WHISPER_SESSIONS.pop(wid, None)
            logger.info("WHISPER EXPIRED (reader media): whisper_id=%s", wid)
            return {"status": "whisper_expired"}
        if not session.get("media"):
            return {"status": "not_media"}
        _maybe_bind_target_id(session, verified)
        if not is_target_user(session, verified):
            logger.info("READER ACCESS DENIED (media): whisper_id=%s user=%s", wid, verified.id)
            return {"status": "denied"}

        mode = session.get("view_mode") or "normal"
        state = session.get("delivery_state")
        if mode == "once" and state == "consumed":
            logger.info("WHISPER ALREADY VIEWED: whisper_id=%s", wid)
            return {"status": "already_viewed"}
        if mode in MEDIA_VIEW_DELETE_DELAYS:
            if state == "consumed":
                return {"status": "timed_expired"}
            if state == "timed" and now >= session.get("consumes_at", 0):
                session["delivery_state"] = "consumed"
                return {"status": "timed_expired"}
        first = state is None

    # Download OUTSIDE the lock (Telegram bots can download files <= 20 MB;
    # larger files raise TelegramError -> handled as media_too_large).
    try:
        loop = _bot_loop
        bot = _bot_ref
        if loop is None or bot is None or loop.is_closed():
            return {"status": "error"}
        data = asyncio.run_coroutine_threadsafe(
            _download_media_bytes(bot, session["media"]), loop
        ).result(timeout=90)
    except TelegramError as exc:
        logger.warning("READER MEDIA DOWNLOAD FAILED: whisper_id=%s error=%s", wid, exc)
        return {"status": "media_too_large"}
    except Exception:
        logger.exception("READER MEDIA DOWNLOAD FAILED (unexpected): whisper_id=%s", wid)
        return {"status": "error"}

    with _whisper_lock:
        session = WHISPER_SESSIONS.get(wid)
        if session is None or time.time() > session.get("expires_at", 0):
            return {"status": "whisper_expired"}
        mode = session.get("view_mode") or "normal"
        remaining = None
        if mode == "once":
            if session.get("delivery_state") == "consumed":
                return {"status": "already_viewed"}
            session["delivery_state"] = "consumed"
        elif mode in MEDIA_VIEW_DELETE_DELAYS:
            if session.get("delivery_state") == "consumed":
                return {"status": "timed_expired"}
            if session.get("delivery_state") == "timed":
                consumes_at = session.get("consumes_at", 0)
                if time.time() >= consumes_at:
                    session["delivery_state"] = "consumed"
                    return {"status": "timed_expired"}
                remaining = int(consumes_at - time.time())
            else:
                session["revealed_at"] = time.time()
                session["consumes_at"] = time.time() + MEDIA_VIEW_DELETE_DELAYS[mode]
                session["delivery_state"] = "timed"
                remaining = MEDIA_VIEW_DELETE_DELAYS[mode]
        else:
            session["delivery_state"] = "delivered"
        mime, filename = _media_mime_and_name(session["media"])
        # Reader UI metadata — REQUIRED so the frontend renders the media
        # correctly (photo <img>, video player, audio player) instead of a
        # generic download button with "Unknown" sender.
        media_type = session["media"]["type"]
        sender = session.get("sender_name") or "Unknown"
        if session.get("sender_username"):
            sender += f" (@{session['sender_username']})"
        caption = build_reader_media_caption(session)
        expires_in = max(0, int(session.get("expires_at", 0) - time.time()))

    logger.info(
        "READER ACCESS GRANTED (media stream): whisper_id=%s mode=%s user=%s",
        wid, mode, verified.id,
    )
    if first:
        _notify_opened(session, verified, wid)
    return {
        "status": "ok_media_data",
        "data": data,
        "mime": mime,
        "filename": filename,
        "media_type": media_type,
        "whisper_id": wid,
        "sender": sender,
        "caption": caption,
        "expires_in": expires_in,
        "remaining": remaining,
        "mode": mode,
        "mode_label": MEDIA_VIEW_LABELS.get(mode, "Normal"),
    }


def reader_deliver_request(token: str, init_data: str) -> Dict[str, Any]:
    """
    Fallback for media Telegram bots cannot download (>20 MB): deliver the
    media to the VERIFIED user's Telegram chat via the existing secure
    file_id flow. Identity + target + state checks are identical.
    """
    now = time.time()
    token = (token or "").strip()
    verified = validate_webapp_init_data(init_data)
    if verified is None:
        return {"status": "unauthorized"}
    logger.info("READER USER VERIFIED (deliver): user=%s", verified.id)

    with _whisper_lock:
        wid = _reader_resolve_token(token, now)
        if wid is None:
            return {"status": "invalid_token"}
        session = WHISPER_SESSIONS.get(wid)
        if session is None or now > session.get("expires_at", 0):
            return {"status": "whisper_expired"}
        if not session.get("media"):
            return {"status": "not_media"}
        _maybe_bind_target_id(session, verified)
        if not is_target_user(session, verified):
            logger.info("READER ACCESS DENIED (deliver): whisper_id=%s user=%s", wid, verified.id)
            return {"status": "denied"}
        mode = session.get("view_mode") or "normal"
        if session.get("delivery_state") == "consumed":
            return {"status": "already_viewed" if mode == "once" else "timed_expired"}

    loop = _bot_loop
    if loop is None or loop.is_closed():
        return {"status": "error"}
    try:
        status = asyncio.run_coroutine_threadsafe(
            _dm_deliver_task(wid, verified.id), loop
        ).result(timeout=120)
    except Exception:
        logger.exception("READER DELIVER FAILED: whisper_id=%s", wid)
        return {"status": "error"}
    return {"status": status}


async def _dm_deliver_task(wid: str, chat_id: int) -> str:
    """Send media to the verified user's chat (oversized-file fallback)."""
    with _whisper_lock:
        session = WHISPER_SESSIONS.get(wid)
        if session is None:
            return "whisper_expired"
        if session.get("delivery_state") == "consumed":
            return "already_viewed"
        media = dict(session["media"])
        caption = build_media_caption(session)
        mode = session.get("view_mode") or "normal"

    bot = _bot_ref
    if bot is None:
        return "error"
    logger.info("MEDIA WHISPER DELIVERY REQUESTED: whisper_id=%s mode=%s user=%s", wid, mode, chat_id)
    try:
        delivered = await _send_media(bot, chat_id, media, caption)
    except Forbidden:
        return "need_start"
    except TelegramError as exc:
        logger.warning("MEDIA WHISPER DELIVERY FAILED (retry possible): whisper_id=%s error=%s", wid, exc)
        return "delivery_failed"

    with _whisper_lock:
        session = WHISPER_SESSIONS.get(wid)
        if session is None:
            return "ok"
        if mode == "once":
            session["delivery_state"] = "consumed"
            schedule_delete(chat_id, delivered.message_id, ONCE_VIEW_DELETE_DELAY_SECONDS, wid)
        elif mode in MEDIA_VIEW_DELETE_DELAYS:
            session["delivery_state"] = "consumed"
            schedule_delete(chat_id, delivered.message_id, MEDIA_VIEW_DELETE_DELAYS[mode], wid)
        else:
            session["delivery_state"] = "delivered"
    logger.info("MEDIA WHISPER DELIVERED: whisper_id=%s mode=%s", wid, mode)
    logger.info("MEDIA WHISPER VIEWED: whisper_id=%s", wid)
    return "ok"


def _notify_opened(session: Dict[str, Any], verified: Any, wid: str) -> None:
    """One-time 'WHISPER OPENED' entry in the private log channel (no text)."""
    if LOG_CHANNEL_DEST is None:
        return
    mode_label = MEDIA_VIEW_LABELS.get(session.get("view_mode") or "normal", "Normal")
    log_text = (
        "📖 <b>WHISPER OPENED</b>\n\n"
        f"🆔 Whisper ID: #{html.escape(wid)}\n\n"
        "👤 <b>Sender:</b>\n"
        f"Name: {html.escape(session.get('sender_name') or 'Unknown')}\n"
        f"Username: {_log_username(session.get('sender_username'))}\n"
        f"ID: <code>{session['sender_id']}</code>\n\n"
        "🎯 <b>Recipient (verified):</b>\n"
        f"Name: {html.escape(getattr(verified, 'name', None) or 'Unknown')}\n"
        f"Username: {_log_username(getattr(verified, 'username', None))}\n"
        f"ID: <code>{verified.id}</code>\n\n"
        f"👁 <b>View mode:</b> {mode_label}\n\n"
        f"🕐 <b>Time:</b> {format_time(time.time())}"
    )
    loop = _bot_loop
    if loop is None or loop.is_closed():
        return
    try:
        asyncio.run_coroutine_threadsafe(_send_opened_log(log_text, wid), loop)
    except Exception:
        logger.exception("Could not schedule WHISPER OPENED log for %s", wid)


async def _send_opened_log(log_text: str, wid: str) -> None:
    bot = _bot_ref
    if bot is None:
        return
    try:
        await bot.send_message(chat_id=LOG_CHANNEL_DEST, text=log_text, parse_mode=ParseMode.HTML)
        logger.info("WHISPER OPENED LOG SENT: whisper_id=%s", wid)
    except Exception:
        logger.exception("WHISPER OPENED LOG FAILED: whisper_id=%s", wid)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is not None:
        register_user(context, user)
    if update.message is None:
        return
    bot_username = context.bot.username
    text = (
        "🔐 <b>Whispry — Inline Whisper Bot</b>\n\n"
        "Send private whispers in any chat — the target never needs to start "
        "this bot, and reading happens in a private in-chat viewer. 🤫\n\n"
        "📌 <b>How to send a whisper</b>\n"
        "1️⃣ In any chat, type (the recipient comes LAST):\n"
        f"    <code>@{bot_username} your secret message @username</code>\n"
        f"    <code>@{bot_username} your secret message 123456789</code>\n"
        "2️⃣ Tap the whisper result.\n"
        "3️⃣ A locked card is posted in the chat:\n\n"
        "    🔐 A whisper message to @username.\n"
        "    Only they can read the message.\n\n"
        "4️⃣ The target taps <b>🔐 Read Whisper</b> — a private viewer opens "
        "right over the chat, verified by their Telegram account. Everyone "
        "else sees “❌ This whisper isn't for you.”\n\n"
        "📝 <b>Long whispers</b> — Telegram limits inline typing, so send long "
        "text to my private chat first, then whisper with just the target.\n\n"
        "🖼 <b>Media whispers</b> — send a photo, video, song or file to my "
        "private chat first (see /help).\n\n"
        "🕐 <b>Recent recipients</b> appear as tappable suggestions whenever "
        "you don't type a target.\n\n"
        "Type /help for details, or /game to play. 🎮"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    bot_username = context.bot.username
    text = (
        "📖 <b>Whispry — Help</b>\n\n"
        "🤫 <b>Sending a text whisper</b>\n"
        f"• Type <code>@{bot_username} your message @username</code> — the recipient "
        "comes LAST.\n"
        f"• Or use a numeric Telegram user ID: <code>@{bot_username} your message "
        "123456789</code>.\n"
        "• Recent recipients appear as tappable suggestions whenever you don't type "
        "a target — or pick “➕ New recipient”.\n"
        "• The whisper text is never shown in the chat.\n\n"
        "📝 <b>Long whispers</b>\n"
        f"• Telegram caps inline typing, so for long text just send it to my private "
        f"chat first (up to {WHISPER_MAX_LENGTH} characters). Then whisper with only "
        "the target: <code>@{bot} @username</code> — your long text is attached "
        "automatically.\n\n"
        "📖 <b>Reading a whisper</b>\n"
        "• The target taps <b>🔐 Read Whisper</b> on the card — a private, "
        "full-screen viewer opens right over the chat (no DM, no Start needed).\n"
        "• The viewer verifies the Telegram identity on the server; only the "
        "intended recipient gets the content.\n"
        "• View modes: 🔓 Normal, ⏳ 3s/5s/10s/30s (locks when the countdown ends), "
        "👁 Once view (single read, enforced server-side).\n\n"
        "🖼 <b>Media whispers (photo / video / audio / file)</b>\n"
        "1️⃣ Send the media to my private chat (caption optional).\n"
        "2️⃣ Whisper with a target — pick a view mode — tap to post.\n"
        "3️⃣ The target opens it with 🔐 Read Whisper: media streams inside the "
        "private viewer (up to 20 MB). Bigger files offer “Receive in chat”.\n"
        "• Timed/once media can be opened only once, enforced on the server.\n\n"
        "🎮 <b>Game</b>\n"
        "• Send /game to open the mini app game right inside Telegram.\n\n"
        "ℹ️ <b>Good to know</b>\n"
        f"• Whispers expire after {SESSION_TTL_SECONDS // 60} minutes.\n"
    ).replace("{bot}", bot_username)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if not GAME_URL:
        await update.message.reply_text(
            "🎮 The game is not configured yet.\n"
            "The bot admin needs to set the <b>GAME_URL</b> environment variable.",
            parse_mode=ParseMode.HTML,
        )
        return
    if not GAME_URL.lower().startswith(("http://", "https://")):
        await update.message.reply_text(
            "⚠️ The configured GAME_URL is invalid — it must be a full HTTPS link."
        )
        return
    caption = (
        "🎮 <b>Ready to play?</b>\n"
        "Tap the button below to open the game right inside Telegram!"
    )
    web_app_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(text="🎮 Open Game", web_app=WebAppInfo(url=GAME_URL))]]
    )
    try:
        await update.message.reply_text(
            caption, parse_mode=ParseMode.HTML, reply_markup=web_app_keyboard
        )
        return
    except BadRequest as exc:
        logger.warning(
            "Telegram rejected the Web App button (%s). Falling back to a normal URL button.", exc
        )
    except Forbidden:
        return
    url_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(text="🎮 Open Game", url=GAME_URL)]]
    )
    try:
        await update.message.reply_text(
            caption, parse_mode=ParseMode.HTML, reply_markup=url_keyboard
        )
    except TelegramError as exc:
        logger.error("Could not send the /game message: %s", exc)


# ---------------------------------------------------------------------------
# Target resolution for admin commands (ID argument OR reply-to-message)
# ---------------------------------------------------------------------------

def _resolve_target_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    if context.args:
        raw = context.args[0].strip()
        return int(raw) if raw.isdigit() else None
    message = update.message
    if message is not None and message.reply_to_message is not None:
        replied = message.reply_to_message.from_user
        if replied is not None:
            register_user(context, replied)
            return replied.id
    return None


# ---------------------------------------------------------------------------
# Global GMUTE commands (owner or trusted auth list only)
# ---------------------------------------------------------------------------

async def cmd_gmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    user = update.effective_user
    if user is not None:
        register_user(context, user)
    if user is None or not is_authorized(user.id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    target = _resolve_target_user(update, context)
    if target is None:
        await update.message.reply_text(
            "Usage:\n"
            "• <code>/gmute 123456789</code> — mute by user ID\n"
            "• Reply to a user's message with <code>/gmute</code> — mute them",
            parse_mode=ParseMode.HTML,
        )
        return
    if target == context.bot.id:
        await update.message.reply_text("🤖 Nice try — you can't mute me!")
        return
    if target in OWNER_IDS:
        await update.message.reply_text("🛡 Owners cannot be muted.")
        return
    if target in GMUTE_USERS:
        await update.message.reply_text(
            f"🔇 User <code>{target}</code> is already globally muted.",
            parse_mode=ParseMode.HTML,
        )
        return
    GMUTE_USERS.add(target)
    _persist_gmute()
    known = get_users(context).get(str(target))
    name_part = f" ({html.escape(known['name'])})" if known and known.get("name") else ""
    logger.info(
        "GMUTE ADDED: user=%s by=%s (reply=%s)",
        target, user.id, update.message.reply_to_message is not None,
    )
    await update.message.reply_text(
        f"🔇 User <code>{target}</code>{name_part} is now <b>globally muted</b>.\n"
        "Their messages will be deleted automatically in every group where I "
        "have permission. Use /gunmute to undo.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_gunmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    user = update.effective_user
    if user is not None:
        register_user(context, user)
    if user is None or not is_authorized(user.id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    target = _resolve_target_user(update, context)
    if target is None:
        await update.message.reply_text(
            "Usage:\n"
            "• <code>/gunmute 123456789</code> — unmute by user ID\n"
            "• Reply to a user's message with <code>/gunmute</code> — unmute them",
            parse_mode=ParseMode.HTML,
        )
        return
    if target not in GMUTE_USERS:
        await update.message.reply_text(
            f"ℹ️ User <code>{target}</code> is not globally muted.",
            parse_mode=ParseMode.HTML,
        )
        return
    GMUTE_USERS.discard(target)
    _persist_gmute()
    logger.info("GMUTE REMOVED: user=%s by=%s", target, user.id)
    await update.message.reply_text(
        f"🔊 User <code>{target}</code> has been <b>unmuted globally</b>.",
        parse_mode=ParseMode.HTML,
    )


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """GMUTE fast-path (PTB handler group 1) — untouched."""
    message = update.message
    if message is None:
        return
    sender = message.from_user
    if sender is None or sender.id not in GMUTE_USERS:
        return
    if message.chat.id in _gmute_failed_chats:
        return
    try:
        await context.bot.delete_message(
            chat_id=message.chat.id, message_id=message.message_id
        )
        logger.info("GMUTE MESSAGE DELETED: user=%s chat=%s", sender.id, message.chat.id)
    except Forbidden as exc:
        _gmute_failed_chats.add(message.chat.id)
        logger.warning(
            "GMUTE: cannot delete messages in chat %s (missing permission) — "
            "skipping this chat until restart. Error: %s", message.chat.id, exc,
        )
    except BadRequest as exc:
        logger.warning("GMUTE: delete failed in chat %s: %s", message.chat.id, exc)
    except TelegramError as exc:
        logger.error("GMUTE: Telegram error deleting message in chat %s: %s", message.chat.id, exc)


# ---------------------------------------------------------------------------
# Owner-only trusted-auth management
# ---------------------------------------------------------------------------

async def cmd_addauth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    user = update.effective_user
    if user is not None:
        register_user(context, user)
    if user is None or not is_owner(user.id):
        await update.message.reply_text("❌ Only the bot owner can manage the trusted list.")
        return
    target = _resolve_target_user(update, context)
    if target is None:
        await update.message.reply_text(
            "Usage:\n"
            "• <code>/addauth 123456789</code> — add by user ID\n"
            "• Reply to a user's message with <code>/addauth</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    if target in OWNER_IDS:
        await update.message.reply_text(
            f"ℹ️ User <code>{target}</code> is an owner — always authorized.",
            parse_mode=ParseMode.HTML,
        )
        return
    if target in AUTH_USERS:
        await update.message.reply_text(
            f"ℹ️ User <code>{target}</code> is already on the trusted list.",
            parse_mode=ParseMode.HTML,
        )
        return
    AUTH_USERS.add(target)
    _persist_auth()
    logger.info("AUTH ADDED: user=%s by=%s", target, user.id)
    await update.message.reply_text(
        f"✅ User <code>{target}</code> added to the <b>trusted list</b> "
        "(may now use /gmute and /gunmute).",
        parse_mode=ParseMode.HTML,
    )


async def cmd_rauth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    user = update.effective_user
    if user is not None:
        register_user(context, user)
    if user is None or not is_owner(user.id):
        await update.message.reply_text("❌ Only the bot owner can manage the trusted list.")
        return
    target = _resolve_target_user(update, context)
    if target is None:
        await update.message.reply_text(
            "Usage:\n"
            "• <code>/rauth 123456789</code> — remove by user ID\n"
            "• Reply to a user's message with <code>/rauth</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    if target in OWNER_IDS:
        await update.message.reply_text(
            f"🛡 User <code>{target}</code> is an owner — owners can never be "
            "removed with /rauth.",
            parse_mode=ParseMode.HTML,
        )
        return
    if target not in AUTH_USERS:
        await update.message.reply_text(
            f"ℹ️ User <code>{target}</code> is not on the trusted list.",
            parse_mode=ParseMode.HTML,
        )
        return
    AUTH_USERS.discard(target)
    _persist_auth()
    logger.info("AUTH REMOVED: user=%s by=%s", target, user.id)
    await update.message.reply_text(
        f"✅ User <code>{target}</code> removed from the <b>trusted list</b>.",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Private chat — media intake + long-text intake
# ---------------------------------------------------------------------------

async def on_private_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Photo/video/audio/document -> pending media (file IDs only)."""
    user = update.effective_user
    if user is not None:
        register_user(context, user)
    message = update.message
    if message is None:
        return

    extra: Dict[str, Any] = {}
    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
        file_unique_id = message.photo[-1].file_unique_id
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id
        file_unique_id = message.video.file_unique_id
        extra = {"file_name": message.video.file_name, "mime_type": message.video.mime_type}
    elif message.audio:
        media_type = "audio"
        file_id = message.audio.file_id
        file_unique_id = message.audio.file_unique_id
        extra = {"file_name": message.audio.file_name or "audio", "mime_type": message.audio.mime_type}
    elif message.document:
        media_type = "document"
        file_id = message.document.file_id
        file_unique_id = message.document.file_unique_id
        extra = {"file_name": message.document.file_name or "file", "mime_type": message.document.mime_type}
    else:
        return

    caption = (message.caption or "").strip() or None
    get_pending_media(context)[str(user.id)] = {
        "type": media_type,
        "file_id": file_id,
        "file_unique_id": file_unique_id,
        "caption": caption,
        **{k: v for k, v in extra.items() if v},
    }
    logger.info(
        "MEDIA STORED (pending): user=%s type=%s has_caption=%s",
        user.id, media_type, bool(caption),
    )
    icon = MEDIA_ICONS[media_type]
    caption_note = (
        "\n✍️ The caption you wrote will be delivered together with the media."
        if caption else ""
    )
    await message.reply_text(
        f"✅ <b>{icon} attached!</b> 🔐\n\n"
        "Your media is stored privately (Telegram file ID only — never exposed "
        "publicly)." + caption_note + "\n\n"
        "📌 <b>Now send the whisper</b>\n"
        "1️⃣ Open any chat and type (recipient LAST):\n"
        f"    <code>@{context.bot.username} your message @target</code>\n"
        f"    <code>@{context.bot.username} your message 123456789</code>\n"
        "2️⃣ Pick a view mode:\n"
        "    🔓 Normal · ⏳ 3s · ⏳ 5s · ⏳ 10s · ⏳ 30s · 👁 Once view\n"
        "3️⃣ Tap the result — the locked card is posted; the target reads the "
        "media in the private viewer via 🔐 Read Whisper.\n\n"
        "ℹ️ Send another photo/video/song/file to replace this one.",
        parse_mode=ParseMode.HTML,
    )


async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not message.text:
        return
    user = update.effective_user
    text = message.text.strip()

    # Long text -> pending whisper text for this sender's next whisper.
    # (Inline queries are capped at 256 chars by Telegram — this is how long
    # whispers are created.)
    if user is not None and len(text) > LONG_TEXT_PENDING_THRESHOLD:
        if len(text) > WHISPER_MAX_LENGTH:
            await message.reply_text(
                f"⚠️ That's too long — the limit is {WHISPER_MAX_LENGTH} characters."
            )
            return
        get_pending_text(context)[str(user.id)] = {"text": text, "at": time.time()}
        logger.info("LONG TEXT STORED (pending): user=%s chars=%d", user.id, len(text))
        await message.reply_text(
            "📝 <b>Long text saved!</b> 🔐\n\n"
            "Now open any chat and whisper it to someone (recipient LAST):\n"
            f"    <code>@{context.bot.username} @target</code>\n"
            f"    <code>@{context.bot.username} 123456789</code>\n\n"
            "Your saved text becomes the whisper body. Send another long message "
            "here to replace it.",
            parse_mode=ParseMode.HTML,
        )
        return

    await message.reply_text(
        "🤫 I deliver whispers!\n\n"
        "To send one, type this in any chat (recipient LAST):\n"
        f"<code>@{context.bot.username} your secret message @username</code>\n"
        f"<code>@{context.bot.username} your secret message 123456789</code>\n\n"
        "📝 Long whisper? Send the long text here first.\n"
        "🖼 Media whisper? Send a photo, video, song or file here first.\n\n"
        "Type /help to learn more, or send /game to play. 🎮",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Inline mode — whisper creation (+ recent-recipient suggestions)
# ---------------------------------------------------------------------------

def _format_hint_result(context: ContextTypes.DEFAULT_TYPE) -> InlineQueryResultArticle:
    bot_username = context.bot.username
    return InlineQueryResultArticle(
        id="howto-" + secrets.token_hex(4),
        title="✍️ Format: message + recipient (LAST)",
        description="Example: @bot good morning @rahul",
        input_message_content=InputTextMessageContent(
            message_text=(
                "🔐 <b>How to send a whisper</b>\n\n"
                "Type your whisper first, then the recipient as the LAST token — a "
                "@username or a Telegram user ID:\n"
                f"<code>@{bot_username} your secret message @username</code>\n"
                f"<code>@{bot_username} your secret message 123456789</code>\n\n"
                "📝 Long whisper? Send the text to my private chat first.\n"
                "🖼 Media whisper? Send a photo/video/song/file to my private chat first.\n\n"
                "A locked whisper card is posted in the chat — only the target can "
                "open it with the 🔐 Read Whisper button. 🤫"
            ),
            parse_mode=ParseMode.HTML,
        ),
    )


def _too_long_result() -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id="toolong-" + secrets.token_hex(4),
        title=f"⚠️ Whisper too long (max {WHISPER_MAX_LENGTH})",
        description="Shorten your message and try again",
        input_message_content=InputTextMessageContent(
            message_text=(
                "⚠️ <b>Whisper too long</b>\n\n"
                f"Please keep your whisper under {WHISPER_MAX_LENGTH} characters."
            ),
            parse_mode=ParseMode.HTML,
        ),
    )


def _bot_target_result() -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id="bottarget-" + secrets.token_hex(4),
        title="🤖 You can't whisper to me",
        description="Pick a human recipient",
        input_message_content=InputTextMessageContent(
            message_text=(
                "🤖 <b>Nice try!</b>\n\n"
                "Whispers are for people — pick a human recipient. 😄"
            ),
            parse_mode=ParseMode.HTML,
        ),
    )


def _new_recipient_result(context: ContextTypes.DEFAULT_TYPE) -> InlineQueryResultArticle:
    bot_username = context.bot.username
    return InlineQueryResultArticle(
        id="newrecip-" + secrets.token_hex(4),
        title="➕ New recipient",
        description="Type your message + @username or user ID",
        input_message_content=InputTextMessageContent(
            message_text=(
                "➕ <b>New recipient</b>\n\n"
                "Type the recipient as the LAST token — a @username or a Telegram "
                "user ID:\n"
                f"<code>@{bot_username} your secret message @username</code>\n"
                f"<code>@{bot_username} your secret message 123456789</code>\n\n"
                "You never need to save a recipient first — typing a target always "
                "works. 🤫"
            ),
            parse_mode=ParseMode.HTML,
        ),
    )


def create_whisper_session(
    context: ContextTypes.DEFAULT_TYPE,
    user: Any,
    whisper_text: str,
    target_type: str,
    target_display: str,
    target_username_norm: Optional[str],
    target_user_id: Optional[int],
    media: Optional[Dict[str, Any]] = None,
    media_caption: Optional[str] = None,
    text_source: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Single session factory (manual + suggestion paths).
    - Resolves username targets to a Telegram user ID when the bot has
      legitimately seen that account (registry) — ID becomes the primary
      authorization value.
    - Issues the reader access token at creation time (the URL button is
      built into the inline result; tokens die with the whisper).
    """
    if target_type == "username" and target_user_id is None:
        target_user_id = _username_index.get(target_username_norm or "")

    wid = generate_whisper_id()
    now = time.time()
    session: Dict[str, Any] = {
        "whisper_id": wid,
        "sender_id": user.id,
        "sender_name": user.first_name or user.full_name or "Unknown",
        "sender_username": user.username,
        "target_type": target_type,
        "target_display": target_display,
        "target_username": target_username_norm,
        "target_user_id": target_user_id,
        "text": whisper_text,
        "status": "created",
        "created_at": now,
        "expires_at": now + SESSION_TTL_SECONDS,
        "posted_at": None,
        "logged": False,
        "media": media,
        "media_caption": media_caption,
        "view_mode": None,
        "delivery_state": None,
        "revealed_at": None,
        "consumes_at": None,
        "opened_logged": False,
        "text_source": text_source,     # "pending" when long-text was attached
        "reader_token": _issue_reader_token(wid),
    }
    WHISPER_SESSIONS[wid] = session
    return session


def build_inline_result(
    session: Dict[str, Any],
    title: str,
    description: str,
    mode: Optional[str] = None,
    reader_url: Optional[str] = None,
) -> InlineQueryResultArticle:
    """
    Single inline-result factory. With the reader configured, the button is a
    URL button that opens the Mini App reader DIRECTLY (one tap, no DM).
    Without it, a legacy callback button is used that never reveals text.
    """
    rid = f"{session['whisper_id']}|{mode}" if mode else session["whisper_id"]
    if reader_url:
        button = InlineKeyboardButton(text="🔐 Read Whisper", url=reader_url)
    else:
        button = InlineKeyboardButton(
            text="🔐 Read Whisper", callback_data=f"whisper:{session['whisper_id']}"
        )
    return InlineQueryResultArticle(
        id=rid,
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=build_whisper_card(session), parse_mode=ParseMode.HTML
        ),
        reply_markup=InlineKeyboardMarkup([[button]]),
    )


def _reader_url_for(context: ContextTypes.DEFAULT_TYPE, session: Dict[str, Any]) -> Optional[str]:
    if not READER_APP_SHORT_NAME or not session.get("reader_token"):
        return None
    return build_reader_url(context.bot.username, session["reader_token"])


def _build_recipient_results(
    context: ContextTypes.DEFAULT_TYPE, user: Any, whisper_text: str,
    text_source: Optional[str] = None,
) -> Optional[List[InlineQueryResultArticle]]:
    """Recent-recipient suggestions for THIS sender only (sessions at query time)."""
    entries = RECIPIENT_HISTORY.get(str(user.id)) or []
    if not entries:
        return None

    pending = get_pending_media(context).get(str(user.id))
    results: List[InlineQueryResultArticle] = []

    if pending:
        media = {
            "type": pending["type"],
            "file_id": pending["file_id"],
            "file_unique_id": pending["file_unique_id"],
            **{k: pending[k] for k in ("file_name", "mime_type") if pending.get(k)},
        }
        for entry in entries[:RECIPIENT_SUGGESTION_MEDIA_LIMIT]:
            if entry.get("target_type") == "user_id":
                session = create_whisper_session(
                    context, user, whisper_text, "user_id",
                    str(entry.get("user_id")), None, entry.get("user_id"),
                    media=media, media_caption=pending.get("caption"),
                    text_source=text_source,
                )
            else:
                uname = entry.get("username") or ""
                session = create_whisper_session(
                    context, user, whisper_text, "username",
                    "@" + uname, uname, None,
                    media=media, media_caption=pending.get("caption"),
                    text_source=text_source,
                )
            for mode in MEDIA_VIEW_MODES:
                results.append(
                    build_inline_result(
                        session,
                        title=(
                            f"{MEDIA_RESULT_TITLES[mode]} — to "
                            f"{entry.get('display', session['target_display'])}"
                        ),
                        description="Only the target can open it with 🔐",
                        mode=mode,
                        reader_url=_reader_url_for(context, session),
                    )
                )
    else:
        for entry in entries[:RECIPIENT_SUGGESTION_LIMIT]:
            plain = entry.get("display") or "?"
            if entry.get("target_type") == "user_id":
                session = create_whisper_session(
                    context, user, whisper_text, "user_id",
                    str(entry.get("user_id")), None, entry.get("user_id"),
                    text_source=text_source,
                )
            else:
                uname = entry.get("username") or ""
                session = create_whisper_session(
                    context, user, whisper_text, "username",
                    "@" + uname, uname, None,
                    text_source=text_source,
                )
            results.append(
                build_inline_result(
                    session,
                    title=f"🔐 A whisper message to {plain}",
                    description=f"Only {plain} can open this whisper — tap to post",
                    reader_url=_reader_url_for(context, session),
                )
            )

    results.append(_new_recipient_result(context))
    return results


async def on_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = update.inline_query
    if inline_query is None:
        return
    user = inline_query.from_user

    register_user(context, user)
    prune_sessions(context)

    target_username, target_id, whisper_text = parse_inline_query(inline_query.query)
    whisper_text = (whisper_text or "").strip()

    # Pending long text attaches when the query carries no text of its own.
    text_source: Optional[str] = None
    if not whisper_text:
        pending_entry = get_pending_text(context).get(str(user.id))
        pending_text = (pending_entry or {}).get("text")
        if pending_text:
            whisper_text = pending_text
            text_source = "pending"

    if not whisper_text:
        try:
            await inline_query.answer(
                results=[_format_hint_result(context)], cache_time=1, is_personal=True
            )
        except TelegramError as exc:
            logger.error("Failed to answer inline query: %s", exc)
        return

    if len(whisper_text) > WHISPER_MAX_LENGTH:
        try:
            await inline_query.answer(
                results=[_too_long_result()], cache_time=1, is_personal=True
            )
        except TelegramError as exc:
            logger.error("Failed to answer inline query: %s", exc)
        return

    # --- No explicit target -> recent-recipient suggestions ------------------
    if target_username is None and target_id is None:
        suggestion_results = _build_recipient_results(context, user, whisper_text, text_source)
        if suggestion_results:
            logger.info(
                "INLINE RECIPIENT SUGGESTIONS: user=%s count=%d",
                user.id, len(suggestion_results),
            )
            try:
                await inline_query.answer(
                    results=suggestion_results, cache_time=0, is_personal=True
                )
            except TelegramError as exc:
                logger.error("Failed to answer inline query: %s", exc)
                try:
                    await inline_query.answer(results=[], cache_time=5)
                except TelegramError:
                    pass
            return
        try:
            await inline_query.answer(
                results=[_format_hint_result(context)], cache_time=1, is_personal=True
            )
        except TelegramError as exc:
            logger.error("Failed to answer inline query: %s", exc)
        return

    # --- Manual target path (target stored EXACTLY as entered) ----------------
    if target_username is not None:
        if normalize_username(target_username) == (context.bot.username or "").lower():
            try:
                await inline_query.answer(
                    results=[_bot_target_result()], cache_time=1, is_personal=True
                )
            except TelegramError as exc:
                logger.error("Failed to answer inline query: %s", exc)
            return
        target_type = "username"
        target_display = "@" + target_username
        target_username_norm = normalize_username(target_username)
        target_user_id = None
    else:
        if target_id == context.bot.id:
            try:
                await inline_query.answer(
                    results=[_bot_target_result()], cache_time=1, is_personal=True
                )
            except TelegramError as exc:
                logger.error("Failed to answer inline query: %s", exc)
            return
        target_type = "user_id"
        target_display = str(target_id)
        target_username_norm = None
        target_user_id = int(target_id)

    pending = get_pending_media(context).get(str(user.id))
    if pending:
        session = create_whisper_session(
            context, user, whisper_text, target_type, target_display,
            target_username_norm, target_user_id,
            media={
                "type": pending["type"],
                "file_id": pending["file_id"],
                "file_unique_id": pending["file_unique_id"],
                **{k: pending[k] for k in ("file_name", "mime_type") if pending.get(k)},
            },
            media_caption=pending.get("caption"),
            text_source=text_source,
        )
        logger.info(
            "MEDIA WHISPER CREATED: whisper_id=%s target=%s media=%s",
            session["whisper_id"], session["target_display"], pending["type"],
        )
        reader_url = _reader_url_for(context, session)
        results = [
            build_inline_result(
                session,
                title=f"{MEDIA_RESULT_TITLES[mode]} — to {session['target_display']}",
                description="Only the target can open it with 🔐",
                mode=mode,
                reader_url=reader_url,
            )
            for mode in MEDIA_VIEW_MODES
        ]
    else:
        session = create_whisper_session(
            context, user, whisper_text, target_type, target_display,
            target_username_norm, target_user_id,
            text_source=text_source,
        )
        logger.info(
            "INLINE WHISPER CREATED: whisper_id=%s target=%s",
            session["whisper_id"], session["target_display"],
        )
        results = [
            build_inline_result(
                session,
                title=f"{MEDIA_RESULT_TITLES[mode]} — to {session['target_display']}",
                description="Only they can read the message — tap to post",
                mode=mode,
                reader_url=_reader_url_for(context, session),
            )
            for mode in MEDIA_VIEW_MODES
        ]

    try:
        await inline_query.answer(results=results, cache_time=0, is_personal=True)
    except TelegramError as exc:
        logger.error("Failed to answer inline query: %s", exc)
        try:
            await inline_query.answer(results=[], cache_time=5)
        except TelegramError:
            pass


async def on_chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """THE whisper-creation moment: post-state, log, recipient history."""
    chosen = update.chosen_inline_result
    if chosen is None:
        return

    result_id = chosen.result_id or ""
    if "|" in result_id:
        wid, mode = result_id.split("|", 1)
        if mode not in MEDIA_VIEW_MODES:
            logger.warning("Unknown view mode %r for whisper %s — using normal.", mode, wid)
            mode = "normal"
    else:
        wid, mode = result_id, None

    session = WHISPER_SESSIONS.get(wid)
    if session is None:
        logger.warning("Chosen inline result for unknown whisper id=%s (expired or lost).", wid)
        return

    if chosen.from_user is None or chosen.from_user.id != session["sender_id"]:
        logger.warning("Chosen inline result id=%s selected by non-sender (ignored).", wid)
        return

    logger.info("INLINE WHISPER SELECTED: whisper_id=%s mode=%s", wid, mode or "text")

    if session.get("media"):
        session["view_mode"] = mode or "normal"
        get_pending_media(context).pop(str(chosen.from_user.id), None)
    elif mode is not None:
        session["view_mode"] = mode

    if session.get("text_source") == "pending":
        get_pending_text(context).pop(str(chosen.from_user.id), None)

    if session.get("posted_at") is None:
        session["status"] = "posted"
        session["posted_at"] = time.time()
        logger.info("Whisper card posted to a chat: whisper_id=%s", wid)

    await send_whisper_log(context, session)
    remember_recipient(context, chosen.from_user.id, session)


# ---------------------------------------------------------------------------
# Legacy callback cards (pre-reader deploys) — NEVER reveals text
# ---------------------------------------------------------------------------

async def _safe_answer(query: CallbackQuery, text: str, alert: bool = True) -> None:
    try:
        await query.answer(text=text, show_alert=alert)
    except TelegramError as exc:
        logger.debug("Could not answer callback query: %s", exc)


async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return

    register_user(context, query.from_user)

    if not query.data.startswith("whisper:"):
        await query.answer()
        return

    wid = query.data.split(":", 1)[1].strip()
    now = time.time()

    with _whisper_lock:
        session = WHISPER_SESSIONS.get(wid)
        if session is None or now > session.get("expires_at", 0):
            if session is not None:
                WHISPER_SESSIONS.pop(wid, None)
            await _safe_answer(query, "🔒 This whisper has expired.")
            return

        _maybe_bind_target_id(session, query.from_user)
        if not is_target_user(session, query.from_user):
            logger.info("READER ACCESS DENIED (legacy card): whisper_id=%s", wid)
            await _safe_answer(query, "❌ This whisper isn't for you.")
            return

        # Verified target on a legacy card: swap the button to the reader URL.
        reader_url = _reader_url_for(context, session)
        if reader_url and query.inline_message_id:
            try:
                await context.bot.edit_message_reply_markup(
                    inline_message_id=query.inline_message_id,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton(text="🔐 Read Whisper", url=reader_url)]]
                    ),
                )
                await _safe_answer(
                    query, "📖 Tap “🔐 Read Whisper” on the card to open the reader.",
                    alert=False,
                )
            except TelegramError as exc:
                logger.debug("Could not upgrade legacy card %s: %s", wid, exc)
                await _safe_answer(
                    query, "📖 Please re-send this whisper to get the reader button."
                )
            return

        await _safe_answer(
            query,
            "⚠️ The private reader is not configured yet — ask the bot admin to set "
            "READER_APP_SHORT_NAME (BotFather /newapp).",
        )


# ---------------------------------------------------------------------------
# Error handling + application wiring
# ---------------------------------------------------------------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error while processing an update", exc_info=context.error)


async def post_init(application: Application) -> None:
    global _bot_ref
    _bot_ref = application.bot

    try:
        await application.bot.set_my_commands(
            [
                BotCommand("start", "How to send whispers"),
                BotCommand("help", "Whisper usage guide"),
                BotCommand("game", "Open the mini app game"),
            ]
        )
        logger.info("Commands registered. Whisper bot @%s is ready.", application.bot.username)
    except TelegramError as exc:
        logger.warning("Could not register bot commands: %s", exc)

    logger.info(
        "Persistent state loaded: gmute=%d auth=%d owners=%d history_senders=%d",
        len(GMUTE_USERS), len(AUTH_USERS), len(OWNER_IDS), len(RECIPIENT_HISTORY),
    )
    if READER_APP_SHORT_NAME:
        logger.info(
            "Mini App reader ENABLED: t.me/%s/%s",
            application.bot.username, READER_APP_SHORT_NAME,
        )
    else:
        logger.error(
            "READER_APP_SHORT_NAME is not set — the private whisper reader is "
            "DISABLED and whisper content cannot be opened. Register the reader "
            "via BotFather /newapp (Web App URL = https://<service>/reader) and "
            "set READER_APP_SHORT_NAME to the app short name."
        )

    if LOG_CHANNEL_DEST is not None:
        try:
            await application.bot.send_message(
                chat_id=LOG_CHANNEL_DEST,
                text="🤖 <b>Whisper Bot</b> connected — whisper logging is active.",
                parse_mode=ParseMode.HTML,
            )
            logger.info("LOG_CHANNEL verified — test message delivered to %s.", LOG_CHANNEL_DEST)
        except TelegramError as exc:
            logger.error(
                "LOG CHANNEL ACCESS FAILED for %s — %s: %s. Add the bot to the "
                "channel as an administrator with 'Post Messages'. Whispers keep "
                "working, but logging will fail until this is fixed.",
                LOG_CHANNEL_DEST, type(exc).__name__, exc,
            )
    else:
        problem = log_channel_problem()
        if problem:
            logger.error("LOG CHANNEL CONFIG PROBLEM: %s", problem)


def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    application.bot_data[USERS_KEY] = {}
    application.bot_data[PENDING_MEDIA_KEY] = {}
    application.bot_data[PENDING_TEXT_KEY] = {}

    # Group 0: normal handlers.
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("game", cmd_game))
    application.add_handler(CommandHandler("gmute", cmd_gmute))
    application.add_handler(CommandHandler("gunmute", cmd_gunmute))
    application.add_handler(CommandHandler("addauth", cmd_addauth))
    application.add_handler(CommandHandler("rauth", cmd_rauth))
    application.add_handler(InlineQueryHandler(on_inline_query))
    application.add_handler(ChosenInlineResultHandler(on_chosen_inline_result))
    application.add_handler(CallbackQueryHandler(on_callback_query, pattern=r"^whisper:"))
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & (filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.Document.ALL),
            on_private_media,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            on_private_message,
        )
    )

    # Group 1 — GMUTE fast-path (separate PTB handler group; never swallows
    # commands registered in group 0).
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.UpdateType.MESSAGE,
            on_group_message,
        ),
        group=1,
    )

    application.add_error_handler(on_error)
    return application


def run_bot() -> None:
    if not BOT_TOKEN:
        logger.critical(
            "BOT_TOKEN is not set — the Telegram bot cannot start. "
            "Set it as an environment variable and restart."
        )
        return
    log_problem = log_channel_problem()
    if log_problem:
        logger.warning("%s", log_problem)
    if not OWNER_IDS:
        logger.warning(
            "OWNER_IDS is not set — nobody can use /addauth /rauth (and thus "
            "nobody can manage the global mute list)."
        )
    if not READER_APP_SHORT_NAME:
        logger.error(
            "READER_APP_SHORT_NAME is not set — whispers will be created but the "
            "reader cannot open. See the startup setup instructions (BotFather /newapp)."
        )
    if not GAME_URL:
        logger.warning("GAME_URL is not set — /game will say the game is not configured.")
    elif not GAME_URL.lower().startswith("https://"):
        logger.warning("GAME_URL should start with https:// for Telegram Mini App buttons.")

    logger.info("Starting Telegram bot (long polling)...")
    application = build_application()
    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            close_loop=False,   # _bot_worker() owns and closes the loop itself.
            stop_signals=None,  # no signal handlers on a non-main thread.
        )
    except InvalidToken:
        logger.critical("BOT_TOKEN is invalid. Get a fresh token from @BotFather.")
    except TelegramError as exc:
        logger.critical("Telegram bot stopped with an API error: %s", exc)
    logger.info("Telegram bot polling stopped.")


# ---------------------------------------------------------------------------
# Background-thread startup (Python 3.12 event-loop fix + stop_signals=None)
# ---------------------------------------------------------------------------

_bot_error: Optional[str] = None


def _bot_worker() -> None:
    global _bot_error, _bot_loop

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _bot_loop = loop
    try:
        run_bot()
    except Exception as exc:
        _bot_error = f"{type(exc).__name__}: {exc}"
        logger.exception("Telegram bot thread crashed!")
    finally:
        _bot_loop = None
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def start_bot_thread() -> threading.Thread:
    thread = threading.Thread(target=_bot_worker, name="telegram-bot", daemon=True)
    thread.start()
    return thread


def get_bot_error() -> Optional[str]:
    return _bot_error
