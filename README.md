# 🔐 Inline Whisper Bot — Telegram + Render Web Service

A production-ready Telegram **inline whisper bot**. People type
`@YourBotName your secret message` in any chat, tap a result, choose the
recipient, and the whisper is delivered **privately** — visible only to the
sender, the recipient, and the admin log channel (for moderation).

## ✨ Features

- **Telegram Inline Mode** — usable in any chat (groups, DMs, etc.)
- **Private delivery** — the whisper text is *never* posted into the chat
- **Telegram-native recipient selection** — inline card + button + DM flow
- **Audit log channel** — every delivered whisper is logged in full
- **Unique whisper IDs** — e.g. `#W-8F42A1`, never reused
- **/start, /help, /game** commands
- **Telegram Mini App** — `/game` opens your game via a real Web App button
- **Render-ready** — Flask health server + bot polling in one process
- **No database** — in-memory sessions that auto-expire (15 minutes)

## 📁 Project structure

| File | Purpose |
|---|---|
| `bot.py` | All Telegram logic (inline flow, delivery, commands) |
| `config.py` | Environment-based configuration |
| `live.py` | Flask health server + bot thread (Render entry point) |
| `requirements.txt` | Python dependencies |
| `README.md` | This guide |

## 🤔 How the whisper flow works (the honest Telegram way)

The Telegram Bot API has **no way to post a message into a chat that only
some members can see**. This bot does not pretend otherwise. Instead:

1. The sender types an inline query in any chat:
   - `@YourBotName your secret message`, or the shortcut
   - `@YourBotName @recipient_username your secret message`
2. Selecting the inline result posts a small **card** into the chat that
   contains **no whisper text**, plus a button:
   - **🎯 Choose Recipient** — the bot DMs the sender, who replies with the
     recipient's `@username`.
   - **📨 Send Whisper** (shortcut mode) — delivers straight to the
     `@username` that was typed in the query.
3. The bot resolves the recipient through Telegram's `getChat()` API (the
   identity cannot be spoofed) and then:
   - sends the **whisper text privately to the recipient** (DM),
   - sends a **private confirmation copy to the sender** (DM),
   - updates the public card in the chat (still without the text),
   - posts the **complete whisper** to the **log channel**.

So the whisper text is only ever visible to: **sender (DM), recipient (DM),
log channel (authorized admin monitoring)**.

## ⚙️ Configuration (environment variables)

| Variable | Required | Example | Description |
|---|---|---|---|
| `BOT_TOKEN` | ✅ | `123456:ABC-DEF...` | Token from @BotFather |
| `LOG_CHANNEL_ID` | recommended | `-1001234567890` | Channel that receives whisper logs |
| `GAME_URL` | optional | `https://game.example.com` | Game opened by `/game` (must be HTTPS for Mini App buttons) |
| `PORT` | auto | `10000` | Render injects it; defaults to 10000 |

---

## 🛠 Setup guide

