<div align="center">

# 👁 The Watcher

### Instagram monitoring, delivered to your Telegram. 100% login-free.

Track any Instagram account — **public or private** — followers, bio, profile picture, stories, posts, reels — and get every change **plus the actual media** dropped straight into your chat. No Instagram account. No cookies. Nothing that can get banned.

**Battle-tested in production: one instance quietly watching 25+ accounts, around the clock.**

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=flat&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?style=flat&logo=telegram&logoColor=white)](https://core.telegram.org/bots)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?style=flat&logo=postgresql&logoColor=white)](https://postgresql.org)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?style=flat&logo=docker&logoColor=white)](Dockerfile)
[![License](https://img.shields.io/badge/License-MIT-22c55e?style=flat)](LICENSE)

[Quick Start](#-quick-start-local) · [Features](#-features) · [Deploy to Render](#-deploy-to-render) · [Commands](#-telegram-commands--menus) · [HTTP API](#-http-api)

<img src="docs/assets/account-card.svg" width="420" alt="The Watcher — live account card inside Telegram, with story status, highlights, and one-tap actions" />

<sub>*An account card, live in Telegram. Every feature is one tap away.*</sub>

</div>

---

## Why The Watcher?

- 🕵️ **Truly anonymous.** Works with zero Instagram credentials — no account, no session cookie, no device fingerprint tied to you. There is nothing for Instagram to ban.
- 🔒 **Private accounts too.** Follower/following/post counts, bio, name, username, profile picture, and privacy flips are tracked even for private accounts — anonymously. (Media delivery applies to public accounts; private content is never accessed.)
- 🎬 **It doesn't just notify — it delivers.** New stories, posts, reels, highlights, and profile pictures arrive in your chat as actual photos and videos, not links.
- 📦 **One tap grabs a whole account.** The **Download all** panel pulls the story, photos, reels, every highlight, and the profile picture of any public account — all of it, or just the parts you tick.
- 📲 **Telegram is the entire UI.** Add targets, pause them, pull stories, tune the schedule, export history — all through inline buttons. You never touch a terminal after deploy.
- ☁️ **Built for datacenter hosting.** Instagram 401-blocks cloud-host IPs wholesale, so The Watcher routes **every** Instagram API call through a free Cloudflare Worker at the edge, and when that gate does close it falls through to the public profile page and a login-free media downloader on separate infrastructure. Render, Fly, any VPS.
- ⚡ **Proven at scale.** A single instance sweeps 25+ accounts around the clock — jittered scheduling, one account at a time, and a guard that backs off (or stops) the moment Instagram starts refusing, instead of hammering a shut door.
- 📦 **One container, five minutes.** A single Docker image with a `render.yaml` blueprint — database, persistent disk, and webhook included. Runs on a free-tier box.

---

## ✨ Recently Shipped

| | |
|---|---|
| 🚪 **A second door when Instagram's API shuts** | When `web_profile_info` answers 401, the bot falls back to the public profile page and reads follower/following/post counts, name and avatar from its Open Graph block — live, login-free, and fetched directly so it carries a real Chrome TLS fingerprint. Partial by nature and handled as such: fields it can't see stay untouched rather than being written as blank, so a fallback check can never fire a phantom "bio removed". |
| 🔬 **`/probe <user>`** | Tests every source against one account and reports which answer — the API, the public page (with HTTP status and byte count), and the media downloader. Turns "everything is 401ing" into a specific, actionable answer in a few seconds, and logs the same at INFO. |
| 🔕 **One story, one message** | A single story used to produce a status line in *every* sweep it survived, plus a "just posted a story!" alert, plus the media itself. Now the status is announced only when it changes, and never on top of the media that already announced it. `STORY_STATUS_HEARTBEAT=true` restores the old behavior. |
| 🚦 **A guard that tells a throttle from an outage** | If some accounts answer, blocks mean a throttle: widen the gap, pause, carry on. If **nothing** answers, the gate is shut — the sweep stops immediately rather than spending hundreds of blocked requests proving it, and the summary says so instead of naming every account as a separate failure. |
| 🧵 **One thread per account** | In a Topics-enabled group, the bot gives every monitored account its own forum thread — that account's profile changes, story status, highlights, media, and went-dark alerts route to its thread, while sweeps and summaries stay in General. Enable with `TELEGRAM_FORUM_TOPICS=true` then **Status → 🧵 Sync topics**. Deleted-topic and non-forum cases fall back to General safely. |
| 🎯 **Stakeout mode** | `/stakeout @user 2h` (or the 🎯 button) watches a single target on a tight loop for a set window, then auto-reverts to the normal schedule. Every tick is a full check — profile, posts, reels, stories, highlights — all through the edge proxy and 90s cache, with an interval floor that keeps it clear of Instagram's rate limits (no 401s). Survives restarts. |
| 📊 **Activity rhythm** | `/rhythm @user` (or the 📊 button) charts *when* a target is active — an hour-of-day and day-of-week histogram built from everything the bot has caught, in your local time. Spot the windows they post in at a glance. |
| 🌑 **Went-dark radar** | The sweep flags any account that posts nothing — no story, post, or reel — for N days (`DARK_RADAR_DAYS`, default 3), and announces the comeback when they return. `/darkradar` lists every target ranked by how long they've been quiet. |
| 🛰 **Every Instagram call rides the edge** | Story/live status, highlight names, and `/add` by numeric ID now go through the same free Cloudflare Worker proxy as profile fetches — so they work flawlessly from cloud hosts whose IPs Instagram 401-blocks. Repeated lookups are served from a 90-second cache, and retry storms against blocked endpoints are gone. |
| 🪶 **Featherweight database** | Each snapshot now stores ~300 bytes instead of Instagram's full 50–200 KB payload — ~99% smaller. A free 0.5 GB Postgres now lasts effectively forever. |
| 🔄 **Full-coverage rechecks** | A manual recheck (🔄 button, `/recheck`, REST endpoint) now covers exactly what a scheduled sweep covers: profile diff, new posts & reels, story & live status, highlight catalog changes, and new story/highlight media delivery. |
| 📦 **Bulk download — a whole account in one tap** | New home-menu button. Pick a monitored account or type any username, profile URL, or numeric ID, then tick exactly what you want — 📖 story, 🖼 photos, 🎬 reels, 👤 profile picture, and each highlight by name — or hit **⚡ Download EVERYTHING**. Live per-category progress, a final summary, and zero login, like everything else. |
| 🔕 **Per-highlight tracking** | Choose exactly which highlights to follow, per account. Mute one, several, or all — muted highlights are skipped by the sweep's auto-download (and not even fetched), while manual downloads keep working. Unmuting resumes cleanly from now, without dumping everything posted in between. |
| 🖼 **Post & reel auto-delivery** | Every sweep detects new posts/reels and sends the actual media to your chat — capped at 5 per sweep so a posting spree never floods you. First sweep baselines silently (no backlog dump). |
| 🔓 **Went-public auto-grab** | The moment a monitored account flips from private to public, the sweep automatically delivers its whole backlog — posts, reels, highlights, and current story — instead of baselining it silently. Retries on a later sweep if the source is briefly rate-limited. Toggle with `AUTO_GRAB_ON_PUBLIC` (default on). |
| ⏸ **Pause / resume targets** | Freeze monitoring with one tap or `/pause` — history, snapshots, and the resolved Instagram ID are all preserved. Resume picks up exactly where it left off. |
| 🔎 **Any public account, on demand** | `/story @user` and `/highlights @user` grab media from **any** public account — no need to monitor it. Also available as the **🔎 Any user** menu button. |
| ⬇️ **Download-all highlights** | One button fetches every highlight of an account, full quality. |
| 🔴 **Live & story status** | The account card shows `🔴 live now` / `🎬 has an active story` in real time — checked at the moment you open the card, not at the last sweep. |
| 🖼 **Max-quality profile pictures** | Full-resolution avatars instead of the 320 px anonymous ceiling, with automatic fallback for accounts the high-res path can't reach. |
| ⚡ **Faster everywhere** | Downloader tokens are cached, blocked endpoints fast-fail instead of timing out, and account cards render instantly. |

---

## How It Works

The Watcher runs as a single container. It connects to your Telegram bot, sweeps a list of Instagram targets on a schedule, diffs each profile against its last snapshot, and pushes changes — with media — to your chat.

```
 Telegram chat ──► commands & inline menus ──► FastAPI + APScheduler
                                                      │  sweep
                     ┌────────────────────────────────┼────────────────────────────┐
                     ▼                                ▼                            ▼
        Instagram API (web + graphql)     Public profile page          Anonymous media downloader
    via Cloudflare Worker edge proxy      direct · Chrome TLS          stories · highlights · posts
    90s cache · full profile fields       og: counts, name, avatar     reels · full-res avatars
    story/live status · highlights        (fallback when the API 401s)
                     └────────────────────────────────┼────────────────────────────┘
                                                      ▼
                                                 PostgreSQL
                                    snapshots · diffs · media hashes · dedup
                                                      │  change detected
                                                      ▼
                                                  Telegram
                                       formatted alert + photos/videos
```

**Three independent doors mean one being blocked never takes the bot down.**

1. **The API**, routed through a free Cloudflare Worker on edge IPs — full profile fields, story/live status, and the highlight catalog. This is the authoritative source and the one Instagram gates hardest.
2. **The public profile page**, tried automatically when the API answers 401. Its Open Graph block carries the follower/following/post counts, display name and avatar — the fields the bot actually alerts on. It is fetched *directly*, so it carries `curl_cffi`'s real Chrome TLS fingerprint (a Worker hop cannot: its runtime fingerprint would contradict a Chrome User-Agent), and it sends no `x-ig-app-id`, so it reads as a page view rather than a private-API call. Live data, never a cache — it reports only the fields it can actually see.
3. **A login-free media downloader** for story/post/reel media and full-resolution avatars, on infrastructure that cloud-IP blocks don't touch.

`/probe <username>` tests all three and tells you which are answering right now.

---

## 🚀 Features

### Change Detection
- Tracks 10+ profile fields: followers, following, posts, reels, highlights, biography, full name, username, external link, verification badge, business flag, public/private status
- **Works on private accounts** — all profile-level fields above are tracked for private targets, including an alert the moment an account goes private or public
- Profile-picture change detection — avatars are compared by a perceptual fingerprint that ignores Instagram's per-URL re-encodes (so it never cries wolf) yet catches real swaps, and archived to disk
- Story & live status checked every sweep and announced when it changes — one message per story, not one per sweep — plus a live check whenever you open an account card
- Highlight catalog tracking — detects added, renamed, and removed highlights by name
- Sweep-complete summary after every run, so you always know the bot is alive

### Media Delivery
- **New posts & reels** auto-downloaded and sent as photos/videos the sweep they appear
- **Stories** fetched and delivered with per-item deduplication — each story is sent exactly once
- **Highlights** listed by name with per-highlight download buttons and a download-all option
- **Per-highlight mute** — 🔕 toggle any highlight (or mute/track all at once) to control exactly what the sweep auto-downloads; muted ones are marked on the account card and skipped without being fetched
- **Profile pictures** in maximum available resolution, on demand via `/fetchphoto`
- **📦 Bulk download** — one panel grabs a whole account: story, photos, reels, profile picture, and any (or all) highlights, with checkbox selection or a one-tap **⚡ EVERYTHING** button
- All media retrieval is **login-free** — no Instagram session is ever used

### On-Demand Lookups
- `/story @user` and `/highlights @user` work on **any public account**, monitored or not
- **🔎 Any user** menu button does the same with zero typing
- **📦 Download all** menu button bulk-grabs any account — monitored or not — by username, profile URL, or numeric Instagram ID

### Intelligence
- **🎯 Stakeout mode** — temporarily watch one target on a tight loop (`/stakeout @user 2h` or the 🎯 button), then auto-revert. Rate-limit-safe: a hard interval floor above the 90s cache, all traffic via the edge proxy. Survives restarts and shows in `/status`
- **📊 Activity rhythm** — `/rhythm @user` renders an hour-of-day and day-of-week histogram of when a target is active, in local time
- **🌑 Went-dark radar** — sweeps flag accounts silent for `DARK_RADAR_DAYS` days and announce their return; `/darkradar` ranks every target by how long it's been quiet

### Target Management
- Add targets by `@username`, full profile URL, or raw numeric Instagram ID
- Pause/resume per target — paused accounts keep their entire history and resolved Instagram ID
- Paginated account list with live 🟢 / ⏸ state markers
- Per-target forced recheck, change history, and stored-photo retrieval

### Reliability
- **Every Instagram API call routed through a Cloudflare Worker edge proxy** (free tier, 100k req/day) — cloud-host IP blocks never reach the bot; falls back to direct requests if the proxy is down
- Chrome TLS fingerprint impersonation (`curl_cffi`) to clear 401/403 walls on the direct path
- 90-second reel-data cache — sweeps and card opens never re-ask Instagram for the same data
- Fast-fail circuit breaker on blocked endpoints instead of retry storms
- **Three independent doors to Instagram** — the API through the edge proxy, the public profile page as a fallback, and a login-free media downloader. When the API gate shuts, follower/following/post counts still arrive from the page, and stories/posts keep flowing from the downloader
- Sweeps check one account at a time — the same request rhythm as a manual recheck, since bursts are what trip Instagram's anonymous rate limiter
- Rate-limit guard that tells a throttle from an outage: if some accounts are answering, it widens the gap and pauses until the window clears; if **nothing** is answering it stops the sweep outright, because no pace helps a shut gate and every extra request keeps it shut
- Blocked-request amplification is budgeted per path — one proxied call is already 8 upstream attempts, so a sweep asks once (14 accounts multiply everything), while an on-demand check retries across Cloudflare colos, which the gate answers differently
- Anything still blocked is retried in paced rounds before it's ever called a failure — and the summary names a shut gate as one problem instead of listing every account as if each had failed separately
- Cached downloader tokens — the three-step token handshake runs once, not per request
- Tenacity retries with exponential backoff; debounced failure alerts (no 429 spam)
- Consecutive-failure counter per target, visible in `/status` and `/list`

### Data & API
- PostgreSQL persistence: snapshots, media hashes, notification log, seen-item dedup, runtime settings
- **Featherweight snapshots** — ~300 bytes stored per check instead of Instagram's 50–200 KB raw payload, so a free 0.5 GB database lasts for years
- Configurable retention windows + a **Clear Old Data** button right in the bot
- HTTP API with liveness/readiness probes and a cron-compatible `/sweep` endpoint
- Token-gated mutation endpoints; CSV export of the full notification history

---

## 🏁 Quick Start (Local)

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

The bot starts in long-polling mode (no public URL required). Send `/add <username>` to your bot and you're monitoring.

---

## 🐳 Docker

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

The container exposes `/health` and ships a built-in `HEALTHCHECK`.

---

## ☁️ Deploy to Render

The `render.yaml` blueprint provisions everything automatically.

1. Fork this repository.
2. Create a free **[Neon](https://neon.tech)** Postgres (free tier, no card, and
   — unlike Render's free database — it doesn't expire). Copy its connection
   string; it can be pasted in verbatim (the `postgres://` prefix and
   `sslmode`/`channel_binding` params are normalized for asyncpg automatically).
3. In Render: **New +** → **Blueprint** → select your fork. Render provisions:
   - A Docker web service
   - A 1 GB persistent disk at `/app/data` for stored profile pictures
4. Set the secrets in the Render dashboard:

   | Variable | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | Token from [@BotFather](https://t.me/botfather) |
   | `TELEGRAM_CHAT_ID` | Your chat or channel ID |
   | `TELEGRAM_ADMIN_IDS` | Comma-separated Telegram user IDs (optional) |
   | `DATABASE_URL` | Your Neon connection string |

   `WEB_API_TOKEN` is auto-generated by Render.

5. Click **Deploy**. The service registers a Telegram webhook on its public URL and starts sweeping immediately — Instagram's cloud-IP blocks are bypassed out of the box: `render.yaml` presets `IG_PROXY_URL` to a Cloudflare Worker edge proxy that carries every Instagram API call.

> **Already running on Render's free Postgres?** It expires ~30 days after
> creation. Move your data to Neon with zero loss using
> [`scripts/migrate_db.py`](scripts/migrate_db.py) — see the runbook at
> [docs/2026-06-11-neon-db-migration.md](docs/2026-06-11-neon-db-migration.md).

### Optional: External Cron Trigger

Render's free tier may suspend the service between requests. Use a Render Cron Job or any external scheduler to keep sweeps firing:

```bash
curl -fsS -X POST https://<your-service>.onrender.com/sweep \
  -H "X-API-Token: $WEB_API_TOKEN"
```

---

## ⚙️ Configuration

All settings come from environment variables. Copy `.env.example` to `.env` for local development.

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
| `TELEGRAM_WEBHOOK_URL` | _(empty)_ | Public base URL for webhook registration. Auto-inferred from `RENDER_EXTERNAL_URL` on Render |
| `TELEGRAM_WEBHOOK_SECRET` | _(empty)_ | Optional secret validated against Telegram's `X-Telegram-Bot-Api-Secret-Token` header |
| `TELEGRAM_WEBHOOK_PATH` | `/telegram/webhook` | Webhook path registered with Telegram and mounted by FastAPI |

### Scheduler

| Variable | Default | Description |
|---|---|---|
| `CHECK_INTERVAL` | `1800` | Seconds between full sweeps |
| `JITTER_SECONDS` | `120` | Maximum random seconds added to each interval |
| `MAX_CONCURRENT_FETCHES` | `3` | Max parallel profile fetches per sweep |
| `REQUEST_TIMEOUT` | `20` | Per-request timeout in seconds |
| `SWEEP_TIMEOUT_SECONDS` | `1500` | Hard cap on one sweep, after which it's abandoned so a hung connection can't block every later run. Must clear the paced sweep *plus* whatever the guard may legitimately spend waiting (pauses + retry budget), or it starts killing healthy sweeps mid-flight. Keep it well under `CHECK_INTERVAL` |

### Sweep Pacing & Rate-Limit Guard

Pacing-on-failure only — none of this ever changes a request, just when (and whether) the next one happens.

| Variable | Default | Description |
|---|---|---|
| `SWEEP_CONCURRENCY` | `1` | Accounts checked at once. `1` is the same request rhythm as a manual recheck, which is the pattern Instagram's anonymous gate answers reliably. Raise only if a sweep can't finish in time — every extra lane is a bigger burst |
| `SWEEP_STAGGER_MAX_SECONDS` | `12` | Ceiling on the gap between checks. The gap starts at 2s and widens by one step per consecutive block, relaxing back on success |
| `SWEEP_BREAKER_THRESHOLD` | `5` | Consecutive 401/403 blocks before the guard reacts. `0` disables the guard (adaptive pacing still runs) |
| `SWEEP_BREAKER_COOLDOWN_SECONDS` | `90` | How long the sweep pauses when blocks pile up *but some accounts are answering* — a throttle worth waiting out. `0` = defer immediately instead of pausing. When **nothing** is answering the sweep stops outright instead, since no pace helps a shut gate |
| `SWEEP_RETRY_ROUNDS` | `3` | Paced re-check rounds for blocked accounts after the sweep, each after a longer cooldown (30s, 60s, 120s). A block lands on a *request*, not an account, so a paced retry often goes through — this is what stops the summary reporting failures you can't reproduce by hand. `0` disables |
| `SWEEP_RETRY_BUDGET_SECONDS` | `300` | Shared wall-clock budget for those rounds, so a real outage can't stretch a sweep indefinitely |

### Stakeout & Radar

| Variable | Default | Description |
|---|---|---|
| `STAKEOUT_DEFAULT_INTERVAL` | `180` | Seconds between checks during a stakeout |
| `STAKEOUT_MIN_INTERVAL` | `120` | Floor for the stakeout interval — kept above the 90s reel cache so every tick is fresh without risking 401s |
| `STAKEOUT_DEFAULT_DURATION` | `3600` | Default stakeout length in seconds when none is given |
| `STAKEOUT_MAX_DURATION` | `21600` | Hard cap (6 h) on a single stakeout |
| `DARK_RADAR_DAYS` | `3` | Flag a target after this many days with no story/post/reel. `0` disables the radar |
| `TELEGRAM_FORUM_TOPICS` | `false` | Give each account its own forum topic when the chat is a Topics-enabled group and the bot is admin with *Manage topics*. Global messages stay in General |
| `TELEGRAM_MIRROR_CHAT_IDS` | _(empty)_ | Comma-separated extra chats that receive a flat copy of every notification — e.g. your DM, while `TELEGRAM_CHAT_ID` is the forum group. Mirrors never use topics |

### Storage & Retention

| Variable | Default | Description |
|---|---|---|
| `MEDIA_DIR` | `./data/media` | Directory for downloaded profile pictures |
| `SNAPSHOT_RETENTION_DAYS` | `30` | Days to keep account snapshots. `0` = keep forever |
| `NOTIFICATION_RETENTION_DAYS` | `90` | Days to keep notification logs |
| `RAW_RESPONSE_RETENTION_DAYS` | `7` | Days to keep raw Instagram API responses |
| `MEDIA_RETENTION_DAYS` | `14` | Days to keep downloaded story/post files. They were already delivered to Telegram and an on-demand request re-downloads, so this is a cache, not an archive. `0` = keep forever |

### Notifications & Digest

| Variable | Default | Description |
|---|---|---|
| `STORY_STATUS_HEARTBEAT` | `false` | Post a `HAS STORY` / `NO STORY` / `LIVE NOW` line every sweep. Off by default: one story used to produce one of these per sweep for as long as it stayed up, on top of the "just posted a story!" alert *and* the media itself. Off means the status is announced only when it **changes**, and never on top of the media that already announced it. A manual recheck always answers, and every status is logged for the digest either way |
| `HIGHLIGHT_SCAN_INTERVAL` | `21600` | Seconds between full re-lists of every highlight reel's media. A reel only changes when its owner adds a story to it — and that story was already caught and delivered live minutes earlier. Reels new to the catalog are always listed immediately. `0` = re-list everything every sweep (~12× the traffic) |
| `AUTO_GRAB_ON_PUBLIC` | `true` | When a monitored account flips private → public, deliver its whole backlog instead of silently baselining it |
| `FOLLOWER_ANOMALY_ABS_MIN` | `500` | A follower change is flagged only when it's large in **both** absolute and relative terms, so it never fires on a small account's noise or a big account's drift. Either value at `0` disables the alert |
| `FOLLOWER_ANOMALY_PCT_MIN` | `0.10` | The relative half of that test (10% of the prior count) |
| `DIGEST_HOUR` | `9` | Hour (UTC) at which a scheduled digest fires. The mode — off/daily/weekly — is set at runtime with `/digest` |
| `DIGEST_WEEKDAY` | `0` | Weekday for a weekly digest (0 = Monday) |

### Instagram

| Variable | Default | Description |
|---|---|---|
| `IG_SESSION_COOKIE` | _(empty)_ | **Optional** — full cookie string from a logged-in browser session. The bot is fully functional without it; login-free is the default and recommended mode |
| `IG_PROXY_URL` | _(empty)_ | Cloudflare Worker that proxies **all** Instagram API calls — profile fields, story/live status, highlight catalog, ID-to-username resolution. Strongly recommended on cloud hosts (preset in `render.yaml`); without it the bot makes direct requests, which datacenter IPs usually get 401-blocked on |
| `IG_SWEEP_AUTH_ATTEMPTS` | `1` | How many times a sweep re-asks the Worker after a 401. One Worker call is already 8 upstream attempts, and a sweep multiplies every extra attempt by every account — that traffic is what keeps the gate shut, so the second chance is left to the retry rounds |
| `IG_MANUAL_AUTH_ATTEMPTS` | `3` | Same, for on-demand checks (Recheck, card open, `/story`). A repeat call may leave from a different Cloudflare colo, and the gate answers differently per colo — so it's a real second chance. One account with someone waiting is not what shuts the gate |

The story/live status is reported from live reel data only — never re-read from a stored snapshot. When Instagram doesn't answer, the bot says the status is unavailable rather than repeating a stale one.

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

## 💬 Telegram Commands & Menus

| Command | Description |
|---|---|
| `/start` or `/menu` | Open the main inline menu |
| `/add <target>` | Start monitoring — accepts `@username`, `https://instagram.com/username`, or a numeric Instagram ID. Runs an immediate baseline fetch |
| `/remove <user>` | Stop monitoring and delete all stored history (`/rm` is an alias) |
| `/pause <user>` | Pause monitoring — the target and its full history stay in the database |
| `/resume <user>` | Resume a paused target exactly where it left off |
| `/list` | All monitored accounts with 🟢 / ⏸ state, last-check status, and failure count |
| `/recheck <user>` | Force an immediate check outside the schedule |
| `/stakeout <user> [duration]` | Watch one target on a tight loop for a window (e.g. `2h`), then auto-revert. Rate-limit-safe |
| `/unstakeout <user>` | End a stakeout early |
| `/rhythm <user>` | Posting-time histogram — when the target is active, by hour and weekday |
| `/darkradar` | List monitored accounts ranked by how long they've been silent |
| `/synctopics` | Create a forum topic per account (needs `TELEGRAM_FORUM_TOPICS=true` + a Topics group) |
| `/interval [value]` | Show or set the sweep interval — `30m`, `1h`, `1800s`, `1h30m`. No argument shows presets |
| `/status` | Global stats: accounts (+ paused / needs-attention), last & next sweep, digest mode, gone-dark count, active guards, and last-hour fetch health |
| `/history <user>` | Recent detected changes for a target |
| `/digest [off\|daily\|weekly]` | Show/preview or set the daily or weekly roll-up of all changes (no argument previews the recent window) |
| `/photo <user>` | Latest stored profile picture and its SHA-256 hash |
| `/fetchphoto <user>` | Download the current profile picture in max quality — works for any public account |
| `/story <user>` | Download any public user's **current story** — no monitoring required |
| `/highlights <user>` | List any public user's highlights with per-item download buttons |
| `/probe <user>` | Test every profile source against one account and report which answer — the API, the public page (with status and byte count), and the media downloader. The fast way to tell a blocked gate from a broken route |
| `/kill` | Stop an in-progress on-demand download (story / highlights / posts / bulk). Already-sent media stays; the rest is skipped (`/stop` is an alias) |
| `/export` | Full notification history as CSV |
| `/help` | Command reference |

**Everything is also reachable through buttons** — no commands required:

- **Main menu** — Accounts · Status · Add · Interval · Export · Help · **🔎 Any user** · **📦 Download all** · **Sweep All**
- **Account card** — Recheck · History · Photo · Remove · **Story** · **Highlights** · **📊 Rhythm** · **🎯 Stakeout** · **Pause ⇄ Resume**
- **Highlights view** — ⬇️ Download all (n), one download button per highlight, plus 🔕/🔔 mute toggles per highlight and a mute-all / track-all shortcut
- **📦 Bulk download panel** — asks whether the target is monitored (pick from the list) or not (type a username, URL, or ID), then shows checkboxes for 📖 Story · 👤 Profile pic · 🖼 Photos · 🎬 Reels · every highlight by name, with a select-all-highlights shortcut. **⬇️ Download selected** sends exactly what's ticked; **⚡ Download EVERYTHING** sends it all — with live per-category progress and a final summary
- **Status view** — Sweep Now · **🌑 Dark radar** · Interval · **Clear Old Data** (with confirmation)
- **Interval picker** — presets from 5 m to 6 h plus free-form custom entry
- **Panel bumping** — after every notification the panel re-posts at the bottom of the chat, so it's always within thumb's reach. It re-posts *whatever view is currently open* — Status, "Sweep running", Dark radar — rather than snapping back to the menu, and it waits out a running download so it lands under the finished batch instead of between its media items

> **🧵 One thread per target:** run the bot in a Telegram group with **Topics**
> enabled, set `TELEGRAM_FORUM_TOPICS=true`, and tap **Status → 🧵 Sync topics**
> (or `/synctopics`). Each account gets its own thread; global messages stay in
> General. Full guide: [docs/telegram-forum-topics.md](docs/telegram-forum-topics.md).

---

## 🔌 HTTP API

All responses are JSON. Mutation endpoints require `X-API-Token` when `WEB_API_TOKEN` is configured.

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET`/`HEAD` | `/health` | None | Liveness probe — returns `{"status":"ok"}` |
| `GET` | `/ready` | None | Readiness probe — checks monitor and scheduler state |
| `GET` | `/status` | None | Stats: account counts, last sweep time, scheduler info |
| `GET` | `/accounts` | None | List all monitored accounts |
| `POST` | `/accounts/{username}/recheck` | Token | Force an immediate check for one account |
| `POST` | `/sweep` | Token | Trigger a full sweep across all active accounts |

```bash
curl -X POST https://<host>/sweep -H "X-API-Token: your-token"
```

---

## 🗄 Data Model

Tables are created automatically on first boot via SQLAlchemy `create_all`.

| Table | Description |
|---|---|
| `monitored_accounts` | One row per target: username, resolved Instagram ID, active/paused flag, last status, failure count |
| `account_snapshots` | One row per *changed* fetch: parsed profile fields, HTTP status, and a slim ~300-byte record (numeric id + what the reel query said) instead of Instagram's 50–200 KB payload. Identical repeat fetches are not stored |
| `profile_media_hashes` | One row per unique profile picture (SHA-256 + disk path), deduplicated across accounts |
| `notification_logs` | One row per dispatched change event: type, payload, delivery status |
| `seen_stories` | Delivery dedup for stories, highlight items, **and posts/reels** — each media item is sent exactly once |
| `stored_highlights` | Per-account highlight catalog (id + title) used to detect added/renamed/removed highlights, with the per-highlight `tracked` mute flag |
| `app_settings` | Key-value store for runtime-tunable config persisted across restarts |

Pausing a target never deletes anything — the row, its Instagram ID, and all history survive until you explicitly `/remove`.

---

## 📁 Project Layout

```
app/
├── api/            HTTP API routes (FastAPI router)
├── bot/            Telegram command handlers, inline menus, notification dispatch,
│                   panel re-anchoring
├── database/       SQLAlchemy models, async session, CRUD helpers
├── monitor/        Instagram client, public-page fallback parser, anonymous media
│                   downloader, change detector, perceptual hashing, sweep orchestrator
├── utils/          Logging setup, user-agent rotation, formatting helpers
├── workers/        APScheduler-based sweep worker
├── config.py       Pydantic Settings — environment-driven configuration
└── main.py         FastAPI app, lifespan wiring, service initialization
scripts/            Standalone test suites + operational tools (run_tests.py, migrate_db.py)
docs/               Architecture notes and dated engineering write-ups
Dockerfile
Procfile
render.yaml
requirements.txt
.env.example
```

---

## 🧪 Tests

Every suite is a standalone script that exits non-zero on failure — no pytest dependency. The runner executes each in its own subprocess, so a crash or a leaked event loop in one can't poison another.

```bash
python scripts/run_tests.py           # all offline suites
python scripts/run_tests.py -k story  # only suites whose name matches
python scripts/run_tests.py --all     # include live-network probes
```

Offline suites need no network and no Postgres (they run on SQLite), so they work in CI and in a sandbox. Live-network probes — which hit the real Instagram and downloader endpoints — are skipped unless you pass `--all`, because a red suite that only means "no internet here" teaches everyone to ignore red.

---

## 🧰 Tech Stack

| Component | Library | Version |
|---|---|---|
| Web framework | FastAPI | 0.115 |
| ASGI server | Uvicorn | 0.32 |
| Instagram client | curl_cffi | 0.15 |
| HTTP client | httpx (HTTP/2) | 0.28 |
| Telegram | python-telegram-bot | 21.9 |
| Task scheduler | APScheduler | 3.10 |
| ORM | SQLAlchemy (async) | 2.0 |
| Database driver | asyncpg | 0.30 |
| Config | pydantic-settings | 2.7 |
| Retry | Tenacity | 9.0 |
| Logging | Loguru | 0.7 |
| Image processing | Pillow | 11.0 |

---

## ⚖️ Responsible Use

- The Watcher reads only what Instagram serves **anonymously** through undocumented, rate-limited endpoints. For private accounts that means profile metadata only — private stories, posts, and media are never accessed.
- Only monitor accounts you have a legitimate reason to track: your own accounts, brand assets, or authorized OSINT research.
- Increase `CHECK_INTERVAL` and reduce `MAX_CONCURRENT_FETCHES` for large target lists.
- 401/403/429 responses are surfaced to you (debounced), so you know immediately if you're being throttled.

---

## ⭐ Support the Project

If The Watcher saves you time, **[star the repo](https://github.com/m0hx65/The_Watcher3.0/stargazers)** — it's the easiest way to help others find it. Issues and PRs are welcome.

## 📄 License

[MIT](LICENSE) © 2026 [Mohamad (m0hx65)](https://github.com/m0hx65)
