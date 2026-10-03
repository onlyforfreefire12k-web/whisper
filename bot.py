"""
bot.py — Inline Whisper Bot (all Telegram logic lives here).

UX (reference whisper-card flow):

1. A user types an inline query in any chat:
       @BotName @TargetUsername secret message

2. Selecting the inline result posts a LOCKED WHISPER CARD into the chat:

       🔐 A whisper message to @TargetUsername.
       Only they can read the message.

       　　🔐

       [ 🔐 ]   <- button, callback_data "whisper:<whisper_id>"

   The actual whisper text is NEVER posted into the chat and is never
   placed inside callback_data.

3. When ANYONE presses the 🔐 button, the bot compares the presser's
   Telegram user ID with the recipient ID stored with the whisper:
       - match    -> the whisper text is revealed ONLY to the presser via an
                     ephemeral callback popup (answerCallbackQuery alerts are
                     visible to the presser alone); long whispers are sent as
                     a private DM instead.
       - no match -> "❌ This whisper isn't for you."

4. The sender cannot read the whisper unless they explicitly targeted
   themselves — the same recipient-ID check applies to them.

5. USER REGISTRY: every user who sends /start is registered (Telegram user
   ID, username, display name). Target usernames are looked up ONLY in this
   registry — no arbitrary Telegram username searching. If the target never
   started the bot, the sender is told so (without leaking whisper text).

6. Whispers expire after SESSION_TTL_SECONDS (15 minutes): expired or
   unknown cards answer "🔒 This whisper has expired." and never reveal text.

7. Every posted whisper is logged (complete text) to the private log channel
   via ChosenInlineResultHandler — i.e. when the card is actually posted.

Threading model (Render Web Service):

    python live.py
        ├── Telegram bot -> daemon background thread (start_bot_thread)
        └── Flask        -> main thread (live.py)

Event-loop / signal notes (Python 3.12, background thread):

* The thread's asyncio event loop is created explicitly with
  asyncio.new_event_loop() + asyncio.set_event_loop() BEFORE the bot starts
  (Python 3.10+/3.12 no longer auto-create loops for non-main threads).
* Application.run_polling() is called with stop_signals=None: by default it
  installs SIGINT/SIGTERM handlers via loop.add_signal_handler() ->
  signal.set_wakeup_fd(), which is only allowed in the MAIN thread (Flask
  owns it). stop_signals=None skips that entirely. close_loop=False because
  _bot_worker() owns and closes the loop itself.
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
from telegram.constants import ChatType, ParseMode
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
    LOG_CHANNEL_ID,
    SESSION_TTL_SECONDS,
    WHISPER_MAX_LENGTH,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory state (no database required)
# ---------------------------------------------------------------------------

SESSIONS_KEY = "whisper_sessions"   # whisper_id -> session dict
USERS_KEY = "user_registry"         # telegram user id (as str) -> profile
USERNAME_INDEX_KEY = "username_index"  # lower-case username -> user id

# answerCallbackQuery text is limited to 200 characters by Telegram.
CALLBACK_ANSWER_MAX = 200

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,64}$")

_used_whisper_ids: set = set()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def generate_whisper_id() -> str:
    """Unique whisper ID such as 'W-8F42A1' (displayed as '#W-8F42A1'). Never reused."""
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


def fmt_user_block(name: Optional[str], username: Optional[str], user_id: int) -> str:
    return f"{fmt_display_name(name, username)}\nID: <code>{user_id}</code>"


def parse_inline_query(raw: str) -> Tuple[Optional[str], str]:
    """Split '@target_username whisper text' into ('target_username', 'whisper text')."""
    text = (raw or "").strip()
    if text.startswith("@"):
        parts = text.split(maxsplit=1)
        candidate = parts[0][1:]
        if candidate and _USERNAME_RE.match(candidate):
            rest = parts[1].strip() if len(parts) > 1 else ""
            return candidate, rest
    return None, text


# ---------------------------------------------------------------------------
# Registry access (bot_data-backed, in-memory)
# ---------------------------------------------------------------------------

def get_sessions(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Dict[str, Any]]:
    return context.application.bot_data.setdefault(SESSIONS_KEY, {})


def get_users(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Dict[str, Any]]:
    return context.application.bot_data.setdefault(USERS_KEY, {})


def get_username_index(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, int]:
    return context.application.bot_data.setdefault(USERNAME_INDEX_KEY, {})


def register_user(context: ContextTypes.DEFAULT_TYPE, user: Any) -> None:
    """Store/update a user in the registry: Telegram ID, username, display name.

    Called on /start (and whenever Telegram provides the user). The data comes
    straight from the Telegram update — never from user-typed text — so it
    cannot be spoofed.
    """
    if user is None:
        return
    get_users(context)[str(user.id)] = {
        "id": user.id,
        "username": user.username,
        "name": user.first_name or user.full_name or "Unknown",
    }
    if user.username:
        get_username_index(context)[user.username.lower()] = user.id


def find_registered_user(
    context: ContextTypes.DEFAULT_TYPE, username: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Look up a target username in the registry ONLY (no Telegram username search)."""
    if not username:
        return None
    user_id = get_username_index(context).get(username.lower())
    if user_id is None:
        return None
    return get_users(context).get(str(user_id))


