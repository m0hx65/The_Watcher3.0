# The Watcher

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=flat&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?style=flat&logo=telegram&logoColor=white)](https://core.telegram.org/bots)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?style=flat&logo=postgresql&logoColor=white)](https://postgresql.org)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?style=flat&logo=docker&logoColor=white)](Dockerfile)
[![License](https://img.shields.io/badge/License-MIT-22c55e?style=flat)](LICENSE)

**Instagram profile intelligence platform operated entirely through Telegram.**

Track public accounts in real time — followers, bios, profile pictures, verification status, and 10+ more fields. Get instant Telegram notifications the moment anything changes. Deploy to Render in under five minutes.

---

## How It Works

The Watcher runs as a single Docker container. It connects to your Telegram bot, schedules sweeps across a list of Instagram usernames, and fires a message to your chat whenever a profile changes. Everything — adding accounts, viewing history, exporting data — is done through Telegram commands or inline menus.

```
Telegram Chat ──► Bot Commands ──► FastAPI + APScheduler
                                          │
                                   ┌──────▼──────┐
                                   │  Instagram  │  HTTP/2 · TLS fingerprint impersonation
                                   └──────┬──────┘
                                          │ profile data
                                   ┌──────▼──────┐
                                   │  PostgreSQL │  snapshots · diffs · media hashes
                                   └──────┬──────┘
                                          │ change detected
                                   ┌──────▼──────┐
                                   │  Telegram   │  formatted alert + panel bump
                                   └─────────────┘
```

---

## Features

**Monitoring**
- Tracks 10+ profile fields: followers, following, posts, reels, highlights, biography, full name, username, external link, verification badge, business flag, public/private status
- Profile picture change detection — SHA-256 hashes each downloaded image and stores it to disk for later retrieval
- Configurable sweep interval with per-check jitter to avoid synchronized request bursts
- Throttled concurrency — configurable max parallel fetches per sweep

**Telegram Interface**
- Full command set: `/add`, `/remove`, `/list`, `/recheck`, `/status`, `/history`, `/photo`, `/export`, `/help`
- Inline keyboard menus — no commands to memorize
- Panel bumping — after each notification the main menu re-posts at the bottom of the chat so it stays accessible
- Authorization via `TELEGRAM_ADMIN_IDS`; leave empty to allow all users

**Reliability**
- Chrome TLS fingerprint impersonation via `curl_cffi` to bypass 401/403 blocks
- Tenacity retry with exponential backoff on transient failures
- Debounced failure notifications — surfaces 401/403/429 errors without spamming on every retry
- Consecutive failure counter per account surfaced in `/status` and `/list`

**Data & API**
- PostgreSQL persistence: snapshots, media hashes, notification logs, runtime settings
- Configurable retention windows for snapshots, notifications, and raw API responses
- HTTP API with liveness/readiness probes and a cron-compatible `/sweep` endpoint
- Token-gated mutation endpoints
- CSV export of full notification history

---

## Table of Contents

- [Quick Start](#quick-start-local)
- [Docker](#docker)
- [Deploy to Render](#deploy-to-render)
- [Configuration](#configuration)
- [Telegram Commands](#telegram-commands)
- [HTTP API](#http-api)
- [Data Model](#data-model)
- [Project Layout](#project-layout)
- [Tech Stack](#tech-stack)
- [Responsible Use](#responsible-use)
- [License](#license)

---

## Quick Start (Local)

**Prerequisites:** Python 3.12+, a PostgreSQL instance (local or remote)

```bash
# 1. Clone
git clone https://github.com/m0hx65/The_Watcher3.0.git
cd The_Watcher3.0

# 2. Configure
cp .env.example .env
# Edit .env — set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DATABASE_URL

# 3. Install
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 4. Run
uvicorn app.main:app --reload --port 8000
```

The bot starts in long-polling mode (no public URL required). Send `/add <username>` to your bot to start monitoring.

---

## Docker

```bash
docker build -t the-watcher .

docker run -d \
  --name watcher \
  --restart unless-stopped \
  -p 8000:8000 \
  -v watcher-media:/app/data/media \
  --env-file .env \
  the-watcher
```

The container exposes a `/health` endpoint and includes a built-in `HEALTHCHECK`.

---

## Deploy to Render

The `render.yaml` blueprint provisions everything automatically.

1. Fork this repository and push it to GitHub.
2. In Render: **New +** → **Blueprint** → select your fork.
3. Render provisions:
   - A Docker web service
   - A managed PostgreSQL 16 database
   - A 1 GB persistent disk at `/app/data` for stored profile pictures
4. Set the three required secrets in the Render dashboard:

   | Variable | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | Token from [@BotFather](https://t.me/botfather) |
   | `TELEGRAM_CHAT_ID` | Your chat or channel ID |
   | `TELEGRAM_ADMIN_IDS` | Comma-separated Telegram user IDs (optional) |

   `DATABASE_URL` and `WEB_API_TOKEN` are auto-generated by Render.

5. Click **Deploy**. The service registers a Telegram webhook using its public URL and begins sweeping immediately.

### Optional: External Cron Trigger

Render's free tier may suspend the web service between requests. Use a Render Cron Job or any external scheduler to keep sweeps firing reliably:

```bash
curl -fsS -X POST https://<your-service>.onrender.com/sweep \
  -H "X-API-Token: $WEB_API_TOKEN"
```

---

## Configuration

All settings are read from environment variables. Copy `.env.example` to `.env` for local development.

### Required

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from [@BotFather](https://t.me/botfather) |
| `TELEGRAM_CHAT_ID` | ID of the chat or channel that receives alerts |
| `DATABASE_URL` | PostgreSQL connection string — `postgres://`, `postgresql://`, and `postgresql+asyncpg://` are all accepted |

### Telegram

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_ADMIN_IDS` | _(empty)_ | Comma-separated user IDs allowed to use the bot. Empty = allow all |
| `TELEGRAM_WEBHOOK_URL` | _(empty)_ | Public base URL for webhook registration. Automatically inferred from `RENDER_EXTERNAL_URL` on Render |
| `TELEGRAM_WEBHOOK_SECRET` | _(empty)_ | Optional secret validated against Telegram's `X-Telegram-Bot-Api-Secret-Token` header |
| `TELEGRAM_WEBHOOK_PATH` | `/telegram/webhook` | Webhook path registered with Telegram and mounted by FastAPI |

### Scheduler

| Variable | Default | Description |
|---|---|---|
| `CHECK_INTERVAL` | `1800` | Seconds between full sweeps |
| `JITTER_SECONDS` | `120` | Maximum random seconds added to each interval |
| `MAX_CONCURRENT_FETCHES` | `3` | Max parallel profile fetches per sweep |
| `REQUEST_TIMEOUT` | `20` | Per-request timeout in seconds |

### Storage & Retention

| Variable | Default | Description |
|---|---|---|
| `MEDIA_DIR` | `./data/media` | Directory for downloaded profile pictures |
| `SNAPSHOT_RETENTION_DAYS` | `30` | Days to keep account snapshots. `0` = keep forever |
| `NOTIFICATION_RETENTION_DAYS` | `90` | Days to keep notification logs |
| `RAW_RESPONSE_RETENTION_DAYS` | `7` | Days to keep raw Instagram API responses |

### Instagram

| Variable | Default | Description |
|---|---|---|
| `IG_SESSION_COOKIE` | _(empty)_ | Optional Instagram `sessionid` cookie value for authenticated requests |
| `IG_PROXY_URL` | _(empty)_ | Optional proxy URL used specifically for Instagram requests |

### Proxy & Network

| Variable | Default | Description |
|---|---|---|
| `PROXY_URL` | _(empty)_ | Outbound proxy for all requests (`http://...` or `socks5://...`). Overrides `HTTP_PROXY`/`HTTPS_PROXY` |
| `HTTP_PROXY` / `HTTPS_PROXY` | _(empty)_ | Standard proxy env vars (used when `PROXY_URL` is unset) |

### Web API & Runtime

| Variable | Default | Description |
|---|---|---|
| `WEB_API_TOKEN` | _(empty)_ | If set, required as `X-API-Token` header for `/sweep` and `/accounts/*/recheck` |
| `PORT` | `8000` | Web server port |
| `LOG_LEVEL` | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## Telegram Commands

| Command | Description |
|---|---|
| `/add <username>` | Start monitoring an account; runs an immediate baseline fetch |
| `/remove <username>` | Stop monitoring and delete all stored history |
| `/list` | Show all monitored accounts with last-check status and failure count |
| `/recheck <username>` | Force an immediate check outside the normal schedule |
| `/status` | Global stats: account count, last sweep time, next scheduled sweep |
| `/history <username>` | Last 15 detected changes for an account |
| `/photo <username>` | Latest stored profile picture and its SHA-256 hash |
| `/export` | Download full notification history as a CSV file |
| `/help` | Command reference |

---

## HTTP API

All responses are JSON. Mutation endpoints require `X-API-Token` when `WEB_API_TOKEN` is configured.

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | None | Liveness probe — returns `{"status":"ok"}` |
| `GET` | `/ready` | None | Readiness probe — checks monitor and scheduler state |
| `GET` | `/status` | None | Stats: account counts, last sweep time, scheduler info |
| `GET` | `/accounts` | None | List all monitored accounts |
| `POST` | `/accounts/{username}/recheck` | Token | Force an immediate check for one account |
| `POST` | `/sweep` | Token | Trigger a full sweep across all active accounts |

**Example — trigger a sweep:**

```bash
curl -X POST https://<host>/sweep \
  -H "X-API-Token: your-token"
```

---

## Data Model

Tables are created automatically on first boot via SQLAlchemy `create_all`.

| Table | Description |
|---|---|
| `monitored_accounts` | One row per target account. Tracks active flag, last HTTP status, consecutive failure count |
| `account_snapshots` | One row per fetch. Stores all parsed profile fields, raw JSON response, and HTTP status |
| `profile_media_hashes` | One row per unique profile picture (SHA-256 + local disk path). Deduplicates across accounts |
| `notification_logs` | One row per dispatched change event, including change type, payload, and delivery status |
| `app_settings` | Key-value store for runtime-tunable config (check interval, panel message IDs) persisted across restarts |

---

## Project Layout

```
app/
├── api/            HTTP API routes (FastAPI router)
├── bot/            Telegram command handlers, inline menus, notification dispatch
├── database/       SQLAlchemy models, async session, CRUD helpers
├── monitor/        Instagram client, media hasher, change detector, sweep orchestrator
├── utils/          Logging setup, user-agent rotation, formatting helpers
├── workers/        APScheduler-based sweep worker
├── config.py       Pydantic Settings — environment-driven configuration
└── main.py         FastAPI app, lifespan wiring, service initialization
Dockerfile
Procfile
render.yaml
requirements.txt
.env.example
```

---

## Tech Stack

| Component | Library | Version |
|---|---|---|
| Web framework | FastAPI | 0.115 |
| ASGI server | Uvicorn | 0.32 |
| Instagram client | curl_cffi | 0.15 |
| Telegram | python-telegram-bot | 21.9 |
| Task scheduler | APScheduler | 3.10 |
| ORM | SQLAlchemy (async) | 2.0 |
| Database driver | asyncpg | 0.30 |
| Config | pydantic-settings | 2.7 |
| Retry | Tenacity | 9.0 |
| Logging | Loguru | 0.7 |
| Image processing | Pillow | 11.0 |

---

## Responsible Use

- The Instagram endpoint used (`/api/v1/users/web_profile_info/`) is undocumented and rate-limited.
- Only monitor accounts you have a legitimate reason to track: your own accounts, brand assets, or authorized OSINT research.
- Increase `CHECK_INTERVAL` and reduce `MAX_CONCURRENT_FETCHES` for large account lists.
- The bot surfaces 401, 403, and 429 responses to the operator with debouncing so you know immediately if you are being throttled.

---

## License

[MIT](LICENSE)
