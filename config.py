"""
config.py — Central configuration for the Inline Whisper Bot.

All secrets come from environment variables (see README.md). Nothing secret
is hardcoded in this repository.

Local development: put your variables in a `.env` file in the project root.
It is loaded automatically if python-dotenv is installed. Real environment
variables (e.g. set on the Render dashboard) ALWAYS take priority.
"""

import os
from pathlib import Path
from typing import Optional, Tuple, Union

# --- Optional .env support (local development) --------------------------------
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    # python-dotenv not installed — plain environment variables still work.
    pass

# --- Required -------------------------------------------------------------------

# Bot token from @BotFather. Required for the Telegram bot to run.
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()

# --- Owners (owner-only auth management) ------------------------------------------
# Comma- or space-separated Telegram user IDs that OWN the bot. Owners are the
# only ones who may manage the trusted auth list via /addauth and /rauth.
# Owners always keep owner rights and can never be removed with /rauth.
#
#   OWNER_IDS=123456789,987654321
#
OWNER_IDS: list = []
for _part in os.getenv("OWNER_IDS", "").replace(";", ",").replace(" ", ",").split(","):
    _part = _part.strip()
    if _part and _part.lstrip("-").isdigit():
        OWNER_IDS.append(int(_part))

# --- Log channel destination ------------------------------------------------------
# LOG_CHANNEL is THE destination for whisper logs. It accepts:
#
#   @MyWhisperLogs        -> public channel username (preferred)
#   MyWhisperLogs         -> same as above (the @ is added automatically)
#   -1001234567890        -> numeric chat/channel ID
#
# IMPORTANT: an invite link such as https://t.me/+xxxxxxxx is NOT a send
# destination and CANNOT be passed to send_message(). If one is configured,
# the bot reports a clear configuration error at startup (see
# log_channel_problem()).
LOG_CHANNEL: str = os.getenv("LOG_CHANNEL", "").strip()

# Legacy fallback: if LOG_CHANNEL is not set, a numeric LOG_CHANNEL_ID is used
# (kept for backward compatibility with earlier deployments).
try:
    LOG_CHANNEL_ID: int = int(os.getenv("LOG_CHANNEL_ID", "0").strip() or "0")
except ValueError:
    LOG_CHANNEL_ID = 0


def _resolve_log_destination() -> Tuple[Optional[Union[str, int]], Optional[Tuple[str, str]]]:
    """
    Turn LOG_CHANNEL / LOG_CHANNEL_ID into a Telegram send_message destination.

    Returns (destination, None) on success, or (None, (problem_kind, raw_value))
    when the configuration cannot be used.
    """
    raw = LOG_CHANNEL
    if raw:
        low = raw.lower()
        if low.startswith(("https://t.me/", "http://t.me/", "t.me/")):
            # Invite links are NOT send destinations — reject loudly.
            return None, ("invite-link", raw)
        if raw.startswith("@"):
            return raw, None
        if raw.lstrip("-").isdigit():
            return int(raw), None
        if raw.replace("_", "").isalnum():
            # Bare public username without the leading @.
            return "@" + raw, None
        return None, ("invalid", raw)
    if LOG_CHANNEL_ID:
        return LOG_CHANNEL_ID, None
    return None, ("unset", "")


LOG_CHANNEL_DEST, _LOG_CHANNEL_PROBLEM = _resolve_log_destination()


def log_channel_problem() -> Optional[str]:
    """Human-readable reason why LOG_CHANNEL is unusable (None = configured OK)."""
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

# HTTPS URL of the game opened by the /game command's Mini App button.
# Set on Render as an environment variable — never hardcoded.
GAME_URL = os.getenv("GAME_URL", "").strip()

# --- Tuning ------------------------------------------------------------------------

# Port for the Flask health server (Render injects PORT automatically).
try:
    PORT: int = int(os.getenv("PORT", "10000"))
except ValueError:
    PORT = 10000

# Maximum length of a single whisper text.
WHISPER_MAX_LENGTH: int = 1000

# How long (seconds) a whisper stays readable before it expires (15 minutes).
SESSION_TTL_SECONDS: int = 900


def config_warnings() -> list:
    """Human-readable configuration problems, for startup logging."""
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
