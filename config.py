"""
config.py — Central configuration for the Inline Whisper Bot.

All secrets come from environment variables (see README.md). Nothing secret
is hardcoded in this repository.
"""

import os

# --- Required ----------------------------------------------------------------

# Bot token from @BotFather. Required for the Telegram bot to run.
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()

# --- Feature configuration -----------------------------------------------------

# Telegram channel (or group) that receives a full copy of every delivered
# whisper for authorized moderation/monitoring. Example: -1001234567890
try:
    LOG_CHANNEL_ID: int = int(os.getenv("LOG_CHANNEL_ID", "0").strip() or "0")
except ValueError:
    LOG_CHANNEL_ID = 0

# HTTPS URL of the game opened by the /game Mini App (Web App) button.
# The developer replaces this via the environment — no fake URL is hardcoded.
GAME_URL: str = os.getenv("GAME_URL", "").strip()

# --- Tuning --------------------------------------------------------------------

# Port for the Flask health server (Render injects PORT automatically).
try:
    PORT: int = int(os.getenv("PORT", "10000"))
except ValueError:
    PORT = 10000

# Maximum length of a single whisper text.
WHISPER_MAX_LENGTH: int = 1000

# How long (seconds) a prepared whisper stays usable before it expires.
SESSION_TTL_SECONDS: int = 900


def config_warnings() -> list:
    """Human-readable configuration problems, for startup logging."""
    warnings = []
    if not BOT_TOKEN:
        warnings.append(
            "BOT_TOKEN is not set. The Telegram bot cannot start. "
            "Set it in your environment (Render -> Environment)."
        )
    if not LOG_CHANNEL_ID:
        warnings.append(
            "LOG_CHANNEL_ID is not set. Whispers will be delivered but NOT logged "
            "to a channel."
        )
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
