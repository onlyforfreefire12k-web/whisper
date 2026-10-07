"""
bot.py — Whispry: Inline Whisper Bot with secure MEDIA WHISPERS, a Mini App
LONG-WHISPER READER, GLOBAL GMUTE, owner-only auth, and recent recipients.

TEXT WHISPER CREATION (unchanged):
    @BotName <whisper text> @TargetUsername
    @BotName <whisper text> 123456789
-> locked text-only card + 🔐 button; verified target only; 15-min TTL.
Manual text whispers now also offer the six view modes (NORMAL, 3s, 5s, 10s,
30s, ONCE) so timed/once text whispers work; recent-recipient suggestions
remain single normal-mode results (previous behavior preserved).

MINI APP LONG-WHISPER READER (new):
* Requires WEBAPP_URL (config). When set, a verified target pressing 🔐 gets
  a private DM with a real web_app button ("📖 Open Private Reader") that
  opens /reader on this service — a full-screen, scrollable, dark reader for
  very long whispers (Telegram popups are no longer used for long texts).
* SECURITY: the reader page POSTs the short-lived random access token plus
  Telegram WebApp initData to POST /api/whisper/open. The backend validates
  initData with Telegram's official HMAC-SHA256 scheme (secret = HMAC
  ("WebAppData", BOT_TOKEN)), extracts the VERIFIED Telegram user id, and
  only then checks it against the stored whisper target. Token alone is
  useless; no whisper id / target id / text is in any URL; the bot token
  never reaches the frontend.
* Timed text whispers: the server records the first successful reveal and a
  consumes_at deadline; the API returns the remaining seconds and refuses
  content after the deadline (state enforced server-side, not by JS).
* Once-view text whispers: consumed ATOMICALLY (check-and-set under a lock)
  on the first successful open; reopening returns "already viewed".
* Fallbacks: without WEBAPP_URL, or when the DM fails and the text is short,
  the classic popup reveal is used — existing behavior is never broken.

MEDIA WHISPER FLOW (unchanged): photo/video/document -> pending media ->
six view-mode results -> 🔐 -> private DM delivery with timers/once states.
Media never enters the Mini App and is never exposed publicly.

RECENT RECIPIENTS (unchanged): per-sender, persisted, private; suggestions
only when no explicit target is typed; manual @username / numeric-ID
targeting keeps priority and works exactly as before.

GLOBAL GMUTE (unchanged): /gmute, /gunmute (owner or trusted list; by ID or
reply), fast-path deletion in PTB handler group 1, persistent JSON store.

OWNER-ONLY AUTH (unchanged): /addauth, /rauth (owners only; owners immune),
persisted; trusted list gates /gmute and /gunmute.

Threading model (Render Web Service) — unchanged:

    python live.py
        ├── Telegram bot -> daemon background thread (start_bot_thread)
        └── Flask        -> main thread (+ /reader Mini App + open API)

Event-loop / signal notes (Python 3.12, background thread) — unchanged:
the thread's event loop is created explicitly (asyncio.new_event_loop() +
asyncio.set_event_loop()) BEFORE the bot starts, and
Application.run_polling() is called with stop_signals=None (no SIGINT/SIGTERM
handlers on a non-main thread — set_wakeup_fd only works in the main thread)
and close_loop=False (_bot_worker() owns and closes the loop itself).
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
    OWNER_IDS,
    SESSION_TTL_SECONDS,
    WEBAPP_URL,
    WHISPER_MAX_LENGTH,
    log_channel_problem,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory state (no database required)
# ---------------------------------------------------------------------------

USERS_KEY = "user_registry"          # telegram user id (as str) -> profile
PENDING_MEDIA_KEY = "pending_media"  # sender id (as str) -> pending media dict

# Whisper sessions now live at MODULE level (instead of bot_data) so the
# Flask Mini App API (main thread) can access the same store thread-safely.
# Bot handlers keep using get_sessions(context) — same dict, same behavior.
_whisper_lock = threading.RLock()
WHISPER_SESSIONS: Dict[str, Dict[str, Any]] = {}  # whisper_id -> session dict

# Short-lived reader access tokens: token -> {"wid", "expires_at"}.
# Cryptographically random, server-side mapping, quick expiry, single whisper,
# no whisper/target IDs encoded in the token itself.
WEBAPP_TOKEN_TTL_SECONDS = 120
_webapp_tokens: Dict[str, Dict[str, Any]] = {}

# How old Telegram initData may be when validated (Telegram recommends
# checking auth_date; whispers' own 15-min TTL is the tighter bound anyway).
WEBAPP_INITDATA_MAX_AGE_SECONDS = 86400

# Bot runtime handles used by the Flask thread (set during bot startup).
_bot_loop: Optional[asyncio.AbstractEventLoop] = None
_bot_ref: Optional[Any] = None

# answerCallbackQuery text is limited to 200 characters by Telegram.
CALLBACK_ANSWER_MAX = 200

# Minimum digit count for a trailing numeric token to be treated as a
# Telegram user ID (guards against ordinary sentences ending in a number).
TARGET_ID_MIN_DIGITS = 4

# --- Media whisper view modes (exactly these — no other timer values) -------
MEDIA_VIEW_MODES: Tuple[str, ...] = ("normal", "3s", "5s", "10s", "30s", "once")

# Seconds to keep the media after successful delivery (timed modes).
MEDIA_VIEW_DELETE_DELAYS: Dict[str, int] = {"3s": 3, "5s": 5, "10s": 10, "30s": 30}

# ONCE VIEW: bots cannot detect when a user actually "views" media, so the
# single viewing opportunity is the delivery itself. The whisper is consumed
# immediately and the message is deleted after this short grace window.
ONCE_VIEW_DELETE_DELAY_SECONDS = 2

MEDIA_VIEW_LABELS: Dict[str, str] = {
    "normal": "Normal",
    "3s": "3 seconds",
    "5s": "5 seconds",
    "10s": "10 seconds",
    "30s": "30 seconds",
    "once": "Once view",
}

MEDIA_RESULT_TITLES: Dict[str, str] = {
    "normal": "🔓 Normal",
    "3s": "⏳ 3 seconds",
    "5s": "⏳ 5 seconds",
    "10s": "⏳ 10 seconds",
    "30s": "⏳ 30 seconds",
    "once": "👁 Once view",
}

MEDIA_TYPE_LABELS: Dict[str, str] = {
    "photo": "Photo",
    "video": "Video",
    "document": "Document/File",
}

MEDIA_ICONS: Dict[str, str] = {"photo": "🖼 Photo", "video": "🎬 Video", "document": "📄 File"}

# Telegram captions (HTML) are limited to 1024 chars — keep a safety budget.
MAX_MEDIA_CAPTION_CHARS = 1000

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,64}$")

# --- Persistent stores (JSON files; loaded once, saved on every change) ------
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

# Per-sender recipient history limits ("recent/frequent", most recent first).
HISTORY_LIMIT = 15                      # stored recipients per sender
RECIPIENT_SUGGESTION_LIMIT = 8          # suggestions shown (no media pending)
RECIPIENT_SUGGESTION_MEDIA_LIMIT = 3    # recipients shown with media (x6 modes)

# Group chats where a mute-deletion failed (missing rights) — skip retries
# until restart so we never hammer the API in chats we can't moderate in.
_gmute_failed_chats: set = set()

_used_whisper_ids: set = set()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def generate_whisper_id() -> str:
    """Unique whisper ID such as 'W-D0579C' (displayed as '#W-D0579C'). Never reused."""
    while True:
        whisper_id = "W-" + secrets.token_hex(3).upper()
        if whisper_id not in _used_whisper_ids:
            _used_whisper_ids.add(whisper_id)
            return whisper_id


def format_time(ts: Optional[float] = None) -> str:
    """'03 Oct 2026, 07:25 PM' style timestamps."""
    dt = datetime.fromtimestamp(ts) if ts is not None else datetime.now()
    return dt.strftime("%d %b %Y, %I:%M %p")


def fmt_display_name(name: Optional[str], username: Optional[str]) -> str:
    """HTML-escaped 'Name (@username)' / 'Name'."""
    safe_name = html.escape(name or "Unknown")
    if username:
        return f"{safe_name} (@{html.escape(username)})"
    return safe_name


def normalize_username(raw: Optional[str]) -> str:
    """
    '@Name ' -> 'name'  (remove @, trim whitespace, lowercase).

    THE single normalization used BOTH when storing a username target and
    when comparing it against the button presser's Telegram username, so the
    two can never diverge.
    """
    return (raw or "").strip().lstrip("@").lower()


def _log_username(username: Optional[str]) -> str:
    """'@name' or the literal 'None' for the log channel."""
    return f"@{username}" if username else "None"


def _html_fit(raw: Optional[str], budget: int) -> str:
    """HTML-escape user text and keep it within `budget` chars (for captions)."""
    esc = html.escape(raw or "")
    return esc if len(esc) <= budget else esc[: budget - 1] + "…"


def parse_inline_query(raw: str) -> Tuple[Optional[str], Optional[int], str]:
    """
    Split '@BotName <whisper text> <target>' into (target_username, target_id, text).

    The target is the LAST token and may be:
      - '@Username'  -> returned as (username_as_typed, None, text)
      - '123456789'  -> returned as (None, 123456789, text)
    If the last token is neither, the whole query is treated as text with no
    target (the caller may then show recipient suggestions / the format hint).
    """
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
# State access (bookkeeping only — whisper targets are NEVER resolved here)
# ---------------------------------------------------------------------------

def get_sessions(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Dict[str, Any]]:
    """All whisper sessions (module-level store shared with the Flask API)."""
    return WHISPER_SESSIONS


def get_users(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Dict[str, Any]]:
    return context.application.bot_data.setdefault(USERS_KEY, {})


def get_pending_media(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Dict[str, Any]]:
    return context.application.bot_data.setdefault(PENDING_MEDIA_KEY, {})


def register_user(context: ContextTypes.DEFAULT_TYPE, user: Any) -> None:
    """
    Store/update a user in the bookkeeping registry (Telegram ID, username,
    display name). All data comes straight from Telegram updates — never from
    user-typed text.

    IMPORTANT: this registry is NOT used for whisper target resolution or
    authorization. Whisper targets are stored exactly as the sender typed
    them and authorized via the button press (see is_target_user).
    """
    if user is None:
        return
    get_users(context)[str(user.id)] = {
        "id": user.id,
        "username": user.username,
        "name": user.first_name or user.full_name or "Unknown",
    }


def prune_sessions(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop whisper sessions and reader tokens past their expiration time."""
    now = time.time()
    with _whisper_lock:
        for wid in [wid for wid, s in WHISPER_SESSIONS.items() if now > s.get("expires_at", 0)]:
            WHISPER_SESSIONS.pop(wid, None)
        for tk in [t for t, e in _webapp_tokens.items() if now > e["expires_at"]]:
            _webapp_tokens.pop(tk, None)


