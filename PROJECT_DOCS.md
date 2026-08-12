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
| Profile picture | Downloaded locally, compared by perceptual fingerprint (resolution/JPEG-proof) |
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
2.  MonitorService.check_all() shuffles the active accounts and paces them
    through _SweepThrottle (one at a time by default; the gap widens, and then
    the sweep pauses outright, as 401/403 blocks pile up)
3.  Per account:
    a.  InstagramClient.fetch_profile()  →  200 JSON from Instagram
    b.  InstagramClient.fetch_hd_pic_url()  →  mobile API for full-res picture
    c.  MediaHasher.hash_url()  →  download + perceptual hash
    d.  detect_changes(previous_snapshot, new_snapshot)
    e.  If changed: INSERT AccountSnapshot, log NotificationLog
    f.  NotificationDispatcher sends text diff + picture document
4.  _retry_blocked() re-checks anything the gate blocked, in paced rounds —
    a 401 blocks a request, not an account, so most recover here
5.  StoriesClient checks stories/highlights; the story/live status is announced
    only when it changed and the media didn't already announce it
6.  Sweep-complete summary notification sent
7.  Panel-bump debounce: main-menu message moved to bottom of chat
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
| `profile_pic_hash` | text | Perceptual fingerprint `p2:<dhash>:<ahash>:<mean>` for change detection |
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
- 401/403 **direct** (no Worker): retried up to `max_retries` (5) with
  `random.uniform(1.0, 3.0)` jitter — a datacenter IP gets these
  intermittently and a re-ask often lands
- 401/403 **through the Worker**: budgeted by the caller, because one Worker
  call is already 8 upstream attempts. A sweep passes
  `IG_SWEEP_AUTH_ATTEMPTS` (1); on-demand callers get
  `IG_MANUAL_AUTH_ATTEMPTS` (3). The re-ask is worth something because a
  repeat call may leave from a different Cloudflare colo, and Instagram's
  gate answers differently per colo — but a sweep multiplies every attempt by
  every account, and that traffic is what keeps the gate shut
- 429: exponential backoff, capped at 60s
- 5xx: exponential backoff, capped at 30s
- 404: return immediately, no retry (rename recovery handles it)

**Proxy path:** When `IG_PROXY_URL` is set, requests are routed through the
Cloudflare Worker instead of hitting Instagram directly.

**Public-page fallback — the second door.** When the API ends in 401/403 the
client tries `instagram.com/<username>/` once, directly, and parses the Relay
payload the page itself renders from (`app/monitor/public_page.py`):

```json
"xig_user_by_username":{"pk":"7880052534","username":"65xim","is_private":true,
  "biography":"…","full_name":"Mohamad","is_verified":false,
  "follower_count":118,"following_count":577,"all_media_count":null}
```

Same shape the API returns, and it carries the privacy flag, so a private
account is never mistaken for a public one. The result is marked
`source="public_page"` and `partial` — it does not know `reels_count`,
`story_count` or `is_business`, and those carry forward rather than being
written as None.

**Not the `og:` meta block, which shipped first and was withdrawn a day later
(2026-08-12).** The tags are a stale cache, and the same response carries both:
`og:description` said *677 Following* for an account whose embedded payload,
rendered HTML and Instagram app all said **577**. Followers and posts matched
exactly, and nothing in the block marks it as old — so there was no read-time
test separating a good value from a bad one, and the whole surface had to go.
The lesson generalises and is now a standing rule: **a source being LIVE says
nothing about it being CORRECT.** Only the payload was checked against ground
truth, and only the payload passed.

Two mechanisms built during that round are still in force, because they guard
any partial reading:

- **Source-scoped diffing** (`crud.get_latest_snapshot_by_source`). The API and
  the page can disagree about the same account at the same moment, so
  alternating between them "detected" a change on every flip. Each source is
  diffed against its own history; carry-forward still uses the newest snapshot
  from any source, because "what we last knew" and "what is safe to diff" are
  different questions.
- **Bidi normalization** (`public_page.strip_bidi`). Instagram wraps RTL display
  names in invisible direction marks on some surfaces and not others, so an
  Arabic name reported as `لِ → ‎لِ‎` — two visibly identical strings. Marks are
  stripped at parse time and ignored in text comparison.

`crud.purge_partial_snapshots()` runs once at startup, bounded to rows written
before `crud.OG_ERA_CUTOFF`, deleting what the withdrawn `og:` parser wrote —
the account card reads the newest snapshot and would otherwise keep stating a
wrong number as current. The cutoff sits in the gap between the two parsers, so
it never touches the payload-sourced rows the current fallback writes.