def prune_sessions(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop whisper sessions that are older than SESSION_TTL_SECONDS."""
    now = time.time()
    sessions = get_sessions(context)
    for wid in [
        wid for wid, s in sessions.items() if now - s["created_at"] > SESSION_TTL_SECONDS
    ]:
        sessions.pop(wid, None)


# ---------------------------------------------------------------------------
# Message builders (all user input is HTML-escaped)
# ---------------------------------------------------------------------------

def build_whisper_card(session: Dict[str, Any]) -> str:
    """The public card posted into the chat. Contains NO whisper text."""
    target = "@" + html.escape(session["recipient_username"])
    return (
        "🔐 A whisper message to <b>" + target + "</b>.\n"
        "Only they can read the message.\n\n"
        "　　🔐"
    )


def build_reveal_dm(session: Dict[str, Any]) -> str:
    """Private message with the full whisper, sent only to the verified recipient."""
    sender = fmt_display_name(session["sender_name"], session["sender_username"])
    return (
        f"🤫 <b>Whisper #{session['whisper_id']}</b>\n\n"
        f"👤 <b>From:</b> {sender}\n"
        f"🕐 <b>Time:</b> {format_time(session['created_at'])}\n\n"
        f"💬 <b>Whisper:</b>\n{html.escape(session['text'])}"
    )


def build_log_text(session: Dict[str, Any]) -> str:
    """Complete moderation log — sent ONLY to the private log channel."""
    sender_block = fmt_user_block(
        session["sender_name"], session["sender_username"], session["sender_id"]
    )
    recipient_block = fmt_user_block(
        session.get("recipient_name"),
        session.get("recipient_username"),
        session.get("recipient_id", 0),
    )
    return (
        "🔐 <b>WHISPER LOG</b>\n\n"
        f"👤 <b>Sender:</b>\n{sender_block}\n\n"
        f"🎯 <b>Recipient:</b>\n{recipient_block}\n\n"
        f"💬 <b>Whisper text:</b>\n{html.escape(session['text'])}\n\n"
        f"🕐 <b>Time:</b>\n{format_time(session.get('posted_at') or session['created_at'])}\n\n"
        f"🆔 <b>Whisper ID:</b>\n#{session['whisper_id']}"
    )


# ---------------------------------------------------------------------------
# Audit log (best-effort, never crashes the bot)
# ---------------------------------------------------------------------------

async def log_whisper(context: ContextTypes.DEFAULT_TYPE, session: Dict[str, Any]) -> None:
    """Best-effort audit log to LOG_CHANNEL_ID. Never raises."""
    if not LOG_CHANNEL_ID:
        logger.warning(
            "LOG_CHANNEL_ID is not configured — whisper #%s was not logged.",
            session["whisper_id"],
        )
        return
    try:
        await context.bot.send_message(
            chat_id=LOG_CHANNEL_ID,
            text=build_log_text(session),
            parse_mode=ParseMode.HTML,
        )
    except Forbidden as exc:
        logger.error(
            "Cannot post to LOG_CHANNEL_ID (%s) — is the bot still an admin with "
            "'Post Messages' rights? Whisper #%s not logged: %s",
            LOG_CHANNEL_ID, session["whisper_id"], exc,
        )
    except TelegramError as exc:
        logger.error(
            "Failed to log whisper #%s to the log channel: %s",
            session["whisper_id"], exc,
        )
    except Exception:
        logger.exception("Unexpected error while logging whisper #%s", session["whisper_id"])


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Register/update the user in the registry (ID, username, display name).
    user = update.effective_user
    if user is not None:
        register_user(context, user)

    if update.message is None:
        return
    bot_username = context.bot.username
    text = (
        "🔐 <b>Inline Whisper Bot</b>\n\n"
        "You're registered! ✅ People can now send you whispers.\n\n"
        "📌 <b>How to send a whisper</b>\n"
        "1️⃣ In any chat, type:\n"
        f"    <code>@{bot_username} @username your secret message</code>\n"
        "2️⃣ Tap the whisper result that appears.\n"
        "3️⃣ A locked card is posted in the chat:\n\n"
        "    🔐 A whisper message to @username.\n"
        "    Only they can read the message.\n\n"
        "4️⃣ Only <b>@username</b> can press the 🔐 button to read it — everyone "
        "else sees “❌ This whisper isn't for you.”\n\n"
        "ℹ️ The whisper text is never shown in the chat — not even for you as the "
        "sender (unless you whispered to yourself 😉).\n\n"
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
        f"• Type <code>@{bot_username} @username your secret message</code> in any chat "
        "and tap the result.\n"
        "• A locked card is posted in the chat — the whisper text is never shown there.\n"
        "• Only the recipient can press the 🔐 button to read the whisper (verified by "
        "their Telegram user ID). Everyone else — including the sender — sees "
        "“❌ This whisper isn't for you.”\n"
        "• Short whispers open as a private popup; long ones are sent to the "
        "recipient's private chat.\n\n"
        "✅ <b>Receiving whispers</b>\n"
        "• You must press Start on this bot once — that registers your @username so "
        "people can whisper to you.\n\n"
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
        title="✍️ Format: @bot @username message",
        description="Example: @bot @rahul meet me at 5?",
        input_message_content=InputTextMessageContent(
            message_text=(
                "🔐 <b>How to send a whisper</b>\n\n"
                "Type your whisper like this:\n"
                f"<code>@{bot_username} @username your secret message</code>\n\n"
                "Example:\n"
                f"<code>@{bot_username} @rahul meet me at 5?</code>\n\n"
                "A locked whisper card is posted in the chat — only @username can "
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


def _not_started_result(
    context: ContextTypes.DEFAULT_TYPE, username: str
) -> InlineQueryResultArticle:
    """Shown when the target username is not in the registry (never started the bot).

    NOTE: contains NO whisper text — the whisper must not leak into the chat.
    """
    bot_username = context.bot.username
    safe = html.escape(username)
    return InlineQueryResultArticle(
        id="notstarted-" + secrets.token_hex(4),
        title=f"❌ @{username} can't receive whispers yet",
        description="They must start the bot once first",
        input_message_content=InputTextMessageContent(
            message_text=(
                f"❌ <b>@{safe}</b> can't receive whispers yet.\n\n"
                f"They must open <code>@{bot_username}</code> and press <b>Start</b> "
                "once. After that you can send them a whisper. 🤫"
            ),
            parse_mode=ParseMode.HTML,
        ),
    )


async def on_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = update.inline_query
    if inline_query is None:
        return
    user = inline_query.from_user
    prune_sessions(context)

    target_username, whisper_text = parse_inline_query(inline_query.query)
    whisper_text = whisper_text.strip()

    # Empty whisper or missing target -> friendly format hint (no whisper text posted).
    if not target_username or not whisper_text:
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

    # Registry-only recipient lookup — no arbitrary Telegram username searching.
    recipient = find_registered_user(context, target_username)
    if recipient is None:
        try:
            await inline_query.answer(
                results=[_not_started_result(context, target_username)],
                cache_time=1,
                is_personal=True,
            )
        except TelegramError as exc:
            logger.error("Failed to answer inline query: %s", exc)
        return

    wid = generate_whisper_id()
    session: Dict[str, Any] = {
        "whisper_id": wid,
        "sender_id": user.id,
        "sender_name": user.first_name or user.full_name or "Unknown",
        "sender_username": user.username,
        # Recipient data comes from the registry (their own /start) — never spoofed.
        "recipient_id": recipient["id"],
        "recipient_name": recipient["name"],
        "recipient_username": recipient["username"] or target_username,
        "text": whisper_text,
        "status": "created",
        "created_at": time.time(),
        "posted_at": None,
    }
    get_sessions(context)[wid] = session

    result = InlineQueryResultArticle(
        id=wid,
        title=f"🔐 A whisper message to @{session['recipient_username']}",
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
    """Fires when the sender actually posts the whisper card into a chat."""
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

    # The card is now live in the chat: log the COMPLETE whisper (moderation).
    await log_whisper(context, session)


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

    # Longer whispers (or popup failure): send the full text to the
    # recipient's private chat. They must have started the bot, so this
    # normally works.
    try:
        await context.bot.send_message(
            chat_id=session["recipient_id"],
            text=build_reveal_dm(session),
            parse_mode=ParseMode.HTML,
        )
        await _safe_answer(query, "📩 The whisper was opened in your private chat.")
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
    if not query.data.startswith("whisper:"):
        await query.answer()
        return

    wid = query.data.split(":", 1)[1].strip()
    sessions = get_sessions(context)
    session = sessions.get(wid)

    # Unknown or expired whispers must NEVER reveal text.
    if session is None:
        await _safe_answer(query, "🔒 This whisper has expired.")
        return
    if time.time() - session["created_at"] > SESSION_TTL_SECONDS:
        sessions.pop(wid, None)
        await _safe_answer(query, "🔒 This whisper has expired.")
        return

    # SECURITY: verify presser identity against the stored recipient ID.
    # The sender is NOT exempt — they only pass if they targeted themselves.
    if query.from_user.id != session["recipient_id"]:
        await _safe_answer(query, "❌ This whisper isn't for you.")
        return

    await reveal_whisper(query, context, session)


# ---------------------------------------------------------------------------
# Private chat fallback (no DM conversation flow anymore)
# ---------------------------------------------------------------------------

async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not message.text:
        return
    await message.reply_text(
        "🤫 I deliver whispers!\n\n"
        "To send one, type this in any chat:\n"
        f"<code>@{context.bot.username} @username your secret message</code>\n\n"
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


def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    application.bot_data[SESSIONS_KEY] = {}
    application.bot_data[USERS_KEY] = {}
    application.bot_data[USERNAME_INDEX_KEY] = {}

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("game", cmd_game))
    application.add_handler(InlineQueryHandler(on_inline_query))
    # Fires when the card is actually posted -> whisper log entry.
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
    if not LOG_CHANNEL_ID:
        logger.warning("LOG_CHANNEL_ID is not set — whispers will NOT be logged to a channel.")
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