# ---------------------------------------------------------------------------
# Whisper authorization — the ONE rule (text, media AND the Mini App reader)
# ---------------------------------------------------------------------------

def is_target_user(session: Dict[str, Any], user: Any) -> bool:
    """
    The single whisper-authorization rule, based ONLY on the stored target.

      - user_id target: exact Telegram user ID match.
      - username target: normalized comparison against the username Telegram
        reports (callback presser OR verified initData user — both come from
        Telegram itself, never from user-typed text).

    The sender has NO special status: they pass only if they are the target.
    """
    if session["target_type"] == "user_id":
        return user.id == session["target_user_id"]
    presser = normalize_username(getattr(user, "username", None))
    return bool(presser) and presser == session["target_username"]


# ---------------------------------------------------------------------------
# Owner / trusted-auth checks
# ---------------------------------------------------------------------------

def is_owner(user_id: int) -> bool:
    """Bot owners (config.OWNER_IDS). Cannot be removed via /rauth."""
    return user_id in OWNER_IDS


def is_authorized(user_id: int) -> bool:
    """Owners plus the persisted trusted/auth list — gates /gmute and /gunmute."""
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
    """Stable identity of a stored recipient (never user-typed free text)."""
    if entry.get("target_type") == "user_id":
        return f"u{entry.get('user_id')}"
    return "@" + (entry.get("username") or "")


def _history_display(context: ContextTypes.DEFAULT_TYPE, session: Dict[str, Any]) -> str:
    """Friendly display for the suggestion title ('Ayaan', '@Rahul', 'user ID 123')."""
    if session["target_type"] == "user_id":
        known = get_users(context).get(str(session.get("target_user_id")))
        if known and known.get("name"):
            return str(known["name"])
        return f"user ID {session.get('target_user_id')}"
    return session["target_display"]  # '@Username' exactly as typed


def remember_recipient(
    context: ContextTypes.DEFAULT_TYPE, sender_id: int, session: Dict[str, Any]
) -> None:
    """
    Record/update the recipient in the SENDER's private history.

    Called from the successful whisper-creation path (ChosenInlineResult) for
    BOTH manual and suggestion-selected recipients, so positions update on
    every successful use. Never raises; persistence failures are logged.
    """
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
        del hist[HISTORY_LIMIT:]  # cap the stored history
        _persist_history()
    except Exception:
        logger.exception("Failed to record recipient history for sender %s", sender_id)


# ---------------------------------------------------------------------------
# Message builders (all user input is HTML-escaped)
# ---------------------------------------------------------------------------

def build_whisper_card(session: Dict[str, Any]) -> str:
    """The public card posted into the chat. Contains NO whisper text, NO
    media references — only the EXACT target the sender entered."""
    if session["target_type"] == "user_id":
        target = f"user ID <b>{session['target_user_id']}</b>"
    else:
        target = "<b>" + html.escape(session["target_display"]) + "</b>"
    return (
        f"🔐 A whisper message to {target}.\n"
        "Only they can read the message.\n\n"
        "　　🔐"
    )


def build_reveal_dm(session: Dict[str, Any]) -> str:
    """Private message with the full whisper, sent only to the verified target."""
    sender = fmt_display_name(session["sender_name"], session["sender_username"])
    return (
        f"🤫 <b>Whisper #{session['whisper_id']}</b>\n\n"
        f"👤 <b>From:</b> {sender}\n"
        f"🕐 <b>Time:</b> {format_time(session['created_at'])}\n\n"
        f"💬 <b>Whisper:</b>\n{html.escape(session['text'])}"
    )


def build_media_caption(session: Dict[str, Any]) -> str:
    """Caption for the privately delivered media (verified target only)."""
    sender = fmt_display_name(session["sender_name"], session["sender_username"])
    header = f"🤫 <b>Whisper #{html.escape(session['whisper_id'])}</b>\n👤 From: {sender}"

    body_parts = []
    if session.get("text"):
        body_parts.append("💬 " + _html_fit(session["text"], 600))
    if session.get("media_caption"):
        body_parts.append("📎 " + _html_fit(session["media_caption"], 100))

    mode = session.get("view_mode") or "normal"
    if mode == "once":
        footer = "👁 <i>Once view — this media will disappear shortly and cannot be opened again.</i>"
    elif mode in MEDIA_VIEW_DELETE_DELAYS:
        footer = (
            f"⏳ <i>This media will be deleted in "
            f"{MEDIA_VIEW_DELETE_DELAYS[mode]} seconds.</i>"
        )
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


def build_log_text(session: Dict[str, Any]) -> str:
    """Complete moderation log — sent ONLY to the private log channel.

    Target is the EXACT string the sender entered: '@ObsessHolic' stays
    '@ObsessHolic', '123456789' stays '123456789'. No resolution required.
    """
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
    """Caption attached to the media copy sent to the log channel."""
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
# Media send helper (Telegram file IDs only — no downloads, no URLs in logs)
# ---------------------------------------------------------------------------

async def _send_media(
    bot: Any, chat_id: int, media: Dict[str, Any], caption: str
) -> Message:
    """Send the stored media (by Telegram file_id) with an HTML caption."""
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
    return await bot.send_document(
        chat_id=chat_id, document=media["file_id"], caption=caption,
        parse_mode=ParseMode.HTML,
    )


