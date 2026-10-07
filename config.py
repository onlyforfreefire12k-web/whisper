"""
config.py — Central configuration for the Whispry bot.

All secrets come from environment variables. Nothing secret is hardcoded.
"""

import os
from pathlib import Path
from typing import Optional, Tuple, Union

# --- Optional .env support (local development) --------------------------------
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

# --- Required -------------------------------------------------------------------

# Bot token from @BotFather.
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()

# --- Owners (owner-only auth management) ------------------------------------------
#   OWNER_IDS=123456789,987654321
OWNER_IDS: list = []
for _part in os.getenv("OWNER_IDS", "").replace(";", ",").replace(" ", ",").split(","):
    _part = _part.strip()
    if _part and _part.lstrip("-").isdigit():
        OWNER_IDS.append(int(_part))

# --- Log channel destination ------------------------------------------------------
#   @MyWhisperLogs  |  MyWhisperLogs  |  -1001234567890
# Invite links (https://t.me/+xxxx) are NOT send destinations and are rejected.
LOG_CHANNEL: str = os.getenv("LOG_CHANNEL", "").strip()

try:
    LOG_CHANNEL_ID: int = int(os.getenv("LOG_CHANNEL_ID", "0").strip() or "0")
except ValueError:
    LOG_CHANNEL_ID = 0


def _resolve_log_destination() -> Tuple[Optional[Union[str, int]], Optional[Tuple[str, str]]]:
    raw = LOG_CHANNEL
    if raw:
        low = raw.lower()
        if low.startswith(("https://t.me/", "http://t.me/", "t.me/")):
            return None, ("invite-link", raw)
        if raw.startswith("@"):
            return raw, None
        if raw.lstrip("-").isdigit():
            return int(raw), None
        if raw.replace("_", "").isalnum():
            return "@" + raw, None
        return None, ("invalid", raw)
    if LOG_CHANNEL_ID:
        return LOG_CHANNEL_ID, None
    return None, ("unset", "")


LOG_CHANNEL_DEST, _LOG_CHANNEL_PROBLEM = _resolve_log_destination()


def log_channel_problem() -> Optional[str]:
    if _LOG_CHANNEL_PROBLEM is None:
        return None
    kind, value = _LOG_CHANNEL_PROBLEM
    if kind == "invite-link":
        return (
            f"LOG_CHANNEL='{value}' is an invite link — invite links are NOT send "
            "destinations. Use the channel's public @username (e.g. @MyWhisperLogs) "
            "or its numeric ID (e.g. -1001234567890)."
        )
    if kind == "invalid":
        return (
            f"LOG_CHANNEL='{value}' is not a valid destination — use a public "
            "@username (e.g. @MyWhisperLogs) or a numeric chat ID."
        )
    return "LOG_CHANNEL is not set — whispers will NOT be logged to a channel."

# --- Game (Telegram Mini App / Web App) ------------------------------------------

GAME_URL = os.getenv("GAME_URL", "").strip()

# --- Private Whisper Reader (Direct-Link Mini App) ---------------------------------
# The reader opens DIRECTLY from the group card button (no DM, no /start).
#
# SETUP (one time):
#   1. BotFather -> /newapp -> attach -> Web App URL:
#        https://<your-service>.onrender.com/reader
#   2. Choose an app short name (e.g. "whisper").
#   3. Set the env var below to that short name.
#
# The bot then builds buttons as:
#   https://t.me/<bot_username>/<READER_APP_SHORT_NAME>?startapp=<token>
#
# Direct-Link Mini Apps receive SIGNED Telegram initData (verified identity)
# and work even for users who never pressed Start — exactly what the reader
# needs. Without this value the reader is DISABLED and no whisper content is
# ever revealed (a startup error explains the setup).
READER_APP_SHORT_NAME: str = os.getenv("READER_APP_SHORT_NAME", "").strip()

# DEPRECATED: previously used for a DM-based reader. Kept only so existing
# environments don't break; it is no longer required by any feature.
WEBAPP_URL: str = os.getenv("WEBAPP_URL", "").strip()

# --- Tuning ------------------------------------------------------------------------

try:
    PORT: int = int(os.getenv("PORT", "10000"))
except ValueError:
    PORT = 10000

# Maximum whisper text length. Telegram caps private-chat messages at 4096
# chars, so long whispers are created by sending the text to the bot's PM
# first (inline query typing itself is capped at 256 chars by Telegram).
WHISPER_MAX_LENGTH: int = 4000

# How long (seconds) a whisper stays readable before it expires (15 minutes).
SESSION_TTL_SECONDS: int = 900

# Private-chat texts longer than this become "pending long text" for the
# sender's next whisper (inline queries cannot carry this much text).
LONG_TEXT_PENDING_THRESHOLD: int = 200


def config_warnings() -> list:
    warnings = []
    if not BOT_TOKEN:
        warnings.append(
            "BOT_TOKEN is not set. The Telegram bot cannot start. "
            "Set it in your .env file or environment."
        )
    if not OWNER_IDS:
        warnings.append(
            "OWNER_IDS is not set. Nobody will be able to use /addauth or /rauth "
            "(and therefore nobody can manage /gmute)."
        )
    problem = log_channel_problem()
    if problem:
        warnings.append(problem)
    if not GAME_URL:
        warnings.append(
            "GAME_URL is not set. The /game command will reply that the game is "
            "not configured."
        )
    elif not GAME_URL.lower().startswith("https://"):
        warnings.append(
            "GAME_URL should start with https:// — Telegram Mini App (Web App) "
            "buttons require HTTPS."
        )
    return warnings
