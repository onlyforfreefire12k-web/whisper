"""
storage.py — Minimal JSON file persistence for Whispry.

The project intentionally has no database. This module provides the smallest
reliable persistent solution: JSON files next to bot.py, written ATOMICALLY
(write to a temp file, then os.replace) so a crash can never leave a
corrupted file behind. Loads are corruption-safe (bad/missing file -> default).

Files used by the bot:
    gmute_users.json       -> list of globally muted Telegram user IDs
    auth_users.json        -> list of trusted/auth Telegram user IDs
    recipient_history.json -> {"<sender_id>": [recipient entries]} (most recent first)

NOTE (Render): the service filesystem survives restarts but is reset on
redeploys unless a Render Disk is attached. Attach a Disk mounted at the
project directory for full persistence across deploys.
"""

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent
_write_lock = threading.Lock()


def load_json(filename: str, default: Any) -> Any:
    """Load a JSON file; return `default` if missing or corrupted (never raises)."""
    path = _BASE_DIR / filename
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Could not read %s (%s) — using default value.", filename, exc)
        return default


def save_json(filename: str, data: Any) -> bool:
    """
    Atomically write `data` to `filename`.

    Returns True on success, False on failure (caller decides whether to
    log and continue — persistence failures must never crash the bot).
    """
    path = _BASE_DIR / filename
    tmp_path = path.with_name(path.name + ".tmp")
    with _write_lock:
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp_path, path)  # atomic on POSIX and Windows
            return True
        except OSError as exc:
            logger.error("Could not save %s: %s", filename, exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False