def schedule_media_delete(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    delay_seconds: int,
    whisper_id: str,
) -> None:
    """
    Schedule deletion of a delivered media message on the bot's own event
    loop (no extra dependency). Failure to delete never affects the whisper.
    """
    logger.info(
        "MEDIA WHISPER DELETE REQUESTED: whisper_id=%s delay=%ss",
        whisper_id, delay_seconds,
    )

    async def _delete_task() -> None:
        await asyncio.sleep(delay_seconds)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            logger.info("MEDIA WHISPER DELETED: whisper_id=%s", whisper_id)
        except TelegramError as exc:
            logger.warning(
                "MEDIA WHISPER DELETE FAILED (whisper continues): whisper_id=%s error=%s",
                whisper_id, exc,
            )
        except Exception:
            logger.exception(
                "MEDIA WHISPER DELETE FAILED (unexpected): whisper_id=%s", whisper_id
            )

    context.application.create_task(_delete_task())


# ---------------------------------------------------------------------------
# THE centralized whisper-log sender (exactly one function, exactly one log)
# ---------------------------------------------------------------------------

async def send_whisper_log(context: ContextTypes.DEFAULT_TYPE, session: Dict[str, Any]) -> None:
    """
    THE centralized whisper-log sender. Called from the real whisper-creation
    path (on_chosen_inline_result) the moment the card is posted into a chat.

    - Exactly-once: the session 'logged' flag guarantees a single whisper can
      never generate two log messages, no matter how many times it's called.
    - Never raises: a logging failure can never break a whisper.
    - Failures print the FULL exception under "WHISPER LOG SEND FAILED".
    - Media whispers additionally send the EXACT original media (photo/video/
      document) to LOG_CHANNEL — best-effort, never breaking the whisper.
    """
    wid = session["whisper_id"]

    if session.get("logged"):
        return  # Exactly-once guard — this whisper was already logged.

    if LOG_CHANNEL_DEST is None:
        logger.warning("Whisper #%s NOT logged — %s", wid, log_channel_problem())
        return

    logger.info(
        "Sending whisper log: whisper_id=%s target=%s", wid, session["target_display"]
    )
    try:
        await context.bot.send_message(
            chat_id=LOG_CHANNEL_DEST,
            text=build_log_text(session),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        # Forbidden / chat not found / network — full traceback in Render logs,
        # never silently swallowed, and the whisper itself is unaffected.
        logger.exception("WHISPER LOG SEND FAILED: whisper_id=%s", wid)
        return  # 'logged' stays unset, so a retry remains possible.

    session["logged"] = True
    logger.info("Whisper log sent successfully: whisper_id=%s", wid)

    if session.get("media"):
        await send_media_log_attachment(context, session)


async def send_media_log_attachment(
    context: ContextTypes.DEFAULT_TYPE, session: Dict[str, Any]
) -> None:
    """Send the EXACT original media to LOG_CHANNEL (best-effort, never raises)."""
    wid = session["whisper_id"]
    try:
        await _send_media(
            context.bot, LOG_CHANNEL_DEST, session["media"],
            build_log_media_caption(session),
        )
        logger.info("MEDIA WHISPER LOG SENT: whisper_id=%s", wid)
    except Exception:
        # Full traceback in Render logs; the whisper flow is unaffected.
        logger.exception("MEDIA WHISPER LOG FAILED: whisper_id=%s", wid)


# ---------------------------------------------------------------------------
# MINI APP LONG-WHISPER READER — tokens, initData validation, open API
# ---------------------------------------------------------------------------

def issue_webapp_token(wid: str) -> str:
    """
    Issue a short-lived, cryptographically random reader access token that
    maps SERVER-SIDE to this whisper. The token contains no whisper/target
    IDs and is useless without a valid Telegram initData.
    """
    now = time.time()
    with _whisper_lock:
        for tk in [t for t, e in _webapp_tokens.items() if now > e["expires_at"]]:
            _webapp_tokens.pop(tk, None)
        token = secrets.token_urlsafe(32)
        _webapp_tokens[token] = {"wid": wid, "expires_at": now + WEBAPP_TOKEN_TTL_SECONDS}
    logger.info(
        "WEBAPP READER TOKEN ISSUED: whisper_id=%s ttl=%ss", wid, WEBAPP_TOKEN_TTL_SECONDS
    )
    return token


def validate_webapp_init_data(init_data: str) -> Optional[SimpleNamespace]:
    """
    Validate Telegram Mini App initData using the OFFICIAL algorithm:

        secret_key        = HMAC_SHA256(key="WebAppData", message=BOT_TOKEN)
        data_check_string = all initData fields except 'hash',
                            sorted alphabetically, joined as 'k=v' with '\\n'
        calculated_hash   = HMAC_SHA256(key=secret_key, message=data_check_string)

    plus an auth_date freshness check. On success returns a SimpleNamespace
    (id, username, name) taken from the VERIFIED user object; None otherwise.
    The identity NEVER comes from URL parameters or client-typed text.
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
        logger.warning("WEBAPP INITDATA REJECTED: HMAC mismatch.")
        return None

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if auth_date <= 0 or time.time() - auth_date > WEBAPP_INITDATA_MAX_AGE_SECONDS:
        logger.warning("WEBAPP INITDATA REJECTED: auth_date too old/invalid.")
        return None

    try:
        user = json.loads(pairs.get("user", "{}"))
        uid = int(user["id"])
    except Exception:
        logger.warning("WEBAPP INITDATA REJECTED: missing/invalid user payload.")
        return None

    return SimpleNamespace(
        id=uid,
        username=user.get("username"),
        name=user.get("first_name") or user.get("full_name") or "Unknown",
    )


def open_whisper_via_webapp(token: str, init_data: str) -> Dict[str, Any]:
    """
    THE Mini App reader API (called synchronously by the Flask thread).

    Order of checks (all server-side):
      1. Access token exists and is not expired.
      2. Telegram initData is cryptographically valid -> VERIFIED user.
      3. Verified user matches the stored whisper target (is_target_user).
      4. Whisper not expired (15-minute TTL).
      5. View-mode state (once/timed/normal) enforced atomically.

    Returns a plain dict; it NEVER includes whisper IDs, target IDs or any
    data to unauthorized callers.
    """
    now = time.time()
    token = (token or "").strip()

    with _whisper_lock:
        entry = _webapp_tokens.get(token)
        if entry is None or now > entry["expires_at"]:
            _webapp_tokens.pop(token, None)
            return {"status": "invalid_token"}
        wid = entry["wid"]

    verified = validate_webapp_init_data(init_data)
    if verified is None:
        return {"status": "unauthorized"}

    with _whisper_lock:
        session = WHISPER_SESSIONS.get(wid)
        if session is None:
            return {"status": "whisper_expired"}
        if now > session.get("expires_at", 0):
            WHISPER_SESSIONS.pop(wid, None)
            return {"status": "whisper_expired"}

        # SECURITY: target identity check with the VERIFIED Telegram id only.
        if not is_target_user(session, verified):
            logger.info(
                "WEBAPP READER DENIED: whisper_id=%s user=%s", wid, verified.id
            )
            return {"status": "denied"}

        # Media whispers use the existing chat delivery flow, not the reader.
        if session.get("media"):
            return {"status": "media"}

        mode = session.get("view_mode") or "normal"
        state = session.get("delivery_state")
        remaining: Optional[int] = None
        first_reveal = not session.get("opened_logged")

        if mode == "once":
            if state == "consumed":
                return {"status": "already_viewed"}
            # ATOMIC check-and-set under the lock — the server enforces once.
            session["delivery_state"] = "consumed"
        elif mode in MEDIA_VIEW_DELETE_DELAYS:
            delay = MEDIA_VIEW_DELETE_DELAYS[mode]
            if state == "consumed":
                return {"status": "timed_expired"}
            if state == "timed":
                consumes_at = session.get("consumes_at", 0)
                if now >= consumes_at:
                    session["delivery_state"] = "consumed"
                    return {"status": "timed_expired"}
                remaining = int(consumes_at - now)
            else:
                # Timer starts at the FIRST successful authorized reveal.
                session["revealed_at"] = now
                session["consumes_at"] = now + delay
                session["delivery_state"] = "timed"
                remaining = delay
        else:  # normal
            session["delivery_state"] = "delivered"

        if first_reveal:
            session["opened_logged"] = True

        payload = {
            "status": "ok",
            "text": session["text"],
            "mode": mode,
            "mode_label": MEDIA_VIEW_LABELS.get(mode, "Normal"),
            "remaining": remaining,
            "sender": (
                (session.get("sender_name") or "Unknown")
                + (f" (@{session['sender_username']})" if session.get("sender_username") else "")
            ),
            "created": format_time(session.get("created_at")),
        }

    logger.info(
        "WEBAPP READER OPENED: whisper_id=%s mode=%s user=%s", wid, mode, verified.id
    )
    if first_reveal:
        _notify_opened(session, verified, wid)
    return payload


def _notify_opened(session: Dict[str, Any], verified: Any, wid: str) -> None:
    """Send a one-time 'WHISPER OPENED' entry to the private log channel.

    The log text is built synchronously in the calling (Flask) thread and the
    actual send is scheduled on the bot's event loop — never blocks Flask and
    never contains the whisper text.
    """
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
    """Deliver the 'WHISPER OPENED' log text to LOG_CHANNEL (best-effort)."""
    bot = _bot_ref
    if bot is None:
        return
    try:
        await bot.send_message(
            chat_id=LOG_CHANNEL_DEST, text=log_text, parse_mode=ParseMode.HTML
        )
        logger.info("WHISPER OPENED LOG SENT: whisper_id=%s", wid)
    except Exception:
        logger.exception("WHISPER OPENED LOG FAILED: whisper_id=%s", wid)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Bookkeeping registry update (NOT required for whisper targets).
    user = update.effective_user
    if user is not None:
        register_user(context, user)

    if update.message is None:
        return

    # --- Deep-link claim: t.me/<bot>?start=m<whisper_id> ----------------------
    # Used by the "▶️ Start Whispry" button when a media target had not
    # started the bot yet. After starting, the recipient is re-verified
    # (same is_target_user rule) and the pending media is delivered here.
    if context.args:
        payload = context.args[0]
        if len(payload) > 1 and payload[0] == "m":
            await handle_media_start_claim(update, context, payload[1:])
            return

    bot_username = context.bot.username
    text = (
        "🔐 <b>Whispry — Inline Whisper Bot</b>\n\n"
        "Send private whispers in any chat. The target never needs to have "
        "started this bot. 🤫\n\n"
        "📌 <b>How to send a whisper</b>\n"
        "1️⃣ In any chat, type (the recipient comes LAST):\n"
        f"    <code>@{bot_username} your secret message @username</code>\n"
        "    or with a Telegram user ID:\n"
        f"    <code>@{bot_username} your secret message 123456789</code>\n"
        "2️⃣ Tap the whisper result.\n"
        "3️⃣ A locked card is posted in the chat:\n\n"
        "    🔐 A whisper message to @username.\n"
        "    Only they can read the message.\n\n"
        "4️⃣ Only the target can open it with 🔐 — verified by their Telegram "
        "account. Everyone else — including the sender — sees “❌ This whisper "
        "isn't for you.”\n\n"
        "📖 <b>Private reader</b> — long whispers open in a full-screen, "
        "scrollable Whispry reader (Mini App) that only the verified target "
        "can unlock.\n\n"
        "🕐 <b>Recent recipients</b> — after you've whispered to someone, they "
        "appear as tappable suggestions whenever you don't type a target.\n\n"
        "🖼 <b>Media whispers</b> — send a photo, video or file to my private "
        "chat first, then whisper as usual (see /help).\n\n"
        "Type /help for details, or /game to play. 🎮"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def handle_media_start_claim(
    update: Update, context: ContextTypes.DEFAULT_TYPE, wid: str
) -> None:
    """
    Recipient pressed Start via the media deep-link (?start=m<whisper_id>).

    Re-verify that they are the intended target (same ONE authorization rule),
    then deliver the pending media whisper privately. Never reveal anything
    to a non-target, and never deliver consumed/expired media.
    """
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return

    session = get_sessions(context).get(wid)

    if (
        session is None
        or not session.get("media")
        or time.time() > session.get("expires_at", 0)
        or session.get("delivery_state") == "consumed"
    ):
        await message.reply_text(
            "🔒 This media whisper has expired or has already been viewed."
        )
        return

    if not is_target_user(session, user):
        await message.reply_text("❌ This whisper isn't for you.")
        return

    logger.info(
        "MEDIA WHISPER DELIVERY REQUESTED (after start): whisper_id=%s user=%s",
        wid, user.id,
    )
    try:
        delivered = await _send_media(
            context.bot, message.chat_id, session["media"],
            build_media_caption(session),
        )
    except Forbidden:
        await message.reply_text(
            "⚠️ I still can't send you the media. Open the whisper card and "
            "press 🔐 again in a moment."
        )
        return
    except TelegramError as exc:
        logger.warning(
            "MEDIA WHISPER DELIVERY FAILED after start (retry possible): "
            "whisper_id=%s error=%s",
            wid, exc,
        )
        await message.reply_text(
            "⚠️ The media could not be delivered right now. Press 🔐 on the "
            "whisper card again in a moment."
        )
        return

    _finalize_media_delivery(context, session, message.chat_id, delivered.message_id)
    await message.reply_text("🔓 The media whisper was delivered above. 🤫")


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
        "a target — tap one to target them instantly, or pick “➕ New recipient”.\n"
        "• The target does NOT need to have started the bot — the whisper card is "
        "posted in the chat and they unlock it with the 🔐 button.\n"
        "• The whisper text is never shown in the chat.\n\n"
        "📖 <b>Reading a whisper (private reader)</b>\n"
        "• Press 🔐 on the card — you'll get a private message with an "
        "“Open Private Reader” button.\n"
        "• The reader is a full-screen, scrollable Mini App — perfect for very "
        "long whispers.\n"
        "• It verifies your Telegram identity on the server before showing "
        "anything; nobody else can open your whisper.\n"
        "• Text whispers support view modes too: 🔓 Normal, ⏳ 3s/5s/10s/30s "
        "(auto-locks after the countdown) and 👁 Once view.\n\n"
        "🖼 <b>Media whispers (photo / video / file)</b>\n"
        "1️⃣ Send a photo, video or document to my private chat (caption optional). "
        "I store only its Telegram file ID — never exposed publicly.\n"
        "2️⃣ Type your whisper + target in any chat — you'll get six results:\n"
        "    🔓 Normal · ⏳ 3s · ⏳ 5s · ⏳ 10s · ⏳ 30s · 👁 Once view\n"
        "3️⃣ Tap one — the locked card is posted; the media is delivered only to "
        "the target's private chat when they press 🔐. Recent recipients work here "
        "too.\n"
        "• Timed and once-view media are deleted automatically after delivery and "
        "can be opened only once.\n"
        "• If the target hasn't started me yet, they'll get a ▶️ Start Whispry "
        "button — after starting, the media is delivered to them.\n\n"
        "🎮 <b>Game</b>\n"
        "• Send /game to open the mini app game right inside Telegram.\n\n"
        "ℹ️ <b>Good to know</b>\n"
        f"• A whisper needs text — up to {WHISPER_MAX_LENGTH} characters.\n"
        f"• Whispers expire after {SESSION_TTL_SECONDS // 60} minutes — expired cards "
        "show “🔒 This whisper has expired.”\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the Telegram Mini App / Web App game via a web_app button."""
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
            "Telegram rejected the Web App button (%s). Falling back to a normal URL button.",
            exc,
        )
    except Forbidden:
        return  # Bot can't post here (e.g. blocked) — nothing to do.

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
    """
    Resolve the target Telegram user ID for /gmute, /gunmute, /addauth, /rauth.

    Supported formats:
      1. /command 123456789             -> numeric user ID argument
      2. Reply to a message + /command  -> targets that message's author.
         Works even with privacy mode ON: Telegram attaches reply_to_message
         (including its from_user) to the command update itself.

    Returns None if neither is present (caller shows usage). @username args
    are deliberately NOT supported (no fake Bot API username lookups).
    """
    if context.args:
        raw = context.args[0].strip()
        return int(raw) if raw.isdigit() else None
    message = update.message
    if message is not None and message.reply_to_message is not None:
        replied = message.reply_to_message.from_user
        if replied is not None:
            register_user(context, replied)  # remember name/username for confirmations
            return replied.id
    return None


