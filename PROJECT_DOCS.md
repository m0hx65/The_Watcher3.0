# The Watcher V3.0 — Full Project Documentation

> Instagram profile intelligence monitoring bot. Tracks 10+ fields on public accounts
> and delivers instant Telegram alerts on any change. Self-hosted, Docker-ready,
> fully free to run 24/7.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [Tech Stack](#3-tech-stack)
4. [Directory Layout](#4-directory-layout)
5. [Data Model](#5-data-model)
6. [Core Components](#6-core-components)
7. [Telegram Bot Interface](#7-telegram-bot-interface)
8. [HTTP API](#8-http-api)
9. [Configuration Reference](#9-configuration-reference)
10. [Deployment](#10-deployment)
11. [Bugs Found & Fixed](#11-bugs-found--fixed)
12. [Known Limitations](#12-known-limitations)
13. [Roadmap](#13-roadmap)

---

## 1. Project Overview

**The Watcher V3.0** is a private Telegram bot that silently monitors public (and
optionally private) Instagram accounts and notifies you the moment anything changes.
Every scheduled sweep fetches the current profile state, diffs it against the last
known snapshot, and sends a rich Telegram notification listing exactly what changed.

### What it monitors per account

| Field | Notes |
|---|---|
| Followers count | Numeric diff shown |
| Following count | Numeric diff shown |
| Posts count | |
| Reels count | |
| Highlight/story count | |
| Biography | Full before/after text |
| Full name | |
| External link | |
| Profile picture | Downloaded locally, SHA-256 hash compared |
| `is_private` flag | Public ↔ Private transitions |
| `is_verified` badge | |
| `is_business` flag | |

Profile pictures are sent as Telegram **documents** (not photos) to preserve quality.

### Cost

| Service | Tier | Cost |
|---|---|---|
| Render | Free web service | $0 |
| PostgreSQL | Render free tier | $0 |
| Cloudflare Workers | Free (100k req/day) | $0 |
| Telegram Bot API | Free | $0 |
| **Total** | | **$0/month** |

---

## 2. Architecture

```
User (Telegram)
     │
     ▼
[Telegram Bot API]
     │  webhook (POST /telegram/webhook)
     ▼
[FastAPI — app/main.py]
     │
     ├── [WatcherScheduler]  ←  APScheduler interval job
     │        │
     │        ▼
     │   [MonitorService]
     │        │
     │        ├── [InstagramClient]  →  Instagram CDN / Cloudflare Worker proxy
     │        ├── [MediaHasher]      →  Downloads & SHA-256-hashes profile pictures
     │        ├── [StoriesClient]    →  Stories & highlights via storiesig.info API
     │        └── [NotificationDispatcher] → Telegram send_text / send_photo / send_video
     │
     ├── [PostgreSQL]  ←  async SQLAlchemy + asyncpg
     │        └── 6 tables (see §5)
     │
     └── [FastAPI HTTP API]  →  /health, /status, /accounts, /sweep, …
```

### Data flow for a single sweep

```
1.  APScheduler fires _sweep_wrapper()
2.  MonitorService.check_all() fans out to all active accounts (semaphore-limited)
3.  Per account:
    a.  InstagramClient.fetch_profile()  →  200 JSON from Instagram
    b.  InstagramClient.fetch_hd_pic_url()  →  mobile API for full-res picture
    c.  MediaHasher.hash_url()  →  download + SHA-256
    d.  detect_changes(previous_snapshot, new_snapshot)
    e.  If changed: INSERT AccountSnapshot, log NotificationLog
    f.  NotificationDispatcher sends text diff + picture document
4.  StoriesClient checks stories/highlights (if API key configured)
5.  Sweep-complete summary notification sent
6.  Panel-bump debounce: main-menu message moved to bottom of chat
```

---

## 3. Tech Stack

| Library | Version | Role |
|---|---|---|
| FastAPI | latest | Web framework + webhook endpoint |
| python-telegram-bot | latest | Telegram Bot SDK |
| SQLAlchemy (async) | latest | ORM |
| asyncpg | latest | Async PostgreSQL driver |
| APScheduler | latest | Periodic sweep scheduler |
| curl_cffi | latest | HTTP client with Chrome TLS impersonation |
| Pydantic v2 / pydantic-settings | latest | Config, validation |
| loguru | latest | Structured logging |
| Python | 3.11+ | Runtime |

---

## 4. Directory Layout

```
the_watcher_V3.0/
├── app/
│   ├── main.py              # FastAPI app, lifespan, panel-bump wiring
│   ├── config.py            # Pydantic Settings — all env vars
│   ├── api/
│   │   └── routes.py        # HTTP API endpoints
│   ├── bot/
│   │   ├── handlers.py      # All Telegram command & callback handlers
│   │   ├── keyboards.py     # Inline keyboard builders
│   │   └── notifications.py # NotificationDispatcher (send_text/photo/video/document)
│   ├── database/
│   │   ├── models.py        # SQLAlchemy ORM models (6 tables)
│   │   ├── crud.py          # All DB read/write helpers
│   │   └── session.py       # Async engine + session factory
│   ├── monitor/
│   │   ├── instagram.py     # InstagramClient — TLS-impersonated fetch + retry
│   │   ├── media_hasher.py  # Download & hash profile pictures
│   │   ├── service.py       # MonitorService — orchestrate fetch/diff/persist/notify
│   │   ├── change_detector.py # ChangeSet diffing logic
│   │   └── stories.py       # StoriesClient — stories & highlights
│   ├── workers/
│   │   └── scheduler.py     # WatcherScheduler (APScheduler wrapper)
│   └── utils/
│       ├── formatting.py    # fmt_timestamp (Damascus UTC+3), fmt_number, esc, truncate
│       ├── logger.py        # Loguru logger
│       └── user_agents.py   # UA pool for the Cloudflare Worker
├── scripts/
│   ├── test_stories.py      # End-to-end stories smoke test
│   ├── test_ig_fetch.py     # Instagram fetch smoke test
│   └── verify_client.py     # Quick client verification
├── docs/                    # All development logs (see §11)
├── Dockerfile
├── Procfile                 # Render start command
└── .env.example
```

---

## 5. Data Model

### `monitored_accounts`
Stores each account being watched.

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `username` | varchar(64) unique | Lowercase, no `@` |
| `instagram_id` | varchar(64) | Populated on first successful fetch |
| `active` | bool | Paused accounts still stored |
| `added_by` | bigint | Telegram user ID |
| `last_checked_at` | timestamptz | Updated every sweep |
| `last_status_code` | int | Last HTTP status |
| `consecutive_failures` | int | Reset to 0 on success |

### `account_snapshots`
One row per detected change (not per sweep — see §11 Fix #4).

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `account_id` | int FK | Cascade delete |
| `username` | varchar | Snapshot copy (handles renames) |
| `full_name`, `biography`, `external_url` | text | Profile fields |
| `followers_count`, `following_count`, `posts_count`, `reels_count`, `story_count` | int | |
| `is_private`, `is_verified`, `is_business` | bool | |
| `profile_pic_url` | text | Raw CDN URL |
| `profile_pic_hash` | varchar(64) | SHA-256 of downloaded image |
| `http_status` | int | 200 = success |
| `raw_response` | JSONB | Nulled after `RAW_RESPONSE_RETENTION_DAYS` |
| `error` | text | Set on failures |
| `created_at` | timestamptz indexed | |

### `profile_media_hashes`
Dedup table for profile pictures — never purged automatically.

| Column | Notes |
|---|---|
| `sha256` | SHA-256 of the downloaded image bytes |
| `source_url` | Instagram CDN URL the image was fetched from |
| `local_path` | Path on disk |
| `byte_size`, `content_type` | |

### `app_settings`
Key-value store for runtime config (check interval, panel message ID, etc.).

| Key | Value stored |
|---|---|
| `check_interval_seconds` | Current sweep interval in seconds |
| `last_sweep_at` | ISO timestamp of last sweep start |
| `panel_msg_id` | Telegram message ID of the main-menu panel |
| `panel_chat_id` | Chat ID for the panel |

### `notification_logs`
Audit log of every notification sent (or attempted).

| Column | Notes |
|---|---|
| `change_type` | `"followers_count"`, `"profile_picture"`, `"fetch_failure"`, etc. |
| `payload` | JSONB `{old, new}` or failure details |
| `delivered` | Bool — whether Telegram confirmed receipt |

### `seen_stories`
Dedup table for delivered story/highlight items.

| Column | Notes |
|---|---|
| `story_pk` | Instagram's internal story ID — dedup key |
| `source` | `"story"` or `"highlight"` |
| `highlight_id`, `highlight_title` | Set for highlights only |
| `media_type` | `"image"` or `"video"` |
| `taken_at` | Unix timestamp when the story was created |
| Unique index on `(account_id, story_pk)` | |

---

## 6. Core Components

### 6.1 InstagramClient (`app/monitor/instagram.py`)

Uses `curl_cffi` to replay Chrome's exact TLS ClientHello (JA3/JA4 fingerprint).
Instagram checks the TLS handshake before reading any HTTP headers — Python's standard
OpenSSL stack is fingerprint-blocked; Chrome impersonation is not.

**Endpoint used:**
```
GET /api/v1/users/web_profile_info/?username=<u> HTTP/2
Host: www.instagram.com
x-ig-app-id: 936619743392459
```

**Retry logic:**
- 401/403: immediate retry up to 5 times with `random.uniform(1.0, 3.0)` jitter
- 429: exponential backoff, capped at 60s
- 5xx: exponential backoff, capped at 30s
- 404: return immediately, no retry

**Proxy path:** When `IG_PROXY_URL` is set, requests are routed through the
Cloudflare Worker instead of hitting Instagram directly.

**HD profile picture:** After the web API fetch, the client calls
`i.instagram.com/api/v1/users/{id}/info/` with an Android UA to retrieve
`hd_profile_pic_url_info` (full-size image, up to ~1440px). Falls back silently
if unavailable.

### 6.2 MonitorService (`app/monitor/service.py`)

Orchestrates the full check pipeline:

1. Fetches profile + HD picture
2. Diffs against the latest snapshot
3. Inserts snapshot **only if something changed** (or first-ever check)
4. Sends notifications
5. Runs story/highlight checks (if `StoriesClient` is wired in)
6. Sends sweep-complete summary

Concurrency is limited by `asyncio.Semaphore(MAX_CONCURRENT_FETCHES)` to avoid
hammering Instagram.

### 6.3 WatcherScheduler (`app/workers/scheduler.py`)

APScheduler wrapper with two jobs:

| Job | Trigger | Role |
|---|---|---|
| `watcher-sweep` | `IntervalTrigger` (configurable, default 30m) | Runs `check_all()` |
| `watcher-cleanup` | `CronTrigger` — 03:00 UTC daily | Purges old DB rows |

The sweep job persists `last_sweep_at` immediately at start (not end) to prevent
duplicate sweeps from rapid server restarts. On startup, it reads this timestamp
to determine whether to fire immediately or wait for the originally-scheduled time.

The `sweep_in_flight` flag prevents concurrent sweeps from button-mashing or
overlapping APScheduler fires.

### 6.4 NotificationDispatcher (`app/bot/notifications.py`)

Three send methods, all with retry logic:

- `send_text(msg)` — plain Telegram text (HTML parse mode)
- `send_photo(path, caption)` — compressed photo
- `send_document(path, caption)` — uncompressed file (used for profile pictures)
- `send_video(path, caption)` — video with streaming support

Each method calls `post_send_hook` on success, which triggers the panel-bump debounce.

### 6.5 Panel Bump (`app/main.py`)

After every batch of notifications, the main-menu panel is moved to the bottom
of the chat so it's always accessible:

1. `post_send_hook` fires after each successful send
2. A 2-second debounced `asyncio.Task` waits for concurrent notifications to land
3. Old panel is deleted, fresh panel is sent
4. New panel message ID is persisted to `app_settings` (survives server restarts)

### 6.6 StoriesClient (`app/monitor/stories.py`)

Fetches stories and highlights from `storiesig.info` API (when available).
Uses the same Chrome TLS impersonation as the rest of the project.

- `fetch_stories(username)` → list of `StoryItem` (images + videos)
- `fetch_highlights(username)` → all highlight reels and their items
- `download(item, username)` → saves to `{MEDIA_DIR}/{username}/stories/{pk}.jpg|mp4`
- Dedup by `story_pk` against `seen_stories` table

**Current status:** The storiesig.info free API endpoint is dead. The code
degrades gracefully — `fetch_stories()` returns `[]` when the API is unreachable.
Activate by setting `STORIESIG_API_KEY` once an API key is obtained.

### 6.7 Cloudflare Worker Proxy

When Render's Frankfurt datacenter IPs are blocked by Instagram, a transparent
Cloudflare Worker proxy is used:

- URL: `https://ig-proxy.m-asaad2005-ma.workers.dev`
- Accepts `?username=<x>`, forwards to Instagram's web_profile_info endpoint
- Rotates across 6 user agents, retries 8 times
- Cloudflare edge IPs are never blocked by Instagram
- Free tier: 100,000 requests/day

Set `IG_PROXY_URL` in environment variables to enable.

---

## 7. Telegram Bot Interface

### Commands

| Command | Description |
|---|---|
| `/start` or `/menu` | Open the main panel |
| `/add @username` | Start monitoring an account (runs first check immediately) |
| `/remove @username` | Stop monitoring, deletes all snapshots |
| `/list` | Paginated list of monitored accounts |
| `/recheck @username` | Force an immediate check |
| `/status` | Scheduler state, interval, next run, DB stats |
| `/interval [value]` | Show or change the sweep interval (e.g. `30m`, `2h`, `1800s`) |
| `/history @username` | Last 15 change events for an account |
| `/photo @username` | Send the stored profile picture |
| `/fetchphoto @username` | Download and send current profile picture without adding to monitoring |
| `/export` | Download a CSV of all change history |
| `/help` | Show help text |

### Inline Menu Navigation

The main panel has a 2×3 button grid: **Accounts · Status · Interval · Add · Export · Help**.

From an account card: **Recheck · History · Photo · Remove · Home**.

The panel always stays as the last message in the chat — automated sweep
notifications push it back down, and the panel-bump logic re-sends it after
every batch of notifications.

### Authorization

When `TELEGRAM_ADMIN_IDS` is set, only those Telegram user IDs can interact
with the bot. When unset, any user can interact (suitable for personal use).

---

## 8. HTTP API

All endpoints are under the FastAPI app. Optional bearer token auth via
`WEB_API_TOKEN`.

| Method | Path | Description |
|---|---|---|
| GET | `/` | Health + endpoint list |
| GET | `/health` | Liveness probe |
| GET | `/ready` | Readiness probe (checks DB) |
| GET | `/status` | Scheduler state, account counts |
| GET | `/accounts` | List all monitored accounts |
| POST | `/accounts/{username}/recheck` | Trigger an immediate check |
| POST | `/sweep` | Trigger a full sweep (all accounts) |
| POST | `/telegram/webhook` | Telegram webhook receiver |

---

## 9. Configuration Reference

All settings are read from environment variables (or a `.env` file locally).

### Required

| Env Var | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Chat/user ID to send notifications to |
| `DATABASE_URL` | PostgreSQL connection string (any `postgres://` or `postgresql://` prefix is normalized automatically) |

### Telegram Webhook

| Env Var | Default | Description |
|---|---|---|
| `TELEGRAM_WEBHOOK_URL` | — | Public base URL for webhook (auto-set by Render via `RENDER_EXTERNAL_URL`) |
| `TELEGRAM_WEBHOOK_SECRET` | — | Webhook secret token — disallowed characters are stripped automatically |
| `TELEGRAM_WEBHOOK_PATH` | `/telegram/webhook` | Webhook path |
| `TELEGRAM_ADMIN_IDS` | — | Comma-separated Telegram user IDs allowed to use the bot |

### Instagram

| Env Var | Default | Description |
|---|---|---|
| `IG_SESSION_COOKIE` | — | Full cookie string from a logged-in browser session (enables HD profile pictures) |
| `IG_PROXY_URL` | — | Cloudflare Worker proxy URL for datacenter IP bypass |

### Scheduler

| Env Var | Default | Description |
|---|---|---|
| `CHECK_INTERVAL` | `1800` | Sweep interval in seconds |
| `JITTER_SECONDS` | `120` | Random jitter added to each sweep interval |
| `MAX_CONCURRENT_FETCHES` | `3` | Max parallel Instagram fetches per sweep |
| `REQUEST_TIMEOUT` | `20` | HTTP request timeout in seconds |

### Data Retention

| Env Var | Default | Description |
|---|---|---|
| `SNAPSHOT_RETENTION_DAYS` | `30` | Delete old snapshot rows (0 = keep forever) |
| `NOTIFICATION_RETENTION_DAYS` | `90` | Delete old notification log rows |
| `RAW_RESPONSE_RETENTION_DAYS` | `7` | NULL out `raw_response` JSONB on old rows |

### Storage & Misc

| Env Var | Default | Description |
|---|---|---|
| `MEDIA_DIR` | `./data/media` | Local directory for downloaded profile pictures |
| `WEB_API_TOKEN` | — | Bearer token for HTTP API auth |
| `LOG_LEVEL` | `INFO` | Logging level |
| `PORT` | `8000` | Web server port (injected by Render automatically) |
| `PROXY_URL` | — | Optional HTTP/HTTPS proxy for all outbound requests |

---

## 10. Deployment

### Local Development

```bash
# 1. Copy and fill in env vars
cp .env.example .env

# 2. Start a local Postgres (or point DATABASE_URL at a remote one)
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=pw postgres:15

# 3. Run
pip install -r requirements.txt
uvicorn app.main:app --reload
```

In local mode, `TELEGRAM_WEBHOOK_URL` is not set, so the bot automatically falls
back to long-polling.

### Docker

```bash
docker build -t watcher .
docker run -p 8000:8000 --env-file .env -v $(pwd)/data:/app/data watcher
```

### Render (Production)

1. Create a **Web Service** pointing at this repo.
2. Set build command: `pip install -r requirements.txt`
3. Set start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Add a **PostgreSQL** instance and link it (Render injects `DATABASE_URL` automatically).
5. Add environment variables:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `TELEGRAM_WEBHOOK_SECRET` (use Render's `generateValue: true` — invalid characters are stripped automatically)
   - Optional: `IG_PROXY_URL`, `IG_SESSION_COOKIE`

Render injects `RENDER_EXTERNAL_URL` automatically, so webhook registration
happens on startup with no extra config.

---

## 11. Bugs Found & Fixed

This section documents all significant issues encountered during development
and the exact changes made to resolve them.

---

### Fix 1 — TLS Fingerprinting (JA3/JA4)

**Symptom:** Requests worked in Burp Suite (proxied through the browser) but
returned HTTP 401 from `curl`, `httpx`, and all standard Python HTTP libraries.

**Root cause:** Instagram inspects the TLS handshake fingerprint (JA3/JA4)
before reading any HTTP headers. Python's OpenSSL stack has a well-known, blocked
fingerprint. Burp proxied through the browser's TLS stack, which looks legitimate.

**Fix:** Switched to `curl_cffi` with `impersonate="chrome120"`. This library
replays Chrome's exact TLS `ClientHello` at the socket level.

Results of testing all available Chrome targets:

| Target | Result |
|---|---|
| `chrome120` | ✅ 200 — most stable |
| `chrome124` | ✅ 200 |
| `chrome131` | ✅ 200 |
| `chrome133a` | ❌ 401 — fingerprint blocked |
| `chrome136` | ✅ 200 |
| `chrome142` | ❌ 401 — fingerprint blocked |
| `chrome145` | ✅ 200 |
| `chrome146` | ✅ 200 (intermittent) |

`chrome120` is the current impersonation target.

**File:** `app/monitor/instagram.py` — `CHROME_IMPERSONATE = "chrome120"`

---

### Fix 2 — 401 Retry Burst

**Symptom:** On transient 401s, all 5 retries fired at the same timestamp
(effectively a burst), which Instagram blocked as bot-like behavior.

**Fix:** Added `random.uniform(1.0, 3.0)` jitter between 401/403 retries to
spread them across time.

**File:** `app/monitor/instagram.py` — retry loop, `asyncio.sleep(random.uniform(1.0, 3.0))`

---

### Fix 3 — Render Datacenter IP Block

**Symptom:** All requests returned 401 when deployed to Render (Frankfurt
datacenter), but the same code worked on the developer's local machine.

**Root cause:** Render's Frankfurt datacenter IPs are flagged wholesale by
Instagram as bot/datacenter traffic. The problem was geographic, not a code issue.

**Fix:** Built a Cloudflare Worker as a transparent proxy. Cloudflare edge IPs
are never blocked by Instagram, and the free tier allows 100,000 requests/day.

Worker behavior:
- Accepts `?username=<x>`
- Forwards to `https://www.instagram.com/api/v1/users/web_profile_info/?username=<x>`
- Rotates 6 user agents on each retry attempt
- Retries 8 times

**Config:** Set `IG_PROXY_URL=https://ig-proxy.m-asaad2005-ma.workers.dev` in Render
environment variables.

**File:** `app/monitor/instagram.py` — `if settings.ig_proxy_url:` branch in `fetch_profile()`

---

### Fix 4 — Database Bloat from Unconditional Snapshot Inserts

**Symptom:** A new `account_snapshots` row was inserted on every single check
regardless of whether anything changed. At 6 accounts × 8h interval = 21 rows/day
of pure duplicate data. The free PostgreSQL tier (1 GB) would fill up in weeks.

**Fix (part a) — Conditional inserts:** Changed the logic to diff first, then
insert only when something actually changed. First-ever check always inserts
(to establish a baseline).

```
before: insert → diff
after:  diff → insert only if changed
```

Failure snapshots follow the same rule: only stored when transitioning from
success (new failure), not on every consecutive failure.

**Fix (part b) — Daily cleanup job:** Added a `watcher-cleanup` APScheduler job
that fires every day at 03:00 UTC and:
1. NULLs the `raw_response` JSONB column on rows older than `RAW_RESPONSE_RETENTION_DAYS`
2. Deletes snapshot rows older than `SNAPSHOT_RETENTION_DAYS`, always keeping the most recent per account
3. Deletes notification log rows older than `NOTIFICATION_RETENTION_DAYS`

Storage impact (6 accounts, 8h interval, nothing changing):

| Metric | Before | After |
|---|---|---|
| New rows/day (no changes) | ~21 | **0** |
| DB growth over 1 year | ~7,600 rows | **0** |
| Old raw_response JSONB | kept forever | nulled after 7 days |

**Files:** `app/monitor/service.py`, `app/workers/scheduler.py`, `app/database/crud.py`, `app/config.py`

---

### Fix 5 — Telegram Webhook Secret Crash on Startup

**Symptom:** Server crashed on every Render deploy with:
```
Secret token contains unallowed characters
```

**Root cause:** `render.yaml` uses `generateValue: true` for
`TELEGRAM_WEBHOOK_SECRET`. Render generates a base64-like string that can contain
`+`, `/`, and `=`. Telegram's `setWebhook` API only accepts `[A-Za-z0-9_-]{1,256}`.

**Fix:** Added a Pydantic `field_validator` on `telegram_webhook_secret` in
`app/config.py` that strips all disallowed characters at config-load time and
caps the result at 256 chars. If nothing valid remains after stripping, the field
is set to `None` (webhook registered without a secret).

```python
@field_validator("telegram_webhook_secret")
@classmethod
def sanitize_webhook_secret(cls, v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    cleaned = "".join(c for c in v if c.isalnum() or c in "_-")[:256]
    return cleaned or None
```

Both the webhook registration (`main.py`) and the inbound verification
(`routes.py`) read from the same sanitized field, so they always stay in sync.

**File:** `app/config.py` — `sanitize_webhook_secret` validator

---

### Fix 6 — Profile Pictures Pixelated in Telegram

**Symptom:** Profile pictures arrived compressed and pixelated at ~320px even
after switching to `profile_pic_url_hd`.

**Root cause investigation:**
- Telegram compresses images sent as photos — sending as `send_document` preserves
  quality at the cost of a preview, but `profile_pic_url_hd` still only returned 320px.
- Tried InstaRaider's CDN URL-stripping trick (removing `/s320x320/` from the URL)
  — partially worked but inconsistently.
- Tried the Instagram mobile API (`i.instagram.com`) — returned 200 but without
  `hd_profile_pic_url_info` unless authenticated.
- Decoded the `efg` base64 field in the CDN URL:
  ```json
  {"venc_tag":"profile_pic.django.1080.c2"}
  ```
  Instagram stores the original at 1080px but gates it behind a session cookie.
  The `stp` HMAC signature on the CDN URL prevents manually requesting a larger
  size — modifying the URL returns 403.

**Fix:** Two-step picture resolution:
1. Web API fetch gives `profile_pic_url_hd` (~320px)
2. `fetch_hd_pic_url()` calls the mobile API (`i.instagram.com`) with an Android
   UA to retrieve `hd_profile_pic_url_info.url` (up to ~1440px) — only works when
   `IG_SESSION_COOKIE` is set
3. Profile pictures are sent as **documents**, not photos, to bypass Telegram's
   compression

Without a session cookie, the best achievable is the web API's `profile_pic_url_hd`.
This is a platform-level limit (Instagram's HMAC-signed CDN URLs), not solvable
without authentication.

**Files:** `app/monitor/service.py` (`_handle_success`), `app/monitor/instagram.py` (`fetch_hd_pic_url`)

---

### Fix 7 — Main Menu Gets Buried Under Notifications

**Symptom:** As sweep notifications arrived, the main-menu panel (inline keyboard)
got buried in chat history. Users had to scroll up to find it.

**Fix:** Panel-bump system with debounce and DB persistence:

1. `NotificationDispatcher` gets a `post_send_hook` callback that fires after
   every successful send.
2. The hook creates a debounced `asyncio.Task` (2-second wait). If one is already
   pending, it's skipped — 6 concurrent notifications → only 1 bump.
3. After the delay: old panel message deleted, fresh panel sent at the bottom.
4. New panel message ID persisted to `app_settings` table so it survives server
   restarts.
5. On startup, `main.py` loads the persisted panel IDs from DB into `bot_data`.

**Files:** `app/main.py`, `app/bot/notifications.py`, `app/bot/handlers.py`

---

### Fix 8 — Duplicate Sweeps from Button Mashing / Rapid Restarts

**Symptom:** Tapping "Sweep All" multiple times quickly, or rapidly restarting
the server, could trigger multiple concurrent sweeps.

**Fix (button mashing):** Added `sweep_in_flight` boolean flag. The Sweep All
button handler checks it and shows an alert instead of launching a new sweep
if one is already running.

**Fix (rapid restart):** `last_sweep_at` is written to `app_settings` at the
**start** of a sweep (not the end). On startup, the scheduler reads this timestamp
and computes whether the next scheduled run is still in the future — if so, it
waits; if overdue, it fires within 5 seconds. This prevents a restart from
immediately re-running a sweep that just completed.

**Files:** `app/workers/scheduler.py`, `app/bot/handlers.py`

---

### Fix 9 — Sweep-Complete Silence

**Symptom:** After every scheduled sweep, the bot went completely silent when
nothing changed. No way to know if it had finished, was stuck, or had no accounts.

**Fix:** Added a summary message at the end of `MonitorService.check_all()` that
always fires:

```
👁 Sweep complete — 4 profiles checked.
👁 Sweep complete — 4 profiles checked. 2 failed: @user1, @user2
```

Failed profile usernames are listed explicitly.

**File:** `app/monitor/service.py` — end of `check_all()`

---

### Fix 10 — Back/Home Button Inconsistencies

**Symptom:** Some "back" buttons were labeled "Menu", some "Accounts", some "Back
to list" — inconsistent and confusing navigation.

**Fix (UI polish):** Full keyboard layout audit:

| Before | After |
|---|---|
| Orphaned "Help" button on its own row | Balanced 2×3 main menu grid |
| `➕ Add account` / `📤 Export CSV` | `➕ Add` / `📤 Export` |
| All back buttons labeled "Menu" | Unified to "Home" |
| Active preset: `• 30m` (barely visible) | `✓ 30m` (clear checkmark) |
| Page indicator `Page 1/2` (looks tappable) | `· 1 / 2 ·` (decorative dots) |
| `✅ Yes, remove` / `❌ Cancel` | `🗑 Remove` / `✕ Cancel` |
| `Open @username` (no emoji) | `👁 @username` |
| `◀️ Back to list` / `◀️ Accounts` | `◀️ List` (consistent) |

**Files:** `app/bot/keyboards.py`, `app/bot/handlers.py`

---

### Fix 11 — Timestamps in UTC 24h Format

**Symptom:** All timestamps shown in the bot were in UTC with 24-hour format,
inconvenient for Damascus-based users.

**Fix:** Added `DAMASCUS_TZ = timezone(timedelta(hours=3))` and a unified
`fmt_timestamp(dt)` function that converts all timestamps to Damascus local
time with 12-hour AM/PM format (`%Y-%m-%d %I:%M:%S %p`). All timestamp
display in the bot flows through this single function.

**File:** `app/utils/formatting.py` — `fmt_timestamp()`

---

### Fix 12 — `edit_message_text` Fails on Photo/Document Messages

**Symptom:** When a callback button was attached to a photo or document message
(e.g., after sending a profile picture), pressing "Back" or any navigation button
crashed with a Telegram `BadRequest` because `edit_message_text` cannot edit
media messages.

**Fix:** `_safe_edit_text()` in `handlers.py` now catches this specific error,
detects whether the message is a media message, deletes it, and sends a fresh
text message instead. Returns the new message object so callers can track it.

**File:** `app/bot/handlers.py` — `_safe_edit_text()`

---

## 12. Known Limitations

### Profile Picture Resolution Without Authentication
Instagram's 1080px profile pictures are behind HMAC-signed CDN URLs and require
a session cookie. Without `IG_SESSION_COOKIE`, the best available is ~320px from
`profile_pic_url_hd`. This is a hard platform limit — there is no URL manipulation
workaround (the `stp` parameter invalidates the signature).

### Stories and Highlights
The `storiesig.info` free API endpoint was shut down in 2024. All no-login
approaches to Instagram stories are either dead or Cloudflare-gated. The
`StoriesClient` code is fully implemented and will activate once an API key is
obtained. Set `STORIESIG_API_KEY` when available.

### Private Account Monitoring
Private accounts return the same `web_profile_info` response (follower count,
bio, etc.) but profile picture downloads may require an authenticated session.

### Render Free Tier Sleep
Render free web services sleep after 15 minutes of inactivity. The Telegram
webhook still wakes the service on incoming messages, but scheduled sweeps may
be delayed while the service is sleeping. Consider using Render's cron trigger or
upgrading to a paid tier for consistent scheduling.

---

## 13. Roadmap

- [ ] `STORIESIG_API_KEY` integration once API access is obtained (code already written)
- [ ] Multi-platform support: X (Twitter), TikTok, YouTube
- [ ] Frontend web dashboard (replacing Telegram-only interface)
- [ ] Per-account configurable intervals
- [ ] Change threshold alerts (e.g., "notify only if followers drop by >10%")
- [ ] Webhook delivery to external systems (Slack, Discord, custom HTTP)
