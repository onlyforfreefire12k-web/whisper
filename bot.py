"""
bot.py — Inline Whisper Bot (all Telegram logic lives here).

Whisper flow:

1. INLINE CREATION — the sender types in any chat:

       @BotName <whisper text> @TargetUsername
       @BotName <whisper text> 123456789

   The target (recipient) is the LAST token — either a @username or a numeric
   Telegram user ID. There is NO separate "Choose Recipient" conversation.

2. THE TARGET IS STORED EXACTLY AS ENTERED — no resolution, no registry,
   no /start requirement, and NO bot.get_chat("@username") lookups:
       '@ObsessHolic' -> target_type="username", target_username="obsessholic"
                         (normalized: no @, trimmed, lowercase) + the exact
                         display string as typed, for the card/log.
       '123456789'    -> target_type="user_id", target_user_id=123456789

3. The card posted into the chat shows the EXACT target and NEVER the text:

       🔐 A whisper message to @ObsessHolic.
       Only they can read the message.

       　　🔐

       [ 🔐 ]   <- button, callback_data "whisper:<whisper_id>" (ID only)

   For numeric targets: "🔐 A whisper message to user ID 123456789."

4. 🔐 BUTTON AUTHORIZATION — ONE rule, based ONLY on the stored target:
       - user_id target: callback_query.from_user.id == target_user_id
       - username target: normalized callback_query.from_user.username ==
         target_username (the username Telegram reports for whoever presses
         the button — learned from the callback itself, no Bot API lookup).
   The sender has no special status: they pass only if they are the target.
   Everyone else gets "❌ This whisper isn't for you." — text never revealed.

   Known Telegram limitation (by design): a username target that doesn't
   exist or changed its username can no longer be matched — the card still
   preserves the target exactly as entered; numeric IDs are immune to this.

5. REVEAL: verified target only. Short whispers (<=200 chars) open as an
   ephemeral callback popup (visible to the presser ALONE). Longer whispers
   are DM'd to the verified presser's own user ID (from the callback query —
   never to a chat at large).

6. Whispers expire after SESSION_TTL_SECONDS (15 minutes): expired or
   unknown cards answer "🔒 This whisper has expired." and never reveal text.

7. LOG CHANNEL (LOG_CHANNEL config — @username or numeric ID; invite links
   are rejected at startup): the moment the card is posted (ChosenInlineResult)
   the COMPLETE whisper is logged — immediately and asynchronously
   (Application.create_task, non-blocking). Failures NEVER break the whisper:
   the full exception is printed via "WHISPER LOG SEND FAILED".

Threading model (Render Web Service) — unchanged:

    python live.py
        ├── Telegram bot -> daemon background thread (start_bot_thread)
        └── Flask        -> main thread (live.py)

Event-loop / signal notes (Python 3.12, background thread) — unchanged:
the thread's event loop is created explicitly (asyncio.new_event_loop() +
asyncio.set_event_loop()) BEFORE the bot starts, and
Application.run_polling() is called with stop_signals=None (no SIGINT/SIGTERM
handlers on a non-main thread — set_wakeup_fd only works in the main thread)
and close_loop=False (_bot_worker() owns and closes the loop itself).
"""

import asyncio
import html
import logging
import re
import secrets
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from telegram import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
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

from config import (
    BOT_TOKEN,
    GAME_URL,
    LOG_CHANNEL_DEST,
    SESSION_TTL_SECONDS,
    WHISPER_MAX_LENGTH,
    log_channel_problem,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory state (no database required)
# ---------------------------------------------------------------------------

SESSIONS_KEY = "whisper_sessions"  # whisper_id -> session dict
USERS_KEY = "user_registry"        # telegram user id (as str) -> profile
                                   # (bookkeeping only — NOT used for whisper
                                   #  targets or authorization)

# answerCallbackQuery text is limited to 200 characters by Telegram.
CALLBACK_ANSWER_MAX = 200

# Minimum digit count for a trailing numeric token to be treated as a
# Telegram user ID (guards against ordinary sentences ending in a number).
TARGET_ID_MIN_DIGITS = 4

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,64}$")

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