# ---------------------------------------------------------------------------
# Global GMUTE commands (owner or trusted auth list only)
# ---------------------------------------------------------------------------

async def cmd_gmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Globally mute a user (owner or trusted auth list only) — by ID or reply."""
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
    """Remove a user from the global mute list (owner or trusted auth list only)."""
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
    """
    Fast-path for GLOBAL GMUTE (runs in PTB handler group 1): delete every new
    group message from a globally muted user. Normal messages are untouched
    (this returns immediately unless the sender is muted).
    """
    message = update.message
    if message is None:
        return
    sender = message.from_user
    if sender is None or sender.id not in GMUTE_USERS:
        return
    if message.chat.id in _gmute_failed_chats:
        return  # We already know we lack delete rights in this chat.

    try:
        await context.bot.delete_message(
            chat_id=message.chat.id, message_id=message.message_id
        )
        logger.info(
            "GMUTE MESSAGE DELETED: user=%s chat=%s", sender.id, message.chat.id
        )
    except Forbidden as exc:
        # No delete permission here — remember it so we stop retrying.
        _gmute_failed_chats.add(message.chat.id)
        logger.warning(
            "GMUTE: cannot delete messages in chat %s (missing permission) — "
            "skipping this chat until restart. Error: %s",
            message.chat.id, exc,
        )
    except BadRequest as exc:
        # Could be transient (e.g. already deleted) — log, keep trying later.
        logger.warning(
            "GMUTE: delete failed in chat %s: %s", message.chat.id, exc
        )
    except TelegramError as exc:
        logger.error(
            "GMUTE: Telegram error deleting message in chat %s: %s",
            message.chat.id, exc,
        )


# ---------------------------------------------------------------------------
# Owner-only trusted-auth management
# ---------------------------------------------------------------------------

async def cmd_addauth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a user to the trusted/auth list — OWNER_IDS only, by ID or reply."""
    if update.message is None:
        return
    user = update.effective_user
    if user is not None:
        register_user(context, user)
    if user is None or not is_owner(user.id):
        await update.message.reply_text(
            "❌ Only the bot owner can manage the trusted list."
        )
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
    """Remove a user from the trusted list — OWNER_IDS only, owners immune."""
    if update.message is None:
        return
    user = update.effective_user
    if user is not None:
        register_user(context, user)
    if user is None or not is_owner(user.id):
        await update.message.reply_text(
            "❌ Only the bot owner can manage the trusted list."
        )
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
# Private chat — media intake for MEDIA WHISPERS (unchanged)
# ---------------------------------------------------------------------------