Parsing is bounded — a 4 MB scan window, a 200 KB object ceiling, brace-matched
in one pass, and run off the event loop. An earlier version used a lazy `(.*?)`
under `re.DOTALL` that rescanned the whole document per non-matching tag; on a
multi-MB profile page that pinned the CPU long enough for the health endpoint to
time out and the instance to be killed.

**Verification status.** The payload matched ground truth for one account
(`@65xim`), on numbers checked by hand — the check that was missing the first
time, but still a sample of one. Whether the page is reachable at all from
Render's datacenter IP is unmeasured; if it answers with a login wall the parse
returns `None` and the fetch reports its original error. Run `/probe <user>` on
a few mixed public/private accounts and compare against the app.

**HD profile picture:** `i.instagram.com/api/v1/users/{id}/info/` returns
`hd_profile_pic_url_info` (up to ~1440px), but **only for logged-in sessions**.
In the anonymous default that call can never succeed, so it is skipped entirely
unless `IG_SESSION_COOKIE` is set — it was one guaranteed-wasted Instagram
request per account per sweep. The anonymous ceiling is `profile_pic_url_hd`
(~320px), with the media downloader's full-resolution avatar as a fallback.

### 6.2 MonitorService (`app/monitor/service.py`)

Orchestrates the full check pipeline:

1. Fetches the profile (API → public-page fallback), and an avatar only when
   the stored asset id shows it is a new upload
2. Diffs against the latest snapshot
3. Inserts a snapshot **only if something changed** (or first-ever check)
4. Sends notifications
5. Runs story/highlight checks (if `StoriesClient` is wired in)
6. Retries anything blocked, in paced rounds
7. Sends the sweep-complete summary

Concurrency is limited by `asyncio.Semaphore(MAX_CONCURRENT_FETCHES)`, and the
sweep itself is paced by `_SweepThrottle`:

- **`SWEEP_CONCURRENCY` lanes** (default 1 — one account at a time, the same
  request rhythm as a manual recheck). The gap is stamped when a check
  *finishes*, so it is a real gap between requests rather than between
  launches: a burst of launches all waiting on a semaphore was the old
  behavior, and it hit Instagram as one wave.
- **Adaptive pacing** — the gap widens by one step per consecutive 401/403 up
  to `SWEEP_STAGGER_MAX_SECONDS`, and relaxes on success.
- **A guard that distinguishes a throttle from an outage.** At
  `SWEEP_BREAKER_THRESHOLD` consecutive blocks: if *some* account has answered
  this sweep, it is a throttle — pause for `SWEEP_BREAKER_COOLDOWN_SECONDS`
  and carry on (those accounts stay in this sweep). If **nothing** has
  answered, the gate is shut: stop immediately, skip the retry rounds, and
  skip the per-account reel fallback, because no pace helps and every further
  request is blocked traffic that keeps it shut.
- **Retry rounds** (`SWEEP_RETRY_ROUNDS`, cooldown doubling 30s → 60s → 120s,
  bounded by `SWEEP_RETRY_BUDGET_SECONDS`) re-check blocked accounts one at a
  time. A block lands on a *request*, not an account, so a paced retry often
  goes through — this is what stops the summary reporting failures the owner
  can reproduce as successes by hand a minute later.
- **Order is shuffled every sweep**, so the same tail of accounts doesn't
  absorb every block sweep after sweep.

### 6.3 WatcherScheduler (`app/workers/scheduler.py`)

APScheduler wrapper with two jobs:

| Job | Trigger | Role |
|---|---|---|
| `watcher-sweep` | `IntervalTrigger` (configurable, default 30m) | Runs `check_all()` |
| `watcher-cleanup` | `CronTrigger` — 03:00 UTC daily | Purges old DB rows and expired media files |
| `watcher-digest` | `CronTrigger` — `DIGEST_HOUR` UTC daily | Sends the roll-up when the runtime mode is daily/weekly |
| `stakeout:<id>` | `IntervalTrigger` (per target, temporary) | One high-frequency watch; removed when the window ends |

Overlap is impossible: the sweep job is `max_instances=1` with `coalesce=True`,
and a re-entrancy flag makes a manual trigger skip while one is already in
flight. `SWEEP_TIMEOUT_SECONDS` bounds a hung sweep — it has to clear the
guard's pauses plus the retry budget, or it would start killing healthy sweeps
mid-flight, which looks exactly like the failures it exists to prevent.
Stakeouts are persisted to `app_settings` and re-armed after a restart.

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