def parse_inline_query(raw: str) -> Tuple[Optional[str], Optional[int], str]:
    """
    Split '@BotName <whisper text> <target>' into (target_username, target_id, text).

    The target is the LAST token and may be:
      - '@Username'  -> returned as (username_as_typed, None, text)
      - '123456789'  -> returned as (None, 123456789, text)
    If the last token is neither, the whole query is treated as text with no
    target (the caller then shows the format hint).
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
# Registry access (bookkeeping only — NOT part of the whisper flow)
# ---------------------------------------------------------------------------

def get_sessions(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Dict[str, Any]]:
    return context.application.bot_data.setdefault(SESSIONS_KEY, {})


def get_users(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Dict[str, Any]]:
    return context.application.bot_data.setdefault(USERS_KEY, {})


def register_user(context: ContextTypes.DEFAULT_TYPE, user: Any) -> None:
    """
    Store/update a user in the bookkeeping registry (Telegram ID, username,
    display name). All data comes straight from Telegram updates — never from
    user-typed text.

    IMPORTANT: this registry is NOT used for whisper target resolution or
    authorization. Whisper targets are stored exactly as the sender typed
    them and authorized via the button-press callback (see is_target_user).
    """
    if user is None:
        return
    get_users(context)[str(user.id)] = {
        "id": user.id,
        "username": user.username,
        "name": user.first_name or user.full_name or "Unknown",
    }


def prune_sessions(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop whisper sessions past their expiration time."""
    now = time.time()
    sessions = get_sessions(context)
    for wid in [wid for wid, s in sessions.items() if now > s.get("expires_at", 0)]:
        sessions.pop(wid, None)


# ---------------------------------------------------------------------------
# Whisper authorization — the ONE rule
# ---------------------------------------------------------------------------

def is_target_user(session: Dict[str, Any], user: Any) -> bool:
    """
    The single whisper-authorization rule, based ONLY on the stored target.

      - user_id target: exact Telegram user ID match.
      - username target: normalized comparison against the username Telegram
        reports for the person pressing the button (learned from the callback
        query itself — no Bot API lookups, no registry, no /start required).

    The sender has NO special status: they pass only if they are the target.
    """
    if session["target_type"] == "user_id":
        return user.id == session["target_user_id"]
    presser = normalize_username(getattr(user, "username", None))
    return bool(presser) and presser == session["target_username"]


# ---------------------------------------------------------------------------
# Message builders (all user input is HTML-escaped)
# ---------------------------------------------------------------------------

def build_whisper_card(session: Dict[str, Any]) -> str:
    """The public card posted into the chat. Contains NO whisper text and
    shows the EXACT target the sender entered."""
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


def build_log_text(session: Dict[str, Any]) -> str:
    """Complete moderation log — sent ONLY to the private log channel."""
    if session["target_type"] == "user_id":
        target_type_label = "User ID"
        target_id_line = str(session["target_user_id"])
    else:
        target_type_label = "Username"
        target_id_line = "Unknown"
    return (
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
        f"{target_id_line}\n\n"
        "💬 <b>Whisper:</b>\n"
        f"{html.escape(session['text'])}\n\n"
        "🕐 <b>Time:</b>\n"
        f"{format_time(session.get('posted_at') or session['created_at'])}"
    )


# ---------------------------------------------------------------------------
# Audit log (best-effort — NEVER breaks whisper creation)
# ---------------------------------------------------------------------------