async def on_private_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    A sender sent a photo / video / document to the bot's private chat.

    The media is stored as Telegram file_id/file_unique_id ONLY (never
    downloaded, never exposed publicly) and becomes 'pending media' for this
    sender. Their next inline whisper attaches it automatically.
    """
    user = update.effective_user
    if user is not None:
        register_user(context, user)

    message = update.message
    if message is None:
        return

    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id            # largest size
        file_unique_id = message.photo[-1].file_unique_id
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id
        file_unique_id = message.video.file_unique_id
    elif message.document:
        media_type = "document"
        file_id = message.document.file_id
        file_unique_id = message.document.file_unique_id
    else:
        return  # Unsupported media type — ignore silently.

    caption = (message.caption or "").strip() or None
    get_pending_media(context)[str(user.id)] = {
        "type": media_type,
        "file_id": file_id,
        "file_unique_id": file_unique_id,
        "caption": caption,
    }
    logger.info(
        "MEDIA STORED (pending): user=%s type=%s has_caption=%s",
        user.id, media_type, bool(caption),
    )

    icon = MEDIA_ICONS[media_type]
    caption_note = (
        "\n✍️ The caption you wrote will be delivered together with the media."
        if caption
        else ""
    )
    await message.reply_text(
        f"✅ <b>{icon} attached!</b> 🔐\n\n"
        "Your media is stored privately (Telegram file ID only — it is never "
        "exposed publicly)." + caption_note + "\n\n"
        "📌 <b>Now send the whisper</b>\n"
        "1️⃣ Open any chat and type (recipient LAST):\n"
        f"    <code>@{context.bot.username} your message @target</code>\n"
        f"    <code>@{context.bot.username} your message 123456789</code>\n"
        "2️⃣ Pick a view mode:\n"
        "    🔓 Normal · ⏳ 3s · ⏳ 5s · ⏳ 10s · ⏳ 30s · 👁 Once view\n"
        "3️⃣ Tap the result — the locked card is posted and only the target can "
        "open the media with 🔐. Recent recipients work here too.\n\n"
        "ℹ️ The media is delivered only to the target's private chat — never "
        "into the public chat. Send another photo/video/file to replace this one.",
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
                "🖼 Media whisper? Send a photo/video/file to my private chat first.\n\n"
                "A locked whisper card is posted in the chat — only the target can "
                "open it with the 🔐 button. 🤫"
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
    """The permanent '➕ New recipient' option (keeps the manual syntax alive)."""
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
) -> Dict[str, Any]:
    """
    Create + store a whisper session (the single session factory used by the
    manual inline path AND the recent-recipient suggestion path).
    """
    wid = generate_whisper_id()
    now = time.time()
    session: Dict[str, Any] = {
        "whisper_id": wid,
        "sender_id": user.id,
        "sender_name": user.first_name or user.full_name or "Unknown",
        "sender_username": user.username,
        "target_type": target_type,            # "username" | "user_id"
        "target_display": target_display,      # exact target string for the card
        "target_username": target_username_norm,  # normalized, username targets only
        "target_user_id": target_user_id,      # numeric targets only
        "text": whisper_text,
        "status": "created",
        "created_at": now,
        "expires_at": now + SESSION_TTL_SECONDS,
        "posted_at": None,
        "logged": False,
        "media": media,           # {"type","file_id","file_unique_id"} or None
        "media_caption": media_caption,
        "view_mode": None,        # set on selection: normal/3s/5s/10s/30s/once
        "delivery_state": None,   # None | "delivered" | "timed" | "consumed"
        "revealed_at": None,      # timed text whispers: first authorized reveal
        "consumes_at": None,      # timed text whispers: lock deadline (server)
        "opened_logged": False,   # one-time WHISPER OPENED log flag
    }
    get_sessions(context)[wid] = session
    return session


def build_inline_result(
    session: Dict[str, Any],
    title: str,
    description: str,
    mode: Optional[str] = None,
) -> InlineQueryResultArticle:
    """
    The single inline-result factory: secure text-only card + 🔐 button whose
    callback_data contains ONLY the whisper ID ("whisper:W-XXXXXX"). The mode
    is encoded in the result id ("<wid>|<mode>") for view-mode choices.
    """
    rid = f"{session['whisper_id']}|{mode}" if mode else session["whisper_id"]
    return InlineQueryResultArticle(
        id=rid,
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=build_whisper_card(session), parse_mode=ParseMode.HTML
        ),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(text="🔐", callback_data=f"whisper:{session['whisper_id']}")]]
        ),
    )


def _build_recipient_results(
    context: ContextTypes.DEFAULT_TYPE, user: Any, whisper_text: str
) -> Optional[List[InlineQueryResultArticle]]:
    """
    Recent-recipient suggestions for THIS sender only (never anyone else's).

    Each suggestion is a REAL whisper session created at query time, so the
    tapped result flows through the existing chosen-inline pipeline (posting,
    logging, media modes, 🔐 verification) unchanged. Result ids reference
    only sessions we just created for this user — nothing user-controlled is
    ever trusted as the recipient identity.

    Text suggestions remain single normal-mode results (existing behavior);
    with pending media the six view modes are offered per recipient.

    Returns None when the sender has no history (caller falls back to the
    format hint).
    """
    entries = RECIPIENT_HISTORY.get(str(user.id)) or []
    if not entries:
        return None

    pending = get_pending_media(context).get(str(user.id))
    results: List[InlineQueryResultArticle] = []

    if pending:
        # Media pending: top recipients x the six view modes.
        media = {
            "type": pending["type"],
            "file_id": pending["file_id"],
            "file_unique_id": pending["file_unique_id"],
        }
        for entry in entries[:RECIPIENT_SUGGESTION_MEDIA_LIMIT]:
            if entry.get("target_type") == "user_id":
                session = create_whisper_session(
                    context, user, whisper_text, "user_id",
                    str(entry.get("user_id")), None, entry.get("user_id"),
                    media=media, media_caption=pending.get("caption"),
                )
            else:
                uname = entry.get("username") or ""
                session = create_whisper_session(
                    context, user, whisper_text, "username",
                    "@" + uname, uname, None,
                    media=media, media_caption=pending.get("caption"),
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
                    )
                )
    else:
        for entry in entries[:RECIPIENT_SUGGESTION_LIMIT]:
            plain = entry.get("display") or "?"
            if entry.get("target_type") == "user_id":
                session = create_whisper_session(
                    context, user, whisper_text, "user_id",
                    str(entry.get("user_id")), None, entry.get("user_id"),
                )
            else:
                uname = entry.get("username") or ""
                session = create_whisper_session(
                    context, user, whisper_text, "username",
                    "@" + uname, uname, None,
                )
            results.append(
                build_inline_result(
                    session,
                    title=f"🔐 A whisper message to {plain}",
                    description=f"Only {plain} can open this whisper — tap to post",
                )
            )

    results.append(_new_recipient_result(context))
    return results


async def on_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = update.inline_query
    if inline_query is None:
        return
    user = inline_query.from_user

    # Bookkeeping registry update (NOT used for whisper targets/auth).
    register_user(context, user)
    prune_sessions(context)

    # 1) Parse whisper text + 2) parse target (manual syntax — unchanged).
    target_username, target_id, whisper_text = parse_inline_query(inline_query.query)
    whisper_text = (whisper_text or "").strip()

    # Empty whisper -> friendly format hint (unchanged).
    if not whisper_text:
        try:
            await inline_query.answer(
                results=[_format_hint_result(context)], cache_time=1, is_personal=True
            )
        except TelegramError as exc:
            logger.error("Failed to answer inline query: %s", exc)
        return

    # Whisper that is too long -> hint as well (unchanged).
    if len(whisper_text) > WHISPER_MAX_LENGTH:
        try:
            await inline_query.answer(
                results=[_too_long_result()], cache_time=1, is_personal=True
            )
        except TelegramError as exc:
            logger.error("Failed to answer inline query: %s", exc)
        return

    # --- NO explicit target -> recent/frequent recipient suggestions ---------
    # (An ADDITION: manual '@username' / numeric-ID targeting below is
    # untouched and takes priority whenever a target IS typed.)
    if target_username is None and target_id is None:
        suggestion_results = _build_recipient_results(context, user, whisper_text)
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
        # No history yet -> existing format hint.
        try:
            await inline_query.answer(
                results=[_format_hint_result(context)], cache_time=1, is_personal=True
            )
        except TelegramError as exc:
            logger.error("Failed to answer inline query: %s", exc)
        return

    # --- Manual target path (existing flow, unchanged) ------------------------
    # Store the target EXACTLY as entered (no resolution of any kind).
    if target_username is not None:
        # Local comparison against the bot's own username only (no Bot API call).
        if normalize_username(target_username) == (context.bot.username or "").lower():
            try:
                await inline_query.answer(
                    results=[_bot_target_result()], cache_time=1, is_personal=True
                )
            except TelegramError as exc:
                logger.error("Failed to answer inline query: %s", exc)
            return
        target_type = "username"
        target_display = "@" + target_username          # exact as typed
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
        target_display = str(target_id)                  # exact as typed
        target_username_norm = None
        target_user_id = int(target_id)

    # 3) Create unique whisper ID + 4) store whisper data.
    pending = get_pending_media(context).get(str(user.id))
    if pending:
        session = create_whisper_session(
            context, user, whisper_text, target_type, target_display,
            target_username_norm, target_user_id,
            media={
                "type": pending["type"],
                "file_id": pending["file_id"],
                "file_unique_id": pending["file_unique_id"],
            },
            media_caption=pending.get("caption"),
        )
        logger.info(
            "MEDIA WHISPER CREATED: whisper_id=%s target=%s media=%s",
            session["whisper_id"], session["target_display"], pending["type"],
        )
        results = [
            build_inline_result(
                session,
                title=f"{MEDIA_RESULT_TITLES[mode]} — to {session['target_display']}",
                description="Only the target can open it with 🔐",
                mode=mode,
            )
            for mode in MEDIA_VIEW_MODES
        ]
    else:
        session = create_whisper_session(
            context, user, whisper_text, target_type, target_display,
            target_username_norm, target_user_id,
        )
        logger.info(
            "INLINE WHISPER CREATED: whisper_id=%s target=%s",
            session["whisper_id"], session["target_display"],
        )
        # Text whispers support the same view modes via the Mini App reader
        # (Normal / 3s / 5s / 10s / 30s / Once). The public card is identical
        # for every mode — modes only affect the PRIVATE reading experience.
        results = [
            build_inline_result(
                session,
                title=f"{MEDIA_RESULT_TITLES[mode]} — to {session['target_display']}",
                description="Only they can read the message — tap to post",
                mode=mode,
            )
            for mode in MEDIA_VIEW_MODES
        ]

    # 5) Serve the public whisper card result(s); the actual logging happens in
    #    on_chosen_inline_result — the REAL creation/success moment, once.
    try:
        await inline_query.answer(results=results, cache_time=0, is_personal=True)
    except TelegramError as exc:
        logger.error("Failed to answer inline query: %s", exc)
        try:
            await inline_query.answer(results=[], cache_time=5)
        except TelegramError:
            pass


async def on_chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    THE whisper-creation handler (text, media, manual AND suggestion paths).
    Fires exactly when the sender taps an inline result and the public whisper
    card is posted into the chat.

    Result IDs: "<whisper_id>" (text suggestion) or "<whisper_id>|<view_mode>".
    The complete whisper log is sent here via send_whisper_log() — exactly once.
    The recipient is recorded in the sender's private history on every
    successful use (position updated for repeat recipients).
    """
    chosen = update.chosen_inline_result
    if chosen is None:
        return

    result_id = chosen.result_id or ""
    if "|" in result_id:
        wid, mode = result_id.split("|", 1)
        if mode not in MEDIA_VIEW_MODES:
            logger.warning(
                "Unknown view mode %r for whisper %s — using normal.", mode, wid
            )
            mode = "normal"
    else:
        wid, mode = result_id, None

    session = get_sessions(context).get(wid)
    if session is None:
        logger.warning(
            "Chosen inline result for unknown whisper id=%s (expired or lost).", wid
        )
        return

    # SECURITY: inline results only ever reach the user they were served to,
    # but verify anyway so a manipulated result can never post someone else's
    # prepared whisper (and never modify a stored recipient identity).
    if chosen.from_user is None or chosen.from_user.id != session["sender_id"]:
        logger.warning(
            "Chosen inline result id=%s selected by non-sender (ignored).", wid
        )
        return

    logger.info("INLINE WHISPER SELECTED: whisper_id=%s mode=%s", wid, mode or "text")

    if session.get("media"):
        session["view_mode"] = mode or "normal"
        # Consume the pending media so it is not attached to future whispers.
        get_pending_media(context).pop(str(chosen.from_user.id), None)
    elif mode is not None:
        # Text whisper with an explicit view mode (reader-enforced later).
        session["view_mode"] = mode

    if session.get("posted_at") is None:
        session["status"] = "posted"
        session["posted_at"] = time.time()
        logger.info("Whisper card posted to a chat: whisper_id=%s", wid)

    # 6) THE log call — directly in the successful whisper-creation path.
    await send_whisper_log(context, session)

    # Remember/update the recipient in THIS sender's private history
    # (manual and suggestion-selected recipients alike).
    remember_recipient(context, chosen.from_user.id, session)
    # 7) Success — the card is in the chat, the log has been sent (or its
    #    failure was fully printed without breaking the whisper).


# ---------------------------------------------------------------------------
# 🔐 Read button — verified reveal: Mini App reader (text) / DM delivery (media)
# ---------------------------------------------------------------------------

async def _safe_answer(query: CallbackQuery, text: str, alert: bool = True) -> None:
    """Answer a callback query; never raise (the query may simply be too old)."""
    try:
        await query.answer(text=text, show_alert=alert)
    except TelegramError as exc:
        logger.debug("Could not answer callback query: %s", exc)


async def reveal_whisper(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: Dict[str, Any]
) -> None:
    """
    CLASSIC reveal (fallback when the Mini App reader is disabled/unavailable):
    ephemeral popup for short whispers, private DM for long ones. Preserved
    exactly as before so existing behavior never regresses.
    """
    text = session["text"]

    # Preferred: ephemeral popup. Telegram shows callback answers (with
    # show_alert=True) to the user who pressed the button ALONE — nothing is
    # posted or edited in the chat. answerCallbackQuery allows max 200 chars.
    if len(text) <= CALLBACK_ANSWER_MAX:
        try:
            await query.answer(text=text, show_alert=True)
            return
        except TelegramError as exc:
            logger.debug("Popup reveal failed (%s); falling back to private DM.", exc)

    # Longer whispers (or popup failure): DM the VERIFIED presser's own user
    # ID — taken straight from the callback query, so it works for both
    # username targets (no stored ID) and numeric-ID targets.
    try:
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text=build_reveal_dm(session),
            parse_mode=ParseMode.HTML,
        )
        await _safe_answer(query, "📩 The whisper was opened in your private chat.")
    except Forbidden:
        # The verified target pressed the button but has never pressed Start
        # (or blocked the bot) — Telegram forbids the DM. Never reveal
        # anything in the chat; tell them how to unlock the full text.
        await _safe_answer(
            query,
            "📩 To read the full whisper: open my chat, press Start once, then press "
            "the 🔐 button again.",
        )
    except TelegramError as exc:
        logger.warning(
            "Could not privately reveal whisper #%s: %s", session["whisper_id"], exc
        )
        # Last resort: truncated popup — still visible only to the presser.
        await _safe_answer(query, text[: CALLBACK_ANSWER_MAX - 1] + "…")


