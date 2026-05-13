# The Watcher V3.0

A production-ready Instagram intelligence monitoring platform operated entirely through Telegram. Tracks public profile state on a configurable interval, hashes profile pictures, detects field-level changes, and notifies a Telegram chat.

## Features

- **Modular architecture** — FastAPI app, APScheduler worker, SQLAlchemy async ORM, python-telegram-bot.
- **Locked Instagram profile lookup** — profile data is fetched with `GET /api/v1/users/web_profile_info/?username=<username>` on `www.instagram.com` over HTTP/2 with `X-Ig-App-Id: 936619743392459`.
- **Change detection** — followers/following/posts/reels/highlights, bio, full name, username, verification, business flag, public/private toggle, external link, profile picture (SHA-256 of binary).
- **Profile picture hashing** — downloads the media URL returned by `web_profile_info`, compares image bytes, stores image on disk, and reuses it for `/photo`.
- **Telegram bot control** — `/add`, `/remove`, `/list`, `/recheck`, `/status`, `/history`, `/photo`, `/export`, `/help`. Authorization via `TELEGRAM_ADMIN_IDS`.
- **Resilient sweeps** — randomized jitter, throttled concurrency, debounced failure notifications.
- **HTTP API** — `/health`, `/ready`, `/status`, `/accounts`, `/sweep` (cron-trigger compatible), token-gated mutating endpoints.
- **PostgreSQL storage** — `monitored_accounts`, `account_snapshots`, `profile_media_hashes`, `notification_logs`.
- **Render-ready** — `render.yaml` provisions web service + Postgres + persistent disk for media; Docker image included.

## Project layout

```
app/
├── api/            HTTP API routes
├── bot/            Telegram handlers and notification dispatch
├── database/       SQLAlchemy models, session, CRUD
├── monitor/        Instagram client, media hasher, change detector, orchestrator
├── utils/          Logging, user agents, formatting helpers
├── workers/        APScheduler-based sweep worker
├── config.py       Environment-driven settings
└── main.py         FastAPI app & lifespan wiring
Dockerfile
Procfile
render.yaml
requirements.txt
.env.example
```

## Quick start (local)

```bash
cp .env.example .env
# edit .env: at minimum set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DATABASE_URL
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Then in Telegram, message your bot: `/add target_username`.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | _required_ | BotFather token |
| `TELEGRAM_CHAT_ID` | _required_ | Chat/channel that receives alerts |
| `TELEGRAM_ADMIN_IDS` | _empty_ | Comma-separated user IDs authorized to use the bot. Empty = allow all |
| `TELEGRAM_WEBHOOK_URL` | _empty_ | Public base URL for Telegram webhooks. If empty, `RENDER_EXTERNAL_URL` is used when available |
| `TELEGRAM_WEBHOOK_SECRET` | _empty_ | Optional secret checked against Telegram's webhook secret header |
| `TELEGRAM_WEBHOOK_PATH` | `/telegram/webhook` | Webhook path registered with Telegram and exposed by FastAPI |
| `DATABASE_URL` | _required_ | Postgres URL; `postgres://`, `postgresql://`, and `postgresql+asyncpg://` are all accepted |
| `CHECK_INTERVAL` | `1800` | Sweep interval, seconds |
| `JITTER_SECONDS` | `120` | Random jitter applied to each interval |
| `REQUEST_TIMEOUT` | `20` | Per-request timeout, seconds |
| `MAX_CONCURRENT_FETCHES` | `3` | Max parallel profile fetches per sweep |
| `MEDIA_DIR` | `./data/media` | Where downloaded profile pictures are stored |
| `LOG_LEVEL` | `INFO` | loguru level |
| `HTTP_PROXY` / `HTTPS_PROXY` | _empty_ | Optional outbound proxy |
| `WEB_API_TOKEN` | _empty_ | If set, required as `X-API-Token` for `/sweep` and `/accounts/*/recheck` |
| `RENDER_EXTERNAL_URL` | _empty_ | Render-provided public URL used as webhook base when `TELEGRAM_WEBHOOK_URL` is unset |

## Telegram commands

| Command | Effect |
|---|---|
| `/add <username>` | Start monitoring; runs an immediate baseline check |
| `/remove <username>` | Stop monitoring and delete history |
| `/list` | Show monitored accounts with last-check status |
| `/recheck <username>` | Force an immediate check |
| `/status` | Global stats + scheduler next-run time |
| `/history <username>` | Last 15 detected changes |
| `/photo <username>` | Latest stored profile picture + hash |
| `/export` | CSV dump of notification history |
| `/help` | Command list |

## Deploying to Render

1. Push this repository to GitHub.
2. In Render: **New +** → **Blueprint** → point at the repo. Render reads `render.yaml` and provisions:
   - A web service running the Dockerfile.
   - A managed Postgres 16 instance.
   - A 1 GB persistent disk mounted at `/app/data` for stored profile pictures.
3. Set the required environment variables in the Render dashboard (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ADMIN_IDS`). `DATABASE_URL` and `WEB_API_TOKEN` are populated automatically.
4. Deploy. On Render, the service registers a Telegram webhook using the public service URL and runs sweeps on the schedule. Without a public webhook URL, it falls back to long-polling.

### Optional: Render Cron Job

Instead of (or in addition to) the in-process scheduler, you can create a Render Cron Job that calls:

```
curl -fsS -X POST https://<your-service>.onrender.com/sweep \
  -H "X-API-Token: $WEB_API_TOKEN"
```

This is useful if you want sweeps to fire even when the web service is sleeping on the Free plan.

## Data model

- `monitored_accounts` — one row per target. Tracks active flag, last status, consecutive failures.
- `account_snapshots` — one row per fetch (success or failure). Holds parsed fields, raw JSON, HTTP status.
- `profile_media_hashes` — one row per unique SHA-256 of a downloaded profile picture, with a path on disk.
- `notification_logs` — one row per dispatched change event, including delivery status.

Tables are created automatically on first boot via `Base.metadata.create_all`.

## Notes on responsible use

- The endpoint is undocumented and rate-limited. Respect Instagram's terms of service and only monitor accounts you have legitimate reason to track (your own accounts, brand mentions, OSINT research with proper authorization, etc.).
- Increase `CHECK_INTERVAL` for large account lists to stay well clear of rate limits.
- Failures of 401/403/429 are surfaced to the operator with debouncing, so you'll know quickly if you're getting throttled.

## License

MIT