async def log_whisper(context: ContextTypes.DEFAULT_TYPE, session: Dict[str, Any]) -> None:
    """
    Send the complete whisper log to LOG_CHANNEL. Never raises.

    Scheduled asynchronously (Application.create_task) by the caller the
    moment the whisper card is posted, so whisper creation is never blocked.
    On failure the FULL exception (with traceback) is printed to the Render
    logs under "WHISPER LOG SEND FAILED"; the whisper itself is unaffected.
    """
    wid = session["whisper_id"]
    if LOG_CHANNEL_DEST is None:
        # Configuration problem (already reported loudly at startup) — still
        # flag it per whisper so nothing is silently dropped.
        logger.warning(
            "Whisper #%s NOT logged — %s", wid, log_channel_problem()
        )
        return
    try:
        await context.bot.send_message(
            chat_id=LOG_CHANNEL_DEST,
            text=build_log_text(session),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        # Covers Forbidden / chat not found / not enough rights / network —
        # print the complete exception with traceback, never swallow it.
        logger.exception("WHISPER LOG SEND FAILED for %s", wid)


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
    bot_username = context.bot.username
    text = (
        "🔐 <b>Inline Whisper Bot</b>\n\n"
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
        "account (user ID, or their current @username). Everyone else — "
        "including the sender — sees “❌ This whisper isn't for you.”\n\n"
        "ℹ️ The whisper text is never shown in the chat.\n\n"
        "Type /help for details, or /game to play. 🎮"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    bot_username = context.bot.username
    text = (
        "📖 <b>Whisper Bot — Help</b>\n\n"
        "🤫 <b>Sending a whisper</b>\n"
        f"• Type <code>@{bot_username} your message @username</code> — the recipient "
        "comes LAST.\n"
        f"• Or use a numeric Telegram user ID: <code>@{bot_username} your message "
        "123456789</code>.\n"
        "• The target does NOT need to have started the bot — the whisper card is "
        "posted in the chat and they unlock it with the 🔐 button.\n"
        "• The whisper text is never shown in the chat.\n\n"
        "🔓 <b>Opening a whisper</b>\n"
        "• Press the 🔐 button on the card.\n"
        "• Numeric-ID whispers open only for that exact Telegram user ID.\n"
        "• @username whispers open only for the Telegram account currently using "
        "that username — if the target changes their username, the whisper can no "
        "longer be opened (user IDs are immune to that).\n"
        "• Short whispers open as a private popup; long ones are sent to the "
        "target's private chat.\n\n"
        "🎮 <b>Game</b>\n"
        "• Send /game to open the mini app game right inside Telegram.\n\n"
        "ℹ️ <b>Good to know</b>\n"
        f"• A whisper needs text — up to {WHISPER_MAX_LENGTH} characters.\n"
        f"• Whispers expire after {SESSION_TTL_SECONDS // 60} minutes — expired cards "
        "show “🔒 This whisper has expired.”\n"
        "• Every whisper is recorded in the bot's private log channel for moderation "
        "and abuse reports."
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
# Inline mode — whisper creation
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


async def on_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = update.inline_query
    if inline_query is None:
        return
    user = inline_query.from_user

    # Bookkeeping registry update (NOT used for whisper targets/auth).
    register_user(context, user)
    prune_sessions(context)

    target_username, target_id, whisper_text = parse_inline_query(inline_query.query)
    whisper_text = (whisper_text or "").strip()

    # Missing recipient or empty whisper -> friendly format hint.
    if (target_username is None and target_id is None) or not whisper_text:
        try:
            await inline_query.answer(
                results=[_format_hint_result(context)], cache_time=1, is_personal=True
            )
        except TelegramError as exc:
            logger.error("Failed to answer inline query: %s", exc)
        return

    # Whisper that is too long -> hint as well.
    if len(whisper_text) > WHISPER_MAX_LENGTH:
        try:
            await inline_query.answer(
                results=[_too_long_result()], cache_time=1, is_personal=True
            )
        except TelegramError as exc:
            logger.error("Failed to answer inline query: %s", exc)
        return

    # --- Store the target EXACTLY as entered (no resolution of any kind) ----
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

    # --- Create the whisper session ------------------------------------------
    wid = generate_whisper_id()
    now = time.time()
    session: Dict[str, Any] = {
        "whisper_id": wid,
        "sender_id": user.id,
        "sender_name": user.first_name or user.full_name or "Unknown",
        "sender_username": user.username,
        "target_type": target_type,            # "username" | "user_id"
        "target_display": target_display,      # exactly as the sender entered it
        "target_username": target_username_norm,  # normalized, username targets only
        "target_user_id": target_user_id,      # numeric targets only
        "text": whisper_text,
        "status": "created",
        "created_at": now,
        "expires_at": now + SESSION_TTL_SECONDS,
        "posted_at": None,
    }
    get_sessions(context)[wid] = session

    if target_type == "user_id":
        title = f"🔐 A whisper message to user ID {target_user_id}"
    else:
        title = f"🔐 A whisper message to {target_display}"

    result = InlineQueryResultArticle(
        id=wid,
        title=title,
        description="Only they can read the message — tap to post",
        input_message_content=InputTextMessageContent(
            message_text=build_whisper_card(session), parse_mode=ParseMode.HTML
        ),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(text="🔐", callback_data=f"whisper:{wid}")]]
        ),
    )
    try:
        await inline_query.answer(results=[result], cache_time=0, is_personal=True)
    except TelegramError as exc:
        logger.error("Failed to answer inline query: %s", exc)
        try:
            await inline_query.answer(results=[], cache_time=5)
        except TelegramError:
            pass