async def open_text_reader(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: Dict[str, Any]
) -> None:
    """
    MINI APP reader flow for verified TEXT targets.

    The 🔐 press was already verified (is_target_user). We issue a short-lived
    random access token and DM the verified presser a private message with a
    REAL web_app button ("📖 Open Private Reader") — web_app buttons work in
    private bot chats (they are NOT permitted on inline messages, which is why
    the reader is delivered here instead of attached to the card).

    Fallbacks preserve existing behavior:
      - WEBAPP_URL not configured -> classic popup/DM reveal (reveal_whisper).
      - DM fails (never started) + short text -> classic popup reveal.
      - DM fails + long text -> "Start Whispry first" instruction (no reveal).
    """
    text = session["text"]

    if not WEBAPP_URL:
        await reveal_whisper(query, context, session)
        return

    token = issue_webapp_token(session["whisper_id"])
    reader_url = WEBAPP_URL.rstrip("/") + "/reader?t=" + token

    dm_text = (
        "🤫 <b>Whisper unlocked!</b>\n\n"
        "Tap the button below to read it in your private Whispry reader — "
        "full screen, scrollable, just for you.\n\n"
        "⏳ <i>This button is valid for a short time. If it expires, press 🔐 on "
        "the whisper card again.</i>"
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(text="📖 Open Private Reader", web_app=WebAppInfo(url=reader_url))]]
    )
    try:
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text=dm_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except Forbidden:
        # Verified target never started the bot -> DM impossible.
        if len(text) <= CALLBACK_ANSWER_MAX:
            try:
                await query.answer(text=text, show_alert=True)
                return
            except TelegramError:
                pass
        await _safe_answer(
            query,
            "📩 Start Whispry first (open my chat and press Start), then press 🔐 "
            "again to read this whisper privately.",
        )
        return
    except TelegramError as exc:
        logger.warning(
            "Reader DM failed for whisper %s: %s", session["whisper_id"], exc
        )
        if len(text) <= CALLBACK_ANSWER_MAX:
            try:
                await query.answer(text=text, show_alert=True)
                return
            except TelegramError:
                pass
        await _safe_answer(
            query, "⚠️ Could not open the reader right now — press 🔐 again in a moment."
        )
        return

    await _safe_answer(
        query, "📖 Check my private message — tap “Open Private Reader”.", alert=False
    )


