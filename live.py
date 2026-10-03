"""
live.py — Render Web Service entry point.

Runs two components side by side without blocking each other:

  1. Flask web server (main thread)
       GET /        -> "Whisper Bot is running"
       GET /health  -> JSON health/status report
     bound to 0.0.0.0:$PORT (Render injects PORT; defaults to 10000).

  2. Telegram bot (daemon background thread)
       Long polling via python-telegram-bot; all logic in bot.py.

Render Start Command:

    python live.py

No Gunicorn and no Docker are required. The Flask app only serves trivial
health responses, so Werkzeug's built-in server is fully sufficient here.
"""

import logging
import sys
import threading
from datetime import datetime, timezone
from typing import Optional

from flask import Flask, jsonify

from bot import run_bot
from config import PORT, config_warnings

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("live")

# ---------------------------------------------------------------------------
# Flask health server
# ---------------------------------------------------------------------------

app = Flask(__name__)

_bot_thread: Optional[threading.Thread] = None
_bot_error: Optional[str] = None


def _bot_worker() -> None:
    """Runs the Telegram bot in this background thread until it stops."""
    global _bot_error
    try:
        run_bot()
    except Exception as exc:  # Never let the thread die silently.
        _bot_error = f"{type(exc).__name__}: {exc}"
        logger.exception("Telegram bot thread crashed!")


@app.get("/")
def index() -> tuple:
    return "Whisper Bot is running", 200


@app.get("/health")
def health() -> tuple:
    bot_alive = _bot_thread is not None and _bot_thread.is_alive()
    payload = {
        "status": "ok" if bot_alive else "degraded",
        "service": "inline-whisper-bot",
        "bot": "running" if bot_alive else "stopped",
        "time": datetime.now(timezone.utc).strftime("%d %b %Y, %I:%M %p UTC"),
    }
    if _bot_error:
        payload["bot_error"] = _bot_error
    return jsonify(payload), 200


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    global _bot_thread

    # Surface configuration problems early and clearly in the logs.
    for warning in config_warnings():
        logger.warning("%s", warning)

    # 1) Telegram bot in a background thread (daemon -> dies with the process).
    #    run_polling() creates its own asyncio event loop inside this thread,
    #    so it does not interfere with Flask in the main thread.
    _bot_thread = threading.Thread(target=_bot_worker, name="telegram-bot", daemon=True)
    _bot_thread.start()
    logger.info("Telegram bot thread started.")

    # 2) Flask web server in the main thread (blocks until shutdown).
    logger.info("Starting Flask web server on 0.0.0.0:%s", PORT)
    try:
        app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)
    except Exception as exc:
        logger.critical("Flask failed to start on port %s: %s", PORT, exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
