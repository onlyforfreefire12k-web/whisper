"""
live.py — Render Web Service entry point.

  1. Flask (main thread):  GET /  |  GET /health  |  GET /reader
                           POST /api/whisper/open
                           POST /api/whisper/media   (authenticated media stream)
                           POST /api/whisper/deliver (oversized-media fallback)
  2. Telegram bot (daemon thread): long polling; all logic in bot.py.

Reader security: every API call carries the short-lived token AND Telegram
WebApp initData; bot.py validates initData with the official HMAC scheme and
enforces target/expiry/once/timed state server-side. The bot token and raw
media URLs never reach the frontend.

Render Start Command: python live.py
"""

import json
import logging
import sys
import threading
from datetime import datetime, timezone
from typing import Optional

from flask import Flask, Response, jsonify, request

from bot import (
    get_bot_error,
    open_whisper_via_webapp,
    reader_deliver_request,
    reader_media_request,
    start_bot_thread,
)
from config import PORT, READER_APP_SHORT_NAME, config_warnings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("live")

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
        "reader": "enabled" if READER_APP_SHORT_NAME else "disabled",
        "time": datetime.now(timezone.utc).strftime("%d %b %Y, %I:%M %p UTC"),
    }
    bot_error = get_bot_error()
    if bot_error:
        payload["bot_error"] = bot_error
    return jsonify(payload), 200


# ---------------------------------------------------------------------------
# Reader APIs — all authorization inside bot.py
# ---------------------------------------------------------------------------

def _json_body() -> dict:
    data = request.get_json(silent=True) or {}
    return {
        "token": str(data.get("token") or "")[:200],
        "initData": str(data.get("initData") or "")[:8192],
    }


@app.get("/reader")
def reader() -> Response:
    """Whispry Mini App reader page (dark, private, scrollable)."""
    return Response(READER_HTML, mimetype="text/html")


@app.post("/api/whisper/open")
def api_whisper_open() -> tuple:
    body = _json_body()
    if not body["token"] or not body["initData"]:
        return jsonify({"status": "invalid_token"}), 400
    try:
        result = open_whisper_via_webapp(body["token"], body["initData"])
    except Exception:
        logger.exception("Whisper reader API failed unexpectedly")
        return jsonify({"status": "error"}), 500
    return jsonify(result), 200


@app.post("/api/whisper/media")
def api_whisper_media() -> tuple:
    """Authenticated media stream (bytes) — metadata via X-Whisper-Meta header."""
    body = _json_body()
    if not body["token"] or not body["initData"]:
        return jsonify({"status": "invalid_token"}), 400
    try:
        result = reader_media_request(body["token"], body["initData"])
    except Exception:
        logger.exception("Whisper media API failed unexpectedly")
        return jsonify({"status": "error"}), 500

    if result.get("status") == "ok_media_data":
        data = result.pop("data")
        meta = {k: v for k, v in result.items() if k != "data"}
        resp = Response(data, mimetype=meta.get("mime") or "application/octet-stream")
        resp.headers["X-Whisper-Meta"] = json.dumps(meta)
        resp.headers["Cache-Control"] = "no-store"
        return resp, 200
    return jsonify(result), 200


@app.post("/api/whisper/deliver")
def api_whisper_deliver() -> tuple:
    """Verified fallback: send oversized media to the user's Telegram chat."""
    body = _json_body()
    if not body["token"] or not body["initData"]:
        return jsonify({"status": "invalid_token"}), 400
    try:
        result = reader_deliver_request(body["token"], body["initData"])
    except Exception:
        logger.exception("Whisper deliver API failed unexpectedly")
        return jsonify({"status": "error"}), 500
    return jsonify(result), 200