### 6.5 Panel Bump (`app/bot/panel_bump.py`)

After every batch of notifications, the panel is moved to the bottom of the
chat so it's always accessible:

1. `post_send_hook` fires after each successful send
2. A 2-second debounced `asyncio.Task` waits for concurrent notifications to land
3. If an on-demand download is running, the bump **waits for it to finish**
   (bounded) rather than being dropped — bumping between the items of a batch
   would wedge a menu between every photo, but dropping it strands the panel
   above the media
4. The old panel is deleted and re-posted at the bottom
5. New panel message ID is persisted to `app_settings` (survives restarts)

**It re-posts the panel's CURRENT view, not a hardcoded menu.** The panel is
also where Status, "Sweep running" and Dark radar render, so posting the menu
back threw those views away — pressing 🔄 Sweep All opened a view that the
sweep's own first notification replaced a second later. Every edit records what
it drew (`{message_id: (text, keyboard)}`, bounded), and the record follows the
panel to its new message id so the next bump preserves it too. The menu is the
fallback only when the content is genuinely unknown (a restart dropped it).

### 6.6 StoriesClient (`app/monitor/stories.py`)

Fetches story, highlight and post media from **saveinsta.to** — login-free, no
API key. Uses the same Chrome TLS impersonation as the rest of the project.
(The older `storiesig.info` API this once used is dead and gone.)

The flow is a three-step handshake, with the tokens cached so repeat fetches
skip two of the round-trips:

```
GET  saveinsta.to/en/highlights   -> page carries k_exp / k_token
POST saveinsta.to/api/userverify  -> issues a per-request cftoken
POST saveinsta.to/api/ajaxSearch  -> returns media HTML for the target URL
```

- `fetch_stories(username)` → list of `StoryItem` (images + videos)
- `fetch_story_by_url(url)` → the same for a single story permalink
- `fetch_highlight_items(username, highlight_id, title)` → one reel's media
- `fetch_posts(username, limit=12)` → recent grid posts/reels, newest first
- `fetch_profile_pic_url(username)` → full-resolution avatar (the anonymous web
  API tops out at ~320px)
- `download(item, username)` → saves to `{MEDIA_DIR}/{username}/stories/{pk}.jpg|mp4`
- Dedup by `story_pk` against the `seen_stories` table

**Why it matters beyond media:** this runs on infrastructure unrelated to
Instagram's own gate, so it keeps working when the API path is 401-blocked.
During an outage the story media still arrives; only the live *status* goes
unknown, and the bot says so rather than guessing.

### 6.7 Cloudflare Worker Proxy

When Render's Frankfurt datacenter IPs are blocked by Instagram, a transparent
Cloudflare Worker proxy is used for EVERY Instagram API call:

- URL: `https://ig-proxy.m-asaad2005-ma.workers.dev`
- `?username=<x>` → web_profile_info (profile fields)
- `?user_id=<id>` → graphql reel query (story/live status, highlight catalog,
  username-by-id — powers `/add <numeric id>` and the ✨ Highlights button)