### 1. Create the bot with BotFather
1. Open [@BotFather](https://t.me/BotFather) → send `/newbot`.
2. Enter a display name, e.g. `Inline Whisper Bot`.
3. Enter a username ending in `bot`, e.g. `MyWhisperBot`.
4. Copy the token it gives you — this is your **BOT_TOKEN**.
   ⚠️ Never commit it or share it publicly.

### 2. Enable Inline Mode
In BotFather send `/setinline` (or `/mybots` → your bot → **Bot Settings** →
**Inline Mode** → **Turn on**) and select your bot.

Without this step the bot will never receive inline queries.

### 3. Set the Inline Placeholder
Right after enabling inline mode, BotFather asks for a **placeholder** — the
grey hint text users see before typing. Enter for example:

```
Type your whisper message…
```

You can change it any time via `/setinline`.

### 4. Add the bot to the Log Channel
1. Create a private Telegram channel (e.g. *Whisper Logs*).
2. Open the channel → **Admins** → **Add Admin** → add your bot.

### 5. Give the bot permission to post messages
While adding the bot as an administrator, enable at least:
- **Post Messages**

If this permission is missing, whisper logging fails — the error is written
to the service logs and the bot keeps running.

### 6. Get LOG_CHANNEL_ID
Easiest: forward any message from your log channel to
[@userinfobot](https://t.me/userinfobot) — it replies with an ID like
`-1001234567890`.

Manual alternative:
1. Add the bot to the channel and post any message in it.
2. Open `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates` in a browser.
3. Find `"chat":{"id":-100…}` — that is your `LOG_CHANNEL_ID`.

Channel IDs are negative and usually start with `-100`.

### 7. Set GAME_URL
Point `GAME_URL` at your deployed game. Requirements:
- **HTTPS** (required by Telegram for Web App / Mini App buttons)
- Publicly reachable, valid certificate
- Any web game works (GitHub Pages, Netlify, Render Static Site, …)

### BotFather Mini App setup (Web Apps)
- The `/game` command uses a **Web App button**
  (`InlineKeyboardButton(text="🎮 Open Game", web_app=WebAppInfo(url=GAME_URL))`).
  Telegram opens `GAME_URL` **directly inside Telegram's Mini App interface** —
  there is no intermediate webpage.
- Telegram's requirements for Web App URLs: **HTTPS**, valid certificate,
  publicly reachable. No extra BotFather registration is strictly required
  for the button itself.
- Optional BotFather extras:
  - `/newapp` → registers the Mini App with BotFather (icon, description) and
    gives you a direct `t.me/bot/app` launch link.
  - `/setmenubutton` → puts the game on the bot's blue **menu button**, so it
    can be opened even without `/game`.
- Note: Telegram currently supports Web App inline buttons only in **private
  chats between the user and the bot**. If `/game` is used somewhere Telegram
  rejects the button, the bot automatically falls back to a normal URL button.

### 8. Install Python dependencies
Requires Python **3.9+**.

```bash
pip install -r requirements.txt
```

### 9. Run locally

Linux / macOS:
```bash
export BOT_TOKEN="123456:ABC-your-token"
export LOG_CHANNEL_ID="-1001234567890"
export GAME_URL="https://your-game-url.example"
python live.py
```

Windows (PowerShell):
```powershell
 $env:BOT_TOKEN="123456:ABC-your-token"
 $env:LOG_CHANNEL_ID="-1001234567890"
 $env:GAME_URL="https://your-game-url.example"
python live.py
```

Then verify:
- `http://localhost:10000/` → `Whisper Bot is running`
- `http://localhost:10000/health` → JSON status

Stop with `Ctrl+C`.

### 10. Deploy to Render
1. Push this project to a GitHub/GitLab repository.
2. Render Dashboard → **New +** → **Web Service** → connect the repo.
3. **Runtime:** Python 3
4. **Build Command:** `pip install -r requirements.txt`
5. **Start Command:** `python live.py`
6. (Recommended) **Health Check Path:** `/health`
7. Add the environment variables (next step) and click **Create Web Service**.

The bot runs continuously; Render restarts the service on deploys.

### 11. Render environment variables

Add these in Render → your service → **Environment**:

```
BOT_TOKEN=YOUR_BOT_TOKEN
LOG_CHANNEL_ID=-100XXXXXXXXXX
GAME_URL=https://your-game-url.example
```

Optional: pin the Python version, e.g. `PYTHON_VERSION=3.12.6`.

### 12. Render Start Command

```
python live.py
```

No Gunicorn, no Docker required. `live.py` runs the Flask health server in
the main thread and the Telegram bot (long polling) in a background thread —
neither blocks the other.

---

## 🩺 Health endpoints

| Endpoint | Response |
|---|---|
| `GET /` | `Whisper Bot is running` |
| `GET /health` | JSON: service + bot thread status + UTC time |

## 🔐 Security

- The **sender identity always comes from Telegram updates** — never from
  user-supplied text, so it cannot be spoofed.
- The **recipient is resolved via Telegram's `getChat()` API** — the bot never
  trusts user-claimed IDs.
- **Only the original sender** can use the whisper buttons (verified on every
  callback press).
- **No database.** Whisper sessions live in memory for 15 minutes and are then
  deleted; they are lost on restart (senders simply re-send).
- **`BOT_TOKEN` is only read from environment variables** and never hardcoded.
- The **log channel receives the complete whisper text** for authorized
  admin/moderation monitoring.

## 🧯 Error handling

- Empty inline query / too-long whisper → friendly hint results.
- Missing recipient → nothing is sent; the sender is guided through the flow.
- Recipient never started the bot or blocked it → clear explanation to the sender.
- Log channel unavailable / no post permission → error in console logs, the
  bot keeps running.
- Invalid `GAME_URL` → `/game` explains the problem; if Telegram rejects the
  Web App button, the bot falls back to a normal URL button.
- Missing environment variables → clear CRITICAL/WARNING lines at startup.
- Flask startup errors (e.g. port busy) → logged, process exits non-zero so
  Render restarts it.

## ❓ Troubleshooting

| Symptom | Fix |
|---|---|
| Inline results never appear | Inline Mode is not enabled (step 2) |
| "can't receive whispers yet" | The recipient must open the bot once and press **Start** |
| Nothing appears in the log channel | The bot must be a channel **admin with Post Messages**; double-check `LOG_CHANNEL_ID` |
| `/game` button missing or error | `GAME_URL` must be HTTPS and publicly reachable |
| Bot offline on Render | Check the logs — usually `BOT_TOKEN` missing/invalid (CRITICAL lines) |