async def offer_start_whispry(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: Dict[str, Any]
) -> None:
    """
    Media target pressed 🔐 but has never started the bot (DM -> Forbidden).

    NOTHING is revealed. Add a "▶️ Start Whispry" deep-link button to the
    public card; after starting via that link, the recipient is re-verified
    and the pending media is delivered privately.
    """
    wid = session["whisper_id"]
    url = f"https://t.me/{context.bot.username}?start=m{wid}"

    if query.inline_message_id:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(text="🔐", callback_data=f"whisper:{wid}"),
                    InlineKeyboardButton(text="▶️ Start Whispry", url=url),
                ]
            ]
        )
        try:
            await context.bot.edit_message_reply_markup(
                inline_message_id=query.inline_message_id, reply_markup=keyboard
            )
        except TelegramError as exc:
            logger.debug(
                "Could not add Start Whispry button to whisper %s: %s", wid, exc
            )

    await _safe_answer(
        query,
        "📩 Start Whispry first (tap ▶️ Start Whispry), then press 🔐 again to "
        "receive the media.",
    )


def _finalize_media_delivery(
    context: ContextTypes.DEFAULT_TYPE,
    session: Dict[str, Any],
    chat_id: int,
    message_id: int,
) -> None:
    """Apply the view mode after a successful private delivery."""
    wid = session["whisper_id"]
    mode = session.get("view_mode") or "normal"

    if mode == "once":
        session["delivery_state"] = "consumed"
        schedule_media_delete(
            context, chat_id, message_id, ONCE_VIEW_DELETE_DELAY_SECONDS, wid
        )
    elif mode in MEDIA_VIEW_DELETE_DELAYS:
        session["delivery_state"] = "consumed"
        schedule_media_delete(
            context, chat_id, message_id, MEDIA_VIEW_DELETE_DELAYS[mode], wid
        )
    else:
        session["delivery_state"] = "delivered"

    logger.info("MEDIA WHISPER DELIVERED: whisper_id=%s mode=%s", wid, mode)
    logger.info("MEDIA WHISPER VIEWED: whisper_id=%s", wid)


async def deliver_media_whisper(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: Dict[str, Any]
) -> None:
    """
    Deliver the media whisper privately to the VERIFIED presser only.

    - Delivery goes to query.from_user.id (their own private chat).
    - Timed/once modes mark the whisper consumed (one delivery, ever).
    - If the presser never started the bot (Forbidden): reveal nothing and
      offer the ▶️ Start Whispry deep-link flow.
    - Any other delivery failure keeps the whisper retryable and is logged.
    """
    wid = session["whisper_id"]
    mode = session.get("view_mode") or "normal"
    logger.info(
        "MEDIA WHISPER DELIVERY REQUESTED: whisper_id=%s mode=%s user=%s",
        wid, mode, query.from_user.id,
    )

    try:
        delivered = await _send_media(
            context.bot, query.from_user.id, session["media"],
            build_media_caption(session),
        )
    except Forbidden:
        await offer_start_whispry(query, context, session)
        return
    except TelegramError as exc:
        logger.warning(
            "MEDIA WHISPER DELIVERY FAILED (retry possible): whisper_id=%s error=%s",
            wid, exc,
        )
        await _safe_answer(
            query,
            "⚠️ The media could not be delivered right now. If it hasn't expired, "
            "press 🔐 again in a moment.",
        )
        return

    _finalize_media_delivery(context, session, query.from_user.id, delivered.message_id)
    await _safe_answer(query, "🔓 The whisper was delivered to your private chat.", alert=False)


