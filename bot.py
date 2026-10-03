"""
bot.py — Inline Whisper Bot (all Telegram logic lives here).

Whisper flow (100% Telegram-native — no fake "invisible messages"):

1. A user types an inline query in any chat:
       @BotName your secret message
   or, with the recipient shortcut:
       @BotName @recipient your secret message

2. Selecting the inline result posts a small "card" into the chat that
   contains NO whisper text, together with a button:
       - "🎯 Choose Recipient" -> the bot DMs the sender, who replies with
                                  the recipient's @username.
       - "📨 Send Whisper"     -> recipient @username was already in the query.

3. The bot resolves the recipient via Telegram's getChat() API (so the
   recipient identity can't be spoofed), then:
       - sends the whisper TEXT as a private message to the recipient,
       - sends a private confirmation copy to the sender,
       - updates the public card in the chat (still no whisper text),
       - posts the complete whisper to the audit log channel.

Result: the whisper text is only ever visible to the sender (DM), the
recipient (DM) and the authorized log channel — never in the group chat.

The deployment entry point is live.py (Flask + bot thread).
"""

import html
import logging
import re
import secrets
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

SESSIONS_KEY = "whisper_sessions"    # sid -> session dict
AWAITING_KEY = "awaiting_recipient"  # telegram user id (as str) -> sid

CB_CHOOSE = "c"  # callback_data prefix: sender wants to pick a recipient
CB_SEND = "s"    # callback_data prefix: deliver to @username from the query

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,64}$")

_used_whisper_ids: set = set()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def generate_whisper_id() -> str:
    """Unique whisper ID such as '#W-8F42A1' (never reused)."""
    while True:
        whisper_id = "#W-" + secrets.token_hex(3).upper()
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


def preview_text(text: str, limit: int = 60) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def parse_inline_query(raw: str) -> Tuple[Optional[str], str]:
    """Split '@BotName @user message text' into ('user', 'message text')."""
    text = (raw or "").strip()
    if text.startswith("@"):
        parts = text.split(maxsplit=1)
        candidate = parts[0][1:]
        if candidate and _USERNAME_RE.match(candidate):
            rest = parts[1].strip() if len(parts) > 1 else ""
            return candidate, rest
    return None, text


def parse_username(text: Optional[str]) -> Optional[str]:
    """Extract a bare username from '@name' / 'name' text, or None."""
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("@"):
        candidate = candidate[1:]
    candidate = candidate.strip()
    if candidate and _USERNAME_RE.match(candidate):
        return candidate
    return None


def get_sessions(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Dict[str, Any]]:
    return context.application.bot_data.setdefault(SESSIONS_KEY, {})


def get_awaiting(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, str]:
    return context.application.bot_data.setdefault(AWAITING_KEY, {})