# ---------------------------------------------------------------------------
# Reader frontend (single self-contained page)
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
.badges{display:flex;gap:6px;flex-shrink:0;flex-wrap:wrap;justify-content:flex-end}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;background:#1d2431;color:var(--accent);font-size:11px;letter-spacing:.05em;white-space:nowrap}
.badge.count,.badge.exp{display:none}
.content{padding:20px 18px;overflow-y:auto;flex:1;font-size:16px;line-height:1.7;
  white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;
  -webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
.content .msg{min-height:1em}
.content .cap{margin-top:14px;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:14px}
.content img,.content video{max-width:100%;border-radius:10px;display:block;margin:0 auto}
.content audio{width:100%;margin-top:6px}
.dl{display:inline-block;margin-top:12px;padding:10px 18px;border-radius:10px;background:var(--accent);color:#141a24;font-weight:600;text-decoration:none;font-size:14px}
.state{padding:36px 22px;text-align:center}
.state .icon{font-size:36px;margin-bottom:14px}
.state .title{font-size:17px;font-weight:600;margin-bottom:8px}
.state .desc{color:var(--muted);font-size:13.5px;line-height:1.6}
.state button{margin-top:18px;padding:11px 20px;border:0;border-radius:10px;background:var(--accent);color:#141a24;font-weight:600;font-size:14px;cursor:pointer}
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
    <div class="sub" id="wid">Private Whisper</div>
  </header>
  <div class="card">
    <div class="meta">
      <span class="who" id="from">—</span>
      <span class="badges">
        <span class="badge exp" id="expiry">⏳</span>
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
  var tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  try {
    if (tg) {
      tg.ready(); tg.expand();
      if (tg.disableVerticalSwipes) tg.disableVerticalSwipes();
      if (tg.setHeaderColor) tg.setHeaderColor('#0b0e13');
      if (tg.setBackgroundColor) tg.setBackgroundColor('#0b0e13');
    }
  } catch (e) {}

  var params = new URLSearchParams(location.search);
  // startapp param arrives via initDataUnsafe.start_param for Mini Apps.
  var token = (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param)
           || params.get('startapp') || params.get('t') || '';
  var initData = tg ? (tg.initData || '') : '';

  var contentEl = document.getElementById('content');
  var fromEl = document.getElementById('from');
  var widEl = document.getElementById('wid');
  var modeEl = document.getElementById('mode');
  var countEl = document.getElementById('countdown');
  var expEl = document.getElementById('expiry');
  var timerId = null, expTimerId = null;

  document.addEventListener('copy', function(e){ e.preventDefault(); });
  document.addEventListener('contextmenu', function(e){ e.preventDefault(); });

  function clearTimers(){
    if (timerId){ clearInterval(timerId); timerId=null; }
    if (expTimerId){ clearInterval(expTimerId); expTimerId=null; }
    countEl.style.display='none';
  }

  function fmtClock(sec){
    var m=Math.floor(sec/60), s=sec%60;
    return (m<10?'0':'')+m+':'+(s<10?'0':'')+s;
  }

  function startCountdown(sec){
    var remain=sec;
    countEl.style.display='inline-block';
    function paint(){ countEl.textContent='⏱ '+remain+'s'; }
    paint();
    timerId=setInterval(function(){
      remain-=1;
      if(remain<=0){
        clearTimers();
        setState('⏱',"Time's Up",'The viewing time for this whisper has ended. The server has locked it.');
        return;
      }
      paint();
    },1000);
  }

  function startExpiry(sec){
    if(!sec||sec<=0) return;
    var remain=sec;
    expEl.style.display='inline-block';
    function paint(){ expEl.textContent='⏳ '+fmtClock(remain); }
    paint();
    expTimerId=setInterval(function(){
      remain-=1;
      if(remain<=0){ expEl.style.display='none'; if(expTimerId){clearInterval(expTimerId);expTimerId=null;} return; }
      paint();
    },1000);
  }

  function setState(icon,title,desc,extraButton){
    clearTimers();
    fromEl.textContent='—';
    modeEl.textContent='🔐 Private';
    contentEl.innerHTML='';
    var box=document.createElement('div'); box.className='state';
    var i=document.createElement('div'); i.className='icon'; i.textContent=icon;
    var t=document.createElement('div'); t.className='title'; t.textContent=title;
    var d=document.createElement('div'); d.className='desc'; d.textContent=desc;
    box.appendChild(i); box.appendChild(t); box.appendChild(d);
    if(extraButton){
      var b=document.createElement('button'); b.textContent=extraButton.label;
      b.addEventListener('click',extraButton.onClick);
      box.appendChild(b);
    }
    contentEl.appendChild(box);
  }

  function loading(label){
    contentEl.innerHTML='';
    var c=document.createElement('div'); c.className='center';
    var sp=document.createElement('div'); sp.className='spinner';
    var lb=document.createElement('div');
    lb.style.cssText='color:var(--muted);font-size:13px';
    lb.textContent=label||'🔐 Verifying access...';
    c.appendChild(sp); c.appendChild(lb);
    contentEl.appendChild(c);
  }

  function renderText(d){
    widEl.textContent='Whisper #'+(d.whisper_id||'');
    fromEl.textContent=d.sender||'Unknown';
    modeEl.textContent=d.mode_label||'Normal';
    contentEl.innerHTML='';
    var msg=document.createElement('div'); msg.className='msg';
    msg.textContent=d.text||'';
    contentEl.appendChild(msg);
    startExpiry(d.expires_in||0);
    if(typeof d.remaining==='number'&&d.remaining>0){ startCountdown(d.remaining); }
  }

  async function post(url){
    var res=await fetch(url,{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({token:token,initData:initData})
    });
    var ct=res.headers.get('Content-Type')||'';
    if(ct.indexOf('application/json')===-1){
      var meta={};
      try{ meta=JSON.parse(res.headers.get('X-Whisper-Meta')||'{}'); }catch(e){}
      var blob=await res.blob();
      return {json:null,blob:blob,meta:meta};
    }
    return {json:await res.json(),blob:null,meta:null};
  }

  function objectUrlFor(mime,blob){
    return URL.createObjectURL(new Blob([blob],{type:mime}));
  }

  function renderMedia(d,meta){
    widEl.textContent='Whisper #'+(meta.whisper_id||'');
    fromEl.textContent=meta.sender||'Unknown';
    modeEl.textContent=meta.mode_label||'Normal';
    contentEl.innerHTML='';
    var type=meta.media_type||'document';
    var url=objectUrlFor(meta.mime||'application/octet-stream',d.blob);
    if(type==='photo'){
      var img=document.createElement('img'); img.src=url; img.alt='Private whisper photo';
      contentEl.appendChild(img);
    } else if(type==='video'){
      var v=document.createElement('video'); v.src=url; v.controls=true; v.playsInline=true;
      contentEl.appendChild(v);
    } else if(type==='audio'){
      var a=document.createElement('audio'); a.src=url; a.controls=true;
      contentEl.appendChild(a);
    } else {
      var link=document.createElement('a'); link.className='dl'; link.href=url;
      link.download=meta.filename||'whisper-file';
      link.textContent='⬇️ Open / Save file';
      contentEl.appendChild(link);
    }
    if(meta.caption){
      var cap=document.createElement('div'); cap.className='cap';
      cap.textContent=meta.caption;
      contentEl.appendChild(cap);
    }
    startExpiry(meta.expires_in||0);
    if(typeof meta.remaining==='number'&&meta.remaining>0){ startCountdown(meta.remaining); }
  }

  function receiveInChat(){
    loading('📩 Sending to your Telegram chat...');
    post('/api/whisper/deliver').then(function(r){
      var s=r.json?r.json.status:'error';
      if(s==='ok'){ setState('📩','Delivered','Check your Telegram chat — the file was sent to you directly.'); }
      else if(s==='need_start'){ setState('📩','Start Whispry first','Open @'+(window.location.hostname? 'the bot':'bot')+' in Telegram, press Start once, then tap Receive again.'); }
      else { handle(r.json||{status:s}); }
    }).catch(function(){ setState('⚠️','Connection Error','Could not reach the Whispry server.'); });
  }

  function handle(d){
    if(d&&d.status==='ok'){ renderText(d); return; }
    if(d&&d.status==='ok_media'){
      loading('🖼 Preparing media...');
      post('/api/whisper/media').then(function(r){
        if(r.blob){ renderMedia(r,r.meta||{}); return; }
        var s=r.json?r.json.status:'error';
        if(s==='media_too_large'){
          setState('📄','File too large','This file is too large to preview here. You can receive it directly in your Telegram chat.',{label:'📩 Receive in chat',onClick:receiveInChat});
          return;
        }
        handle(r.json);
      }).catch(function(){ setState('⚠️','Connection Error','Could not load the media. Try again in a moment.'); });
      return;
    }
    var s=d?d.status:'error';
    switch(s){
      case 'denied':          setState('🚫','Access denied','This whisper is not intended for you.'); break;
      case 'unauthorized':    setState('🚫','Access denied','Could not verify your Telegram identity. Open the whisper with the 🔐 Read Whisper button.'); break;
      case 'whisper_expired': setState('🔒','Whisper expired','This whisper is no longer available.'); break;
      case 'already_viewed':  setState('👁','Whisper already viewed','This whisper has already been viewed.'); break;
      case 'timed_expired':   setState('⏱',"Time's Up",'The viewing time for this whisper has ended.'); break;
      case 'invalid_token':   setState('🔗','Link expired','This reader link is invalid or expired. Press 🔐 Read Whisper on the card again.'); break;
      case 'delivery_failed': setState('⚠️','Could not deliver','The file could not be delivered right now. Tap Receive again in a moment.',{label:'📩 Receive in chat',onClick:receiveInChat}); break;
      default:                setState('⚠️','Something went wrong','Please close this viewer and try again.');
    }
  }

  if(!token){ setState('🔗','Link expired','Open the whisper with the 🔐 Read Whisper button on the card.'); return; }
  loading();
  post('/api/whisper/open').then(function(r){ handle(r.json); })
    .catch(function(){ setState('⚠️','Connection Error','Could not reach the Whispry server. Check your connection and try again.'); });
})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    global _bot_thread

    for warning in config_warnings():
        logger.warning("%s", warning)

    if READER_APP_SHORT_NAME:
        logger.info("Mini App reader ENABLED (app short name: %s).", READER_APP_SHORT_NAME)
    else:
        logger.error(
            "READER_APP_SHORT_NAME is not set — the whisper reader is DISABLED. "
            "Register via BotFather /newapp (Web App URL = https://<service>/reader) "
            "and set READER_APP_SHORT_NAME."
        )

    _bot_thread = start_bot_thread()
    logger.info("Telegram bot thread started.")

    logger.info("Starting Flask web server on 0.0.0.0:%s", PORT)
    try:
        app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)
    except Exception as exc:
        logger.critical("Flask failed to start on port %s: %s", PORT, exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
