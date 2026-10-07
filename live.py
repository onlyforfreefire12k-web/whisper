"""
live.py — Render Web Service entry point.

Runs two components side by side without blocking each other:

  1. Flask web server (main thread)
       GET  /                    -> "Whisper Bot is running"
       GET  /health              -> JSON health/status report
       GET  /reader              -> Whispry Mini App reader (dark, scrollable)
       POST /api/whisper/open    -> secure whisper-open API (token + initData)
     bound to 0.0.0.0:$PORT (Render injects PORT; defaults to 10000).

  2. Telegram bot (daemon background thread)
       Long polling via python-telegram-bot; all logic in bot.py.
       The thread's asyncio event loop is created explicitly inside
       bot.start_bot_thread() — required on Python 3.10+/3.12.

SECURITY (Mini App reader):
  - The reader page never receives whisper data directly; it POSTs the
    short-lived access token plus Telegram WebApp initData to
    /api/whisper/open.
  - bot.open_whisper_via_webapp() validates initData with Telegram's official
    HMAC-SHA256 scheme, verifies the target identity server-side, enforces
    expiration and once/timed view states, and only then returns content.
  - The bot token never reaches the frontend; no whisper/target IDs are in
    any URL; failures return JSON status codes the UI understands.

Render Start Command:

    python live.py

No Gunicorn and no Docker are required.
"""

import logging
import sys
import threading
from datetime import datetime, timezone
from typing import Optional

from flask import Flask, Response, jsonify, request

from bot import get_bot_error, open_whisper_via_webapp, start_bot_thread
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
        "reader": "enabled" if READER_ENABLED else "disabled",
        "time": datetime.now(timezone.utc).strftime("%d %b %Y, %I:%M %p UTC"),
    }
    bot_error = get_bot_error()
    if bot_error:
        payload["bot_error"] = bot_error
    return jsonify(payload), 200


# ---------------------------------------------------------------------------
# Mini App whisper reader (served by the same Render service)
# ---------------------------------------------------------------------------

READER_ENABLED = False  # set in main() from bot/config state


@app.get("/reader")
def reader() -> Response:
    """The Whispry Mini App reader page (dark, private, scrollable)."""
    return Response(READER_HTML, mimetype="text/html")


@app.post("/api/whisper/open")
def api_whisper_open() -> tuple:
    """
    Secure whisper-open API.

    Body (JSON): {"token": "<short-lived access token>", "initData": "<raw>"}

    All authorization happens inside bot.open_whisper_via_webapp():
      1. token valid + not expired
      2. Telegram initData cryptographically validated (official scheme)
      3. verified Telegram user id == stored whisper target
      4. whisper not expired; once/timed states enforced server-side

    Responses are JSON status objects; no content is ever returned to
    unauthorized callers. Never crashes: unexpected errors -> 500 JSON.
    """
    data = request.get_json(silent=True) or {}
    token = str(data.get("token") or "")[:200]
    init_data = str(data.get("initData") or "")[:8192]

    if not token or not init_data:
        return jsonify({"status": "invalid_token"}), 400

    try:
        result = open_whisper_via_webapp(token, init_data)
    except Exception:
        logger.exception("Whisper reader API failed unexpectedly")
        return jsonify({"status": "error"}), 500

    return jsonify(result), 200


# ---------------------------------------------------------------------------
# Reader frontend (single self-contained page — no build tools, no CDN CSS)
# ---------------------------------------------------------------------------