async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return

    # Bookkeeping registry update (NOT used for authorization).
    register_user(context, query.from_user)

    if not query.data.startswith("whisper:"):
        await query.answer()
        return

    # callback_data is "whisper:<whisper_id>" — ID only, never the text/media.
    wid = query.data.split(":", 1)[1].strip()
    sessions = get_sessions(context)
    session = sessions.get(wid)

    if session is None:
        await _safe_answer(query, "🔒 This whisper has expired.")
        return

    expired = time.time() > session.get("expires_at", 0)
    consumed = session.get("delivery_state") == "consumed"
    if expired or consumed:
        if expired:
            sessions.pop(wid, None)
        if session.get("media"):
            await _safe_answer(
                query, "🔒 This media whisper has expired or has already been viewed."
            )
        else:
            await _safe_answer(query, "🔒 This whisper has expired.")
        return

    # LOG SAFETY NET (exactly-once, deduped inside send_whisper_log):
    # normally the log was already sent in on_chosen_inline_result the moment
    # the card was posted. If that update was ever lost, the first button
    # press proves the card exists in a chat — send the log NOW, once.
    await send_whisper_log(context, session)

    # SECURITY: the ONE authorization rule — based ONLY on the stored target
    # (exact user ID, or the presser's current Telegram username). The sender
    # is NOT exempt: they pass only if they are the target. Never trust the
    # callback message text.
    if not is_target_user(session, query.from_user):
        await _safe_answer(query, "❌ This whisper isn't for you.")
        return

    if session.get("media"):
        await deliver_media_whisper(query, context, session)
        return

    await open_text_reader(query, context, session)


# ---------------------------------------------------------------------------
# Private chat fallback (no DM conversation flow)
# ---------------------------------------------------------------------------

async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not message.text:
        return
    await message.reply_text(
        "🤫 I deliver whispers!\n\n"
        "To send one, type this in any chat (recipient LAST):\n"
        f"<code>@{context.bot.username} your secret message @username</code>\n"
        f"<code>@{context.bot.username} your secret message 123456789</code>\n\n"
        "🖼 Media whisper? Just send me a photo, video or file here first.\n\n"
        "Type /help to learn more, or send /game to play. 🎮",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Error handling + application wiring
# ---------------------------------------------------------------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log anything a handler didn't catch; keep the bot alive."""
    logger.error("Unhandled error while processing an update", exc_info=context.error)


async def post_init(application: Application) -> None:
    """Runs once after the bot is initialized, before polling starts."""
    global _bot_ref
    _bot_ref = application.bot  # used by the Flask thread for async log sends

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

    # Startup state summary for the persistent stores.
    logger.info(
        "Persistent state loaded: gmute=%d auth=%d owners=%d history_senders=%d",
        len(GMUTE_USERS), len(AUTH_USERS), len(OWNER_IDS), len(RECIPIENT_HISTORY),
    )
    if WEBAPP_URL:
        logger.info("Mini App whisper reader ENABLED: %s/reader", WEBAPP_URL.rstrip("/"))
    else:
        logger.info(
            "WEBAPP_URL not set — text whispers use the classic popup/DM reveal."
        )

    # --- Verify the LOG_CHANNEL destination at startup -----------------------
    # A real send is the only way to catch Forbidden / chat-not-found /
    # not-enough-rights. Never silently ignored — but never fatal either:
    # whispers keep working even if logging is broken.
    if LOG_CHANNEL_DEST is not None:
        try:
            await application.bot.send_message(
                chat_id=LOG_CHANNEL_DEST,
                text="🤖 <b>Whisper Bot</b> connected — whisper logging is active.",
                parse_mode=ParseMode.HTML,
            )
            logger.info(
                "LOG_CHANNEL verified — test message delivered to %s.", LOG_CHANNEL_DEST
            )
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

    # Group 0: normal handlers (commands, inline, callbacks, private messages).
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("game", cmd_game))
    # Owner/auth management + global mute (internally permission-checked,
    # support both numeric-ID arguments and reply-to-message targets).
    application.add_handler(CommandHandler("gmute", cmd_gmute))
    application.add_handler(CommandHandler("gunmute", cmd_gunmute))
    application.add_handler(CommandHandler("addauth", cmd_addauth))
    application.add_handler(CommandHandler("rauth", cmd_rauth))
    application.add_handler(InlineQueryHandler(on_inline_query))
    # THE whisper-creation path: fires the moment the card is posted -> log
    # -> recipient history update.
    application.add_handler(ChosenInlineResultHandler(on_chosen_inline_result))
    application.add_handler(CallbackQueryHandler(on_callback_query, pattern=r"^whisper:"))
    # Media intake (private chat): photo / video / document -> pending media.
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & (filters.PHOTO | filters.VIDEO | filters.Document.ALL),
            on_private_media,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            on_private_message,
        )
    )

    # Group 1 — GMUTE fast-path (SEPARATE handler group!):
    # PTB processes each group independently, so this runs for every group
    # message WITHOUT swallowing commands registered in group 0. Returns
    # instantly for non-muted users, so normal messages are never affected.
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
    """
    Build the Telegram application and start long polling.

    Blocks the calling thread — start_bot_thread() runs this in a background
    thread (with its own explicitly created asyncio event loop) so the Flask
    web server in the main thread stays responsive.
    """
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
    if not GAME_URL:
        logger.warning("GAME_URL is not set — /game will say the game is not configured.")
    elif not GAME_URL.lower().startswith("https://"):
        logger.warning("GAME_URL should start with https:// for Telegram Mini App buttons.")

    logger.info("Starting Telegram bot (long polling)...")
    application = build_application()
    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,  # includes chosen_inline_result!
            drop_pending_updates=True,
            close_loop=False,   # _bot_worker() owns and closes the loop itself.
            stop_signals=None,  # CRITICAL: no SIGINT/SIGTERM handlers on this
                                # background thread's loop — set_wakeup_fd()
                                # only works in the main thread (Flask owns it).
        )
    except InvalidToken:
        logger.critical("BOT_TOKEN is invalid. Get a fresh token from @BotFather.")
    except TelegramError as exc:
        logger.critical("Telegram bot stopped with an API error: %s", exc)
    logger.info("Telegram bot polling stopped.")


# ---------------------------------------------------------------------------
# Background-thread startup (Python 3.12 event-loop fix + stop_signals=None)
# ---------------------------------------------------------------------------

# Last fatal error of the bot thread (surfaced by Flask's /health endpoint).
_bot_error: Optional[str] = None


def _bot_worker() -> None:
    """
    Thread target that runs the Telegram bot.

    Python 3.10+/3.12 do NOT create an asyncio event loop automatically for
    non-main threads, so we explicitly create and install one here BEFORE
    starting the bot, and close it again when the bot stops.
    """
    global _bot_error, _bot_loop

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _bot_loop = loop  # used by the Flask thread for scheduled log sends
    try:
        run_bot()
    except Exception as exc:
        # Never let the thread die silently — /health reports this error.
        _bot_error = f"{type(exc).__name__}: {exc}"
        logger.exception("Telegram bot thread crashed!")
    finally:
        _bot_loop = None
        try:
            # Give pending async generators a chance to finalize, then close.
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def start_bot_thread() -> threading.Thread:
    """
    Start the Telegram bot in a daemon background thread and return the thread.

    Called by live.py BEFORE Flask starts, so the architecture stays:

        python live.py
            ├── Telegram bot  -> background thread (own asyncio event loop,
            │                    run_polling(stop_signals=None))
            └── Flask         -> main thread (+ /reader Mini App + open API)

    Exactly one thread, one Application, one run_polling() call.
    """
    thread = threading.Thread(target=_bot_worker, name="telegram-bot", daemon=True)
    thread.start()
    return thread


def get_bot_error() -> Optional[str]:
    """Return the bot thread's last fatal error (None if the bot is healthy)."""
    return _bot_error