def prune_sessions(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop whisper sessions that are older than SESSION_TTL_SECONDS."""
    now = time.time()
    sessions = get_sessions(context)
    for sid in [sid for sid, s in sessions.items() if now - s["created_at"] > SESSION_TTL_SECONDS]:
        sessions.pop(sid, None)
    awaiting = get_awaiting(context)
    for user_key in [uid for uid, sid in awaiting.items() if sid not in sessions]:
        awaiting.pop(user_key, None)


# ---------------------------------------------------------------------------
# Message builders (all user input is HTML-escaped)
# ---------------------------------------------------------------------------

def build_pending_card(session: Dict[str, Any], choosing: bool = False) -> str:
    sender = fmt_display_name(session["sender_name"], session["sender_username"])
    status_line = (
        "🕓 The sender is choosing the recipient..."
        if choosing
        else "🔒 The whisper will be delivered privately."
    )
    return (
        "🤫 <b>Whisper incoming...</b>\n"
        f"👤 {sender}\n"
        f"{status_line}\n"
        f"🆔 <code>{session['whisper_id']}</code>"
    )


def build_mention_card(session: Dict[str, Any]) -> str:
    sender = fmt_display_name(session["sender_name"], session["sender_username"])
    recipient = "@" + html.escape(session.get("recipient_username") or "")
    return (
        "🤫 <b>A whisper is waiting...</b>\n"
        f"👤 {sender} → 🎯 {recipient}\n"
        "🔒 Press “Send Whisper” to deliver it privately.\n"
        f"🆔 <code>{session['whisper_id']}</code>"
    )


def build_delivered_card(session: Dict[str, Any]) -> str:
    sender = fmt_display_name(session["sender_name"], session["sender_username"])
    recipient = fmt_display_name(session.get("recipient_name"), session.get("recipient_username"))
    return (
        "✅ <b>Whisper delivered!</b>\n"
        f"👤 {sender} → 🎯 {recipient}\n"
        "🔒 The whisper text was sent privately to the recipient.\n"
        f"🆔 <code>{session['whisper_id']}</code>"
    )


def build_log_text(session: Dict[str, Any]) -> str:
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
        f"🕐 <b>Time:</b>\n{format_time(session.get('delivered_at'))}\n\n"
        f"🆔 <b>Whisper ID:</b>\n{session['whisper_id']}"
    )


# ---------------------------------------------------------------------------
# Whisper delivery + audit log
# ---------------------------------------------------------------------------

async def log_whisper(context: ContextTypes.DEFAULT_TYPE, session: Dict[str, Any]) -> None:
    """Best-effort audit log to LOG_CHANNEL_ID. Never raises."""
    if not LOG_CHANNEL_ID:
        logger.warning(
            "LOG_CHANNEL_ID is not configured — whisper %s was not logged.",
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
            "'Post Messages' rights? Whisper %s not logged: %s",
            LOG_CHANNEL_ID, session["whisper_id"], exc,
        )
    except TelegramError as exc:
        logger.error(
            "Failed to log whisper %s to the log channel: %s",
            session["whisper_id"], exc,
        )
    except Exception:
        logger.exception("Unexpected error while logging whisper %s", session["whisper_id"])


async def resolve_recipient(
    context: ContextTypes.DEFAULT_TYPE, username: str
) -> Tuple[Optional[Any], Optional[str]]:
    """
    Resolve '@username' to a real Telegram user via getChat().

    Returns (chat, None) on success or (None, user_facing_error_message).
    Usernames can therefore never be spoofed — the identity always comes
    straight from Telegram.
    """
    try:
        chat = await context.bot.get_chat(f"@{username}")
    except BadRequest:
        return None, (
            f"❌ I couldn't find @{html.escape(username)}. "
            "Double-check the username and try again."
        )
    except Forbidden:
        return None, f"❌ Telegram refused the lookup for @{html.escape(username)}."
    except TelegramError as exc:
        logger.error("get_chat failed for @%s: %s", username, exc)
        return None, "⚠️ Telegram lookup failed. Please try again in a moment."

    if chat.type != ChatType.PRIVATE:
        return None, (
            f"❌ @{html.escape(username)} is not a personal account — "
            "whispers can only be sent to people."
        )
    if chat.id == context.bot.id:
        return None, "🤖 Nice try — you can't whisper to me!"
    return chat, None


async def deliver_whisper(
    context: ContextTypes.DEFAULT_TYPE,
    session: Dict[str, Any],
    recipient_chat: Any,
) -> bool:
    """
    Deliver the whisper. Returns True on success.

    Order: recipient DM (critical) -> sender copy -> public card update ->
    audit log. Every step after the recipient DM is best-effort; failures
    are logged and never crash the bot.
    """
    whisper_id = session["whisper_id"]
    sender_line = fmt_display_name(session["sender_name"], session["sender_username"])
    recipient_name = recipient_chat.first_name or recipient_chat.title or "Unknown"
    recipient_username = recipient_chat.username or session.get("recipient_username")
    when_str = format_time(time.time())

    session["recipient_id"] = recipient_chat.id
    session["recipient_name"] = recipient_name
    session["recipient_username"] = recipient_username
    session["delivered_at"] = time.time()

    # 1) Private whisper -> recipient (the only critical step).
    recipient_text = (
        "🤫 <b>You've received a whisper!</b>\n\n"
        f"👤 <b>From:</b> {sender_line}\n"
        f"🆔 <b>Whisper ID:</b> {whisper_id}\n"
        f"🕐 <b>Time:</b> {when_str}\n\n"
        f"💬 <b>Whisper:</b>\n{html.escape(session['text'])}"
    )
    try:
        await context.bot.send_message(
            chat_id=recipient_chat.id, text=recipient_text, parse_mode=ParseMode.HTML
        )
    except Forbidden:
        logger.info(
            "Recipient %s (@%s) cannot be messaged (never started the bot or blocked it).",
            recipient_chat.id, recipient_username,
        )
        return False
    except TelegramError as exc:
        logger.error("Failed to deliver whisper %s: %s", whisper_id, exc)
        return False

    session["status"] = "delivered"

    # 2) Private confirmation -> sender (best effort).
    sender_text = (
        "✅ <b>Whisper delivered!</b>\n\n"
        f"🎯 <b>To:</b> {fmt_display_name(recipient_name, recipient_username)}\n"
        f"🆔 <b>Whisper ID:</b> {whisper_id}\n"
        f"🕐 <b>Time:</b> {when_str}\n\n"
        f"💬 <b>Whisper:</b>\n{html.escape(session['text'])}"
    )
    try:
        await context.bot.send_message(
            chat_id=session["sender_id"], text=sender_text, parse_mode=ParseMode.HTML
        )
    except Forbidden:
        logger.info(
            "Sender %s cannot be DM'd (blocked bot); skipping confirmation.",
            session["sender_id"],
        )
    except TelegramError as exc:
        logger.warning("Could not send confirmation for whisper %s: %s", whisper_id, exc)

    # 3) Update the public card in the original chat (best effort).
    if session.get("inline_message_id"):
        try:
            await context.bot.edit_message_text(
                inline_message_id=session["inline_message_id"],
                text=build_delivered_card(session),
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
        except TelegramError as exc:
            logger.debug("Could not update whisper card %s: %s", whisper_id, exc)

    # 4) Audit log (best effort).
    await log_whisper(context, session)
    return True


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    bot_username = context.bot.username
    text = (
        "🔐 <b>Inline Whisper Bot</b>\n\n"
        "Send secret whispers in <b>any</b> Telegram chat. The whisper text is never "
        "posted in the chat — it is delivered privately, so only <b>you</b> and the "
        "<b>recipient</b> can read it.\n\n"
        "📌 <b>How to send a whisper</b>\n"
        "1️⃣ In any chat, type:\n"
        f"    <code>@{bot_username} your secret message</code>\n"
        "2️⃣ Tap the whisper result that appears.\n"
        "3️⃣ Press the button on the card and pick the recipient — done! 🤫\n\n"
        "⚡ <b>Shortcut</b> — already know the recipient?\n"
        f"    <code>@{bot_username} @username your secret message</code>\n\n"
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
        f"• Type <code>@{bot_username} your message</code> in any chat, tap the result, "
        "then choose the recipient from the button on the card.\n"
        f"• Shortcut: <code>@{bot_username} @username your message</code> — the card gets "
        "a “📨 Send Whisper” button that delivers straight to that user.\n"
        "• The whisper text is never shown in the chat. It is delivered privately to the "
        "recipient, with a confirmation copy for you.\n\n"
        "🎮 <b>Game</b>\n"
        "• Send /game to open the mini app game right inside Telegram.\n\n"
        "ℹ️ <b>Good to know</b>\n"
        f"• A whisper needs text — up to {WHISPER_MAX_LENGTH} characters.\n"
        "• The recipient must have pressed Start on this bot at least once; otherwise "
        "Telegram does not allow me to message them.\n"
        "• Every delivered whisper is recorded in the bot's private log channel for "
        "moderation and abuse reports.\n"
        f"• Unsent whispers expire after {SESSION_TTL_SECONDS // 60} minutes."
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
# Inline mode
# ---------------------------------------------------------------------------

def _help_result(context: ContextTypes.DEFAULT_TYPE) -> InlineQueryResultArticle:
    bot_username = context.bot.username
    return InlineQueryResultArticle(
        id="howto-" + secrets.token_hex(4),
        title="✍️ Type your whisper message",
        description=f"Example: @{bot_username} your secret message",
        input_message_content=InputTextMessageContent(
            message_text=(
                "🔐 <b>Inline Whisper Bot</b>\n\n"
                "Type your whisper <b>after</b> the bot mention:\n"
                f"<code>@{bot_username} your secret message</code>\n\n"
                "Then tap the result and choose the recipient. 🤫"
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


async def on_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = update.inline_query
    if inline_query is None:
        return
    user = inline_query.from_user
    prune_sessions(context)

    recipient_username, whisper_text = parse_inline_query(inline_query.query)
    whisper_text = whisper_text.strip()

    # Empty whisper -> show a friendly hint instead of a sendable whisper.
    if not whisper_text:
        try:
            await inline_query.answer(
                results=[_help_result(context)], cache_time=1, is_personal=True
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

    sid = secrets.token_hex(8)
    session: Dict[str, Any] = {
        "sid": sid,
        "whisper_id": generate_whisper_id(),
        "sender_id": user.id,
        "sender_name": user.first_name or user.full_name or "Unknown",
        "sender_username": user.username,
        "text": whisper_text,
        "recipient_username": recipient_username,  # None unless '@user msg' shortcut
        "recipient_id": None,
        "recipient_name": None,
        "inline_message_id": None,
        "status": "pending",
        "created_at": time.time(),
    }
    get_sessions(context)[sid] = session

    if recipient_username:
        title = f"🕊 Whisper to @{recipient_username}"
        card = build_mention_card(session)
        button = InlineKeyboardButton(text="📨 Send Whisper", callback_data=f"{CB_SEND}|{sid}")
    else:
        title = "🤫 Send a Whisper"
        card = build_pending_card(session)
        button = InlineKeyboardButton(
            text="🎯 Choose Recipient", callback_data=f"{CB_CHOOSE}|{sid}"
        )

    result = InlineQueryResultArticle(
        id=sid,
        title=title,
        description=f"Tap to prepare: {preview_text(whisper_text)}",
        input_message_content=InputTextMessageContent(
            message_text=card, parse_mode=ParseMode.HTML
        ),
        reply_markup=InlineKeyboardMarkup([[button]]),
    )
    try:
        await inline_query.answer(results=[result], cache_time=0, is_personal=True)
    except TelegramError as exc:
        logger.error("Failed to answer inline query: %s", exc)
        try:
            await inline_query.answer(results=[], cache_time=5)
        except TelegramError:
            pass


# ---------------------------------------------------------------------------
# Button presses (on the inline "cards")
# ---------------------------------------------------------------------------

async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    try:
        action, sid = query.data.split("|", 1)
    except ValueError:
        await query.answer()
        return

    sessions = get_sessions(context)
    session = sessions.get(sid)

    if session is None:
        await query.answer("⌛ This whisper has expired. Please send a new one.", show_alert=True)
        return
    if session["status"] == "delivered":
        await query.answer("✅ This whisper was already delivered.", show_alert=True)
        return
    if time.time() - session["created_at"] > SESSION_TTL_SECONDS:
        sessions.pop(sid, None)
        await query.answer("⌛ This whisper has expired. Please send a new one.", show_alert=True)
        return
    if query.from_user.id != session["sender_id"]:
        await query.answer("🚫 Only the sender can manage this whisper.", show_alert=True)
        return

    if action == CB_CHOOSE:
        await handle_choose_recipient(query, context, session)
    elif action == CB_SEND:
        await handle_send_now(query, context, session)
    else:
        await query.answer()


async def handle_choose_recipient(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: Dict[str, Any]
) -> None:
    """Sender pressed '🎯 Choose Recipient' -> DM them for a @username."""
    session["status"] = "awaiting_recipient"
    session["inline_message_id"] = query.inline_message_id or session.get("inline_message_id")
    get_awaiting(context)[str(session["sender_id"])] = session["sid"]

    prompt = (
        f"🤫 <b>Whisper {session['whisper_id']}</b>\n\n"
        "Send me the <b>@username</b> of the person who should receive this whisper.\n\n"
        "Example: <code>@rahul</code>"
    )
    try:
        await context.bot.send_message(
            chat_id=session["sender_id"], text=prompt, parse_mode=ParseMode.HTML
        )
    except Forbidden:
        get_awaiting(context).pop(str(session["sender_id"]), None)
        session["status"] = "pending"
        await query.answer(
            "📩 I can't message you privately. Open my chat, press Start, then tap the "
            "button again.",
            show_alert=True,
        )
        return
    except TelegramError as exc:
        logger.error("Could not DM the recipient prompt to %s: %s", session["sender_id"], exc)
        get_awaiting(context).pop(str(session["sender_id"]), None)
        session["status"] = "pending"
        await query.answer(
            "⚠️ Something went wrong. Please tap the button again.", show_alert=True
        )
        return

    await query.answer("📩 Check my private message to choose the recipient.")

    # Visible feedback on the card, but keep the button usable for retries.
    if session.get("inline_message_id"):
        try:
            await context.bot.edit_message_text(
                inline_message_id=session["inline_message_id"],
                text=build_pending_card(session, choosing=True),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(
                        text="🎯 Choose Recipient",
                        callback_data=f"{CB_CHOOSE}|{session['sid']}",
                    )]]
                ),
            )
        except TelegramError as exc:
            logger.debug("Could not update whisper card: %s", exc)


async def handle_send_now(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: Dict[str, Any]
) -> None:
    """Sender pressed '📨 Send Whisper' (recipient @username from the query)."""
    username = session.get("recipient_username")
    if not username:
        await query.answer("⚠️ No recipient stored for this whisper.", show_alert=True)
        return

    recipient_chat, error = await resolve_recipient(context, username)
    if recipient_chat is None:
        await query.answer(error, show_alert=True)
        return

    delivered = await deliver_whisper(context, session, recipient_chat)
    if delivered:
        await query.answer("✅ Whisper delivered!")
    else:
        await query.answer(
            f"❌ @{username} can't receive whispers yet.\n\n"
            "They must open my chat and press Start first.",
            show_alert=True,
        )


# ---------------------------------------------------------------------------
# Private chat: sender replies with the recipient's @username
# ---------------------------------------------------------------------------

async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if message is None or user is None or not message.text:
        return

    awaiting = get_awaiting(context)
    sid = awaiting.get(str(user.id))

    if sid is None:
        await message.reply_text(
            "🤫 I deliver whispers!\n\n"
            "Type /help to learn how to use me, or send /game to play. 🎮"
        )
        return

    sessions = get_sessions(context)
    session = sessions.get(sid)
    if (
        session is None
        or session["status"] == "delivered"
        or time.time() - session["created_at"] > SESSION_TTL_SECONDS
    ):
        awaiting.pop(str(user.id), None)
        await message.reply_text("⌛ That whisper has expired. Please send a new one.")
        return

    username = parse_username(message.text)
    if username is None:
        await message.reply_text(
            "❌ That doesn't look like a Telegram username.\n"
            "Send it like this: <code>@rahul</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    recipient_chat, error = await resolve_recipient(context, username)
    if recipient_chat is None:
        await message.reply_text(error, parse_mode=ParseMode.HTML)
        return  # Keep the session so the sender can try another username.

    delivered = await deliver_whisper(context, session, recipient_chat)
    if delivered:
        awaiting.pop(str(user.id), None)
        await message.reply_text(
            f"✅ Whisper <b>{session['whisper_id']}</b> delivered to "
            f"{fmt_display_name(session.get('recipient_name'), session.get('recipient_username'))}! 🤫",
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.reply_text(
            f"❌ @{html.escape(username)} can't receive whispers yet.\n"
            "They must open my chat and press Start first.\n\n"
            "Send another @username to try again."
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
    application.bot_data[AWAITING_KEY] = {}

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("game", cmd_game))
    application.add_handler(InlineQueryHandler(on_inline_query))
    application.add_handler(CallbackQueryHandler(on_callback_query, pattern=r"^[cs]\|"))
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
    Build the bot application and start long polling.

    Blocks the calling thread (live.py runs this in a background thread so
    the Flask web server stays responsive).
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
        )
    except InvalidToken:
        logger.critical("BOT_TOKEN is invalid. Get a fresh token from @BotFather.")
    except TelegramError as exc:
        logger.critical("Telegram bot stopped with an API error: %s", exc)
    logger.info("Telegram bot polling stopped.")