async def on_chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Fires the instant the sender posts the whisper card into a chat, i.e. the
    moment the whisper is successfully created in the chat.

    The complete whisper log is sent to LOG_CHANNEL IMMEDIATELY and
    ASYNCHRONOUSLY (create_task) so the handler never blocks and a logging
    failure can never break the whisper.
    """
    chosen = update.chosen_inline_result
    if chosen is None:
        return
    sessions = get_sessions(context)
    session = sessions.get(chosen.result_id)
    if session is None:
        return
    if session.get("posted_at") is not None:
        return  # Same whisper posted twice — log it only once.

    session["status"] = "posted"
    session["posted_at"] = time.time()

    context.application.create_task(log_whisper(context, session))


# ---------------------------------------------------------------------------
# 🔐 Read button — verified, private reveal
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
    """Reveal the whisper text ONLY to the already-verified presser."""
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


async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return

    # Bookkeeping registry update (NOT used for authorization).
    register_user(context, query.from_user)

    if not query.data.startswith("whisper:"):
        await query.answer()
        return

    # callback_data is "whisper:<whisper_id>" — ID only, never the text.
    wid = query.data.split(":", 1)[1].strip()
    sessions = get_sessions(context)
    session = sessions.get(wid)

    # Unknown or expired whispers must NEVER reveal text.
    if session is None or time.time() > session.get("expires_at", 0):
        sessions.pop(wid, None)
        await _safe_answer(query, "🔒 This whisper has expired.")
        return

    # SECURITY: the ONE authorization rule — based ONLY on the stored target
    # (exact user ID, or the presser's current Telegram username). The sender
    # is NOT exempt: they pass only if they are the target.
    if not is_target_user(session, query.from_user):
        await _safe_answer(query, "❌ This whisper isn't for you.")
        return

    await reveal_whisper(query, context, session)


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

    application.bot_data[SESSIONS_KEY] = {}
    application.bot_data[USERS_KEY] = {}

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("game", cmd_game))
    application.add_handler(InlineQueryHandler(on_inline_query))
    # Fires the moment the card is posted -> immediate async log to channel.
    application.add_handler(ChosenInlineResultHandler(on_chosen_inline_result))
    application.add_handler(CallbackQueryHandler(on_callback_query, pattern=r"^whisper:"))
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            on_private_message,
        )
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
    global _bot_error

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        run_bot()
    except Exception as exc:
        # Never let the thread die silently — /health reports this error.
        _bot_error = f"{type(exc).__name__}: {exc}"
        logger.exception("Telegram bot thread crashed!")
    finally:
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
            └── Flask         -> main thread

    Exactly one thread, one Application, one run_polling() call.
    """
    thread = threading.Thread(target=_bot_worker, name="telegram-bot", daemon=True)
    thread.start()
    return thread


def get_bot_error() -> Optional[str]:
    """Return the bot thread's last fatal error (None if the bot is healthy)."""
    return _bot_error
