# Repo Professionalization — The Watcher V3.0

## What Was Done

### 1. README.md — Full Rewrite
The original README was functional but minimal. It was rewritten from scratch to meet professional open-source standards:

- **Badges row** — Python 3.12, FastAPI, Telegram, PostgreSQL, Docker, MIT license
- **Architecture diagram** — ASCII flow showing Telegram → FastAPI → Instagram → PostgreSQL → Telegram
- **Feature list** — organized into four categories: Monitoring, Telegram Interface, Reliability, Data & API
- **Table of contents** with anchor links
- **Quick start** — local setup in 4 steps
- **Docker** — build and run commands with volume mount
- **Render deployment** — step-by-step with the 3 required secrets and optional cron trigger
- **Complete configuration reference** — split into 6 sections covering all env vars including previously undocumented ones
- **Telegram commands table** — all 9 commands with clear descriptions
- **HTTP API reference** — all 6 endpoints with auth requirements and a curl example
- **Data model** — all 5 tables explained
- **Project layout** — directory tree with purpose of each folder
- **Tech stack table** — all major libraries with versions
- **Responsible use** section

---

### 2. .env.example — Updated
Added missing environment variables that existed in the app but were not documented:

| Added Variable | Purpose |
|---|---|
| `SNAPSHOT_RETENTION_DAYS` | Days to keep account snapshots |
| `NOTIFICATION_RETENTION_DAYS` | Days to keep notification logs |
| `RAW_RESPONSE_RETENTION_DAYS` | Days to keep raw API responses |
| `IG_SESSION_COOKIE` | Optional Instagram session cookie |
| `IG_PROXY_URL` | Optional proxy for Instagram requests |
| `PORT` | Web server port |

All variables now have inline comments and are grouped into labeled sections.

---

### 3. GitHub Repo Description & Topics
Crafted for discoverability:

**Description:**
> Instagram profile monitor operated via Telegram — tracks followers, bios, profile pictures & 10+ fields. Self-hosted, Docker-ready, deploys to Render in minutes.

**Topics:**
`instagram` `telegram-bot` `monitoring` `fastapi` `postgresql` `docker` `python` `self-hosted`

Set via: repo page → About gear icon ⚙️ → Edit repository details.

---

### 4. Bot Descriptions (BotFather)
Written for the Telegram bot's two profile fields:

**About** (120 chars, shown on bot profile):
> Real-time Instagram profile monitor. Get instant alerts when followers, bios, or profile pictures change.

**Description** (512 chars, shown on first open):
> The Watcher monitors public and private Instagram profiles and alerts you the moment anything changes.
> Track: followers · following · posts · bio · full name · profile picture · verification badge · external link · private/public status · and more.

Set via: BotFather → /mybots → Edit Bot → Edit Description / Edit About.

---

### 5. Image Prompts
Two prompts written for Gemini image generation:

- **Banner** (1280×640, 2:1) — dark cyberpunk-lite wide banner with eye icon, Instagram cards, Telegram alerts, cyan/purple palette
- **Logo** (1:1) — minimalist geometric eye icon with Instagram lens iris and Telegram paper plane pupil, dark #0d1117 background

---

## Commits

| Hash | Message |
|---|---|
| `5c70b40` | Rewrite README and update .env.example for professional presentation |