READER_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Whispry — Private Whisper</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --bg:#0b0e13; --bg2:#10151f; --card:#141a24; --card2:#1a212e;
  --line:#212a38; --text:#e9e7e2; --muted:#8b93a3; --accent:#d8b06a;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  background:linear-gradient(180deg,var(--bg) 0%,var(--bg2) 100%);
  color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  display:flex;flex-direction:column;min-height:100vh;
  -webkit-tap-highlight-color:transparent;overflow-x:hidden;
}
.wrap{flex:1;display:flex;flex-direction:column;width:100%;max-width:560px;margin:0 auto;padding:16px 18px 12px}
header{text-align:center;padding:14px 0 4px}
.lock{width:64px;height:64px;border-radius:50%;background:var(--card2);display:flex;align-items:center;justify-content:center;font-size:30px;margin:0 auto 14px;box-shadow:0 0 0 1px var(--line),0 10px 30px rgba(0,0,0,.4)}
h1{font-size:15px;letter-spacing:.35em;color:var(--accent);font-weight:600}
.sub{color:var(--muted);font-size:13px;margin-top:6px;letter-spacing:.08em}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;margin-top:16px;box-shadow:0 12px 40px rgba(0,0,0,.35);overflow:hidden;display:flex;flex-direction:column;flex:1;min-height:240px}
.meta{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:12px;color:var(--muted)}
.meta .who{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badges{display:flex;gap:6px;flex-shrink:0}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;background:#1d2431;color:var(--accent);font-size:11px;letter-spacing:.05em;white-space:nowrap}
.badge.count{display:none}
.content{padding:20px 18px;overflow-y:auto;flex:1;font-size:16px;line-height:1.7;
  white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;
  -webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
.content .msg{min-height:1em}
.state{padding:36px 22px;text-align:center}
.state .icon{font-size:36px;margin-bottom:14px}
.state .title{font-size:17px;font-weight:600;margin-bottom:8px}
.state .desc{color:var(--muted);font-size:13.5px;line-height:1.6}
.center{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:200px;width:100%}
.spinner{width:28px;height:28px;border:3px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:spin .9s linear infinite;margin-bottom:14px}
@keyframes spin{to{transform:rotate(360deg)}}
footer{padding:12px;text-align:center;color:var(--muted);font-size:12px;border-top:1px solid var(--line)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="lock">🔐</div>
    <h1>WHISPRY</h1>
    <div class="sub">Private Whisper</div>
  </header>
  <div class="card">
    <div class="meta">
      <span class="who" id="from">—</span>
      <span class="badges">
        <span class="badge count" id="countdown">⏱</span>
        <span class="badge" id="mode">🔐 Private</span>
      </span>
    </div>
    <div class="content" id="content"></div>
  </div>
  <footer>🔒 Only you can view this whisper</footer>
</div>
<script>
(function(){
  var params = new URLSearchParams(location.search);
  var token = params.get('t') || '';
  var tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  try {
    if (tg) {
      tg.ready(); tg.expand();
      if (tg.disableVerticalSwipes) tg.disableVerticalSwipes();
      if (tg.setHeaderColor) tg.setHeaderColor('#0b0e13');
      if (tg.setBackgroundColor) tg.setBackgroundColor('#0b0e13');
    }
  } catch (e) {}

  var contentEl = document.getElementById('content');
  var fromEl = document.getElementById('from');
  var modeEl = document.getElementById('mode');
  var countEl = document.getElementById('countdown');
  var timerId = null;

  // Copy prevention "where practical" — this is convenience hardening,
  // NOT DRM: screenshots/recording can never be fully prevented.
  document.addEventListener('copy', function(e){ e.preventDefault(); });
  document.addEventListener('contextmenu', function(e){ e.preventDefault(); });

  function clearTimer(){ if (timerId) { clearInterval(timerId); timerId = null; } countEl.style.display = 'none'; }

  function setState(icon, title, desc){
    clearTimer();
    fromEl.textContent = '—';
    modeEl.textContent = '🔐 Private';
    contentEl.innerHTML = '';
    var box = document.createElement('div'); box.className = 'state';
    var i = document.createElement('div'); i.className = 'icon'; i.textContent = icon;
    var t = document.createElement('div'); t.className = 'title'; t.textContent = title;
    var d = document.createElement('div'); d.className = 'desc'; d.textContent = desc;
    box.appendChild(i); box.appendChild(t); box.appendChild(d);
    contentEl.appendChild(box);
  }

  function startCountdown(sec){
    var remain = sec;
    countEl.style.display = 'inline-block';
    function paint(){ countEl.textContent = '⏱ ' + remain + 's'; }
    paint();
    timerId = setInterval(function(){
      remain -= 1;
      if (remain <= 0){
        clearTimer();
        setState('⏱', "Time's Up", 'The viewing time for this whisper has ended. The server has locked it.');
        return;
      }
      paint();
    }, 1000);
  }

  function render(d){
    fromEl.textContent = d.sender || 'Unknown';
    modeEl.textContent = d.mode_label || 'Normal';
    contentEl.innerHTML = '';
    var msg = document.createElement('div'); msg.className = 'msg';
    msg.textContent = d.text || '';
    contentEl.appendChild(msg);
    if (typeof d.remaining === 'number' && d.remaining > 0){ startCountdown(d.remaining); }
  }

  function handle(d){
    if (d && d.status === 'ok'){ render(d); return; }
    var s = d ? d.status : 'error';
    switch (s) {
      case 'denied':          setState('🚫', 'Access Denied', 'This whisper is not addressed to you.'); break;
      case 'whisper_expired': setState('🔒', 'Whisper Expired', 'This whisper is no longer available.'); break;
      case 'already_viewed':  setState('👁', 'Whisper Already Viewed', 'This whisper has already been viewed.'); break;
      case 'timed_expired':   setState('⏱', "Time's Up", 'The viewing time for this whisper has ended.'); break;
      case 'invalid_token':   setState('🔗', 'Link Expired', 'This reader link is invalid or expired. Press 🔐 on the whisper card again.'); break;
      case 'unauthorized':    setState('🚫', 'Access Denied', 'Could not verify your Telegram identity. Open this reader from the button in the bot\\'s private chat.'); break;
      case 'media':           setState('🖼', 'Media Whisper', 'Press 🔐 on the whisper card in the chat to receive the media privately.'); break;
      default:                setState('⚠️', 'Something went wrong', 'Please close this reader and try again.');
    }
  }

  async function open(){
    contentEl.innerHTML = '';
    var c = document.createElement('div'); c.className = 'center';
    var sp = document.createElement('div'); sp.className = 'spinner';
    var lb = document.createElement('div');
    lb.style.cssText = 'color:var(--muted);font-size:13px';
    lb.textContent = 'Decrypting securely…';
    c.appendChild(sp); c.appendChild(lb);
    contentEl.appendChild(c);

    var initData = tg ? (tg.initData || '') : '';
    try {
      var res = await fetch('/api/whisper/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: token, initData: initData })
      });
      var d = await res.json();
      handle(d);
    } catch (e) {
      setState('⚠️', 'Connection Error', 'Could not reach the Whispry server. Check your connection and try again.');
    }
  }

  open();
})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    global _bot_thread, READER_ENABLED

    # Surface configuration problems early and clearly in the logs.
    for warning in config_warnings():
        logger.warning("%s", warning)

    # Reader availability (for /health reporting and logging).
    try:
        from config import WEBAPP_URL

        READER_ENABLED = bool(WEBAPP_URL)
    except ImportError:
        READER_ENABLED = False

    # 1) Telegram bot in a background thread (daemon -> dies with the process).
    #    start_bot_thread() creates and installs the thread's asyncio event
    #    loop explicitly (Python 3.10+/3.12 do not do this automatically for
    #    non-main threads) and then starts long polling. Flask in the main
    #    thread is never blocked, and polling never blocks Flask.
    _bot_thread = start_bot_thread()
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