- `?hd_user_id=<id>` → mobile API user info (HD profile picture; only useful
  with a logged-in session, which the anonymous setup doesn't use)
- Rotates across 6 user agents, retries 8 times; a 200 with a non-JSON body
  (login-wall HTML) counts as blocked and is retried
- Free tier: 100,000 requests/day

Set `IG_PROXY_URL` in environment variables to enable. The bot falls back to
direct Instagram requests if the proxy is unreachable, and serves repeated
reel queries from a 90-second in-memory cache to keep request volume low.

**Cloudflare edge IPs are not immune — they are just better odds.** Worth
stating plainly, because the opposite assumption shaped earlier versions of
this design:

- Worker `fetch()` egress leaves from Cloudflare's published ranges under a
  single well-known ASN, which every IP-intelligence dataset labels as
  datacenter. Classifying it is a list lookup, not clever detection.
- Those IPs are **shared** with every other Worker on the platform, so the
  reputation attached to them is everyone's aggregate traffic, not yours. This
  is why blocks flip per colo and differ per account, and why the same username
  can 401 one minute and answer the next.
- The 8 upstream attempts inside one call all leave from the **same colo with
  the same TLS fingerprint** — they vary the User-Agent and host, which are not
  what the gate keys on. Separate calls have a chance at a different colo,
  which is why the manual path re-asks and the sweep does not.
- A Worker hop also **loses the Chrome TLS fingerprint**: the runtime's own
  handshake carries a header claiming to be Chrome. That mismatch is a stronger
  signal than either fact alone, and it is why the public-page fallback is
  fetched directly instead.

The durable fix within a login-free design is a residential/mobile proxy
(`IG_PROXY_URL` unset, `PROXY_URL` set), which gets a consumer-ASN IP *and*
keeps the real Chrome fingerprint. The Worker remains the free default.

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
| `/pause @username` / `/resume @username` | Freeze or resume a target — the row, its Instagram ID and all history are preserved |
| `/stakeout @username [2h]` / `/unstakeout @username` | Temporary high-frequency watch on one target, then auto-revert |
| `/rhythm @username` | Hour-of-day and day-of-week activity histogram |
| `/darkradar` | Targets ranked by how long they've been silent |
| `/story @username` / `/highlights @username` | On-demand media for **any** public account, monitored or not |
| `/probe @username` | Test every profile source and report which answer — API, public page (HTTP status + byte count), media downloader |
| `/digest [off\|daily\|weekly]` | Show, preview, or set the roll-up mode |
| `/synctopics` | Create one forum topic per account (needs `TELEGRAM_FORUM_TOPICS=true`) |
| `/kill` (alias `/stop`) | Abort an in-progress on-demand download; already-sent media stays |
| `/export` | Download a CSV of all change history |
| `/help` | Show help text |

`/rm` is an alias for `/remove`. Sweeps and scheduled jobs are unaffected by
`/kill`, which only cancels on-demand downloads.

### Inline Menu Navigation

The main panel buttons: **Accounts · Status · Add · Interval · Export · Help ·
🔎 Any user · 📦 Download all · Sweep All**.

From an account card: **Recheck · History · Photo · Remove · Story ·
Highlights · Pause ⇄ Resume · Home**.

The panel always stays as the last message in the chat — automated sweep
notifications push it back down, and the panel-bump logic re-sends it after
every batch of notifications.

### 📦 Bulk Download

The **Download all** button on the main menu grabs a whole account in one flow:

1. Asks whether the account is in the monitored list — pick it from the
   paginated list, or type a **username**, **profile URL**, or **numeric
   Instagram ID** (same parsing as `/add`).
2. Shows a checkbox panel: **📖 Story · 👤 Profile pic · 🖼 Photos ·
   🎬 Reels**, plus **every highlight listed by name** and a
   select-all-highlights shortcut.
3. **⬇️ Download selected** sends exactly the ticked items —
   e.g. just the reels and two of six highlights. **⚡ Download EVERYTHING**
   sends story + photos + reels + profile picture + all highlights without
   ticking anything.

Per-category progress is edited into the message as the run advances, and a
summary closes it out. Works for any public account, monitored or not, and
stays 100% login-free (anonymous GraphQL catalog + saveinsta media paths —
photos/reels cover what the anonymous profile grid serves). For monitored
accounts, delivered items are marked seen so the next sweep never re-sends
them. Selection state lives in `user_data` (`dl:*` callbacks in
`app/bot/keyboards.py`); the download fan-out is `_run_bundle_download` in
`app/bot/handlers.py` backed by `download_posts`,
`download_highlights_from_catalog`, `fetch_and_send_profile_picture`, and
`get_download_overview` in `app/monitor/service.py`. The panel's
already-fetched highlight catalog and numeric id are reused by the download
steps, so a full run makes at most one Instagram web call — important on
datacenter IPs (e.g. Render), where Instagram starts returning 401 after a
few requests; the media itself flows entirely through saveinsta.

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
| `IG_SESSION_COOKIE` | — | Full cookie string from a logged-in browser session (enables HD profile pictures). Optional — login-free is the default and recommended mode |
| `IG_PROXY_URL` | — | Cloudflare Worker proxy URL for datacenter IP bypass |
| `IG_SWEEP_AUTH_ATTEMPTS` | `1` | Worker re-asks per blocked check during a sweep (each call is already 8 upstream attempts) |
| `IG_MANUAL_AUTH_ATTEMPTS` | `3` | Worker re-asks for an on-demand check — a repeat call may land on a different colo |

### Scheduler

| Env Var | Default | Description |
|---|---|---|
| `CHECK_INTERVAL` | `1800` | Sweep interval in seconds |
| `JITTER_SECONDS` | `120` | Random jitter added to each sweep interval |
| `MAX_CONCURRENT_FETCHES` | `3` | Max parallel Instagram fetches per sweep |
| `REQUEST_TIMEOUT` | `20` | HTTP request timeout in seconds |
| `SWEEP_CONCURRENCY` | `1` | Accounts checked at a time (1 = the manual-recheck rhythm) |
| `SWEEP_STAGGER_MAX_SECONDS` | `12` | Widest gap between checks once blocks pile up |
| `SWEEP_BREAKER_THRESHOLD` | `5` | Consecutive 401/403s that trip the guard (0 = off) |
| `SWEEP_BREAKER_COOLDOWN_SECONDS` | `90` | Mid-sweep pause when it trips (0 = defer instead) |
| `SWEEP_RETRY_ROUNDS` | `3` | Paced re-check rounds for blocked accounts (0 = off) |
| `SWEEP_RETRY_BUDGET_SECONDS` | `300` | Wall-clock budget shared by those rounds |
| `SWEEP_TIMEOUT_SECONDS` | `1500` | Hard cap on one sweep; must clear the pauses + retry budget |

### Data Retention

| Env Var | Default | Description |
|---|---|---|
| `SNAPSHOT_RETENTION_DAYS` | `30` | Delete old snapshot rows (0 = keep forever) |
| `NOTIFICATION_RETENTION_DAYS` | `90` | Delete old notification log rows |
| `RAW_RESPONSE_RETENTION_DAYS` | `7` | NULL out `raw_response` JSONB on old rows |
| `MEDIA_RETENTION_DAYS` | `14` | Delete downloaded story/post files older than this — already delivered to Telegram, and on-demand requests re-download |

### Monitoring Behavior

| Env Var | Default | Description |
|---|---|---|
| `STORY_STATUS_HEARTBEAT` | `false` | `true` posts a story/live status line every sweep (the old behavior). Off = announced only when it changes, and never on top of the media that already announced it |
| `HIGHLIGHT_SCAN_INTERVAL` | `21600` | Seconds between full re-lists of every highlight's media. New reels are always listed immediately. `0` = every sweep (~12× traffic) |
| `AUTO_GRAB_ON_PUBLIC` | `true` | Deliver the whole backlog when a target flips private → public |
| `DARK_RADAR_DAYS` | `3` | Flag a target after this many days with no story/post/reel (`0` disables) |
| `FOLLOWER_ANOMALY_ABS_MIN` | `500` | Follower-jump alert fires only when the change is large in **both** absolute and relative terms |
| `FOLLOWER_ANOMALY_PCT_MIN` | `0.10` | The relative half of that test |
| `DIGEST_HOUR` | `9` | UTC hour a scheduled digest fires (mode is set at runtime via `/digest`) |
| `DIGEST_WEEKDAY` | `0` | Weekday for a weekly digest (0 = Monday) |
| `STAKEOUT_DEFAULT_INTERVAL` | `180` | Seconds between checks during a stakeout |
| `STAKEOUT_MIN_INTERVAL` | `120` | Floor, kept above the 90s reel cache so every tick is fresh |
| `STAKEOUT_DEFAULT_DURATION` | `3600` | Default stakeout length when none is given |
| `STAKEOUT_MAX_DURATION` | `21600` | Hard cap (6h) on one stakeout |

### Chat Routing

| Env Var | Default | Description |
|---|---|---|
| `TELEGRAM_FORUM_TOPICS` | `false` | One forum topic per account in a Topics-enabled group; global messages stay in General |
| `TELEGRAM_MIRROR_CHAT_IDS` | — | Comma-separated extra chats receiving a flat copy of every notification (mirrors never use topics) |

### Storage & Misc

| Env Var | Default | Description |
|---|---|---|
| `MEDIA_DIR` | `./data/media` | Local directory for downloaded profile pictures |
| `WEB_API_TOKEN` | — | Bearer token for HTTP API auth |
| `LOG_LEVEL` | `INFO` | Logging level |
| `PORT` | `8000` | Web server port (injected by Render automatically) |
| `PROXY_URL` | — | Optional HTTP/HTTPS/SOCKS5 proxy for all outbound requests. Wins over `HTTP_PROXY`/`HTTPS_PROXY`. Note it wraps the whole session, so with `IG_PROXY_URL` set it applies to the hop that reaches the Worker — not to the Worker's own egress to Instagram |
| `HTTP_PROXY` / `HTTPS_PROXY` | — | Standard proxy env vars, used when `PROXY_URL` is unset |

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
