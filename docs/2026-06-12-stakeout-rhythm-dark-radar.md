# Feature drop: stakeout, activity rhythm, went-dark radar (2026-06-12)

Three new "intelligence" features plus a how-to for Telegram forum topics.

## 1. Stakeout mode — `/stakeout @user [duration]`, 🎯 button

Temporarily watch one target on a tight loop, then auto-revert to the normal
sweep schedule. Built on the existing APScheduler instance as a per-account
interval job (`stakeout:<account_id>`).

**No-401 design (the explicit requirement):**
- Every tick calls the same `check_username` — which already routes all
  Instagram traffic through the Cloudflare edge proxy (IPs Instagram doesn't
  block) and reuses the 90-second reel cache.
- The interval has a hard **floor of `STAKEOUT_MIN_INTERVAL` (120s)**, set just
  above the 90s cache so every tick gets fresh data but volume stays low — at
  the 180s default that's ~20 checks/hour for one account, trivial against the
  worker's 100k/day budget and gentle on Instagram.
- `max_instances=1` + `coalesce=True`, so a slow tick never stacks.

**Lifecycle:** `WatcherScheduler.start_stakeout / stop_stakeout /
active_stakeouts / stakeout_for`. Duration capped at `STAKEOUT_MAX_DURATION`
(6h). State is persisted to `app_settings` (`active_stakeouts` JSON) and
**restored on boot**, so a Render restart doesn't silently drop a stakeout. The
tick self-terminates and notifies when the window ends. Starting one also kicks
off an immediate first check so the user sees current state without waiting.

Config: `STAKEOUT_DEFAULT_INTERVAL=180`, `STAKEOUT_MIN_INTERVAL=120`,
`STAKEOUT_DEFAULT_DURATION=3600`, `STAKEOUT_MAX_DURATION=21600`.

## 2. Activity rhythm — `/rhythm @user`, 📊 button

An hour-of-day and day-of-week histogram of when a target is active, built from
`seen_stories.seen_at` (stories, posts, highlights the bot has delivered).
`seen_at` is within one sweep of the real post, so it's a faithful proxy;
`taken_at` from the anonymous source is usually 0 and isn't used. Bucketing is
in Damascus local time to match the rest of the bot.

New code is a pure module — `app/monitor/analytics.py` (`compute_rhythm`,
`render_rhythm`) — fed by `crud.activity_timestamps / first_activity_at /
last_activity_at`. Pure functions = trivially unit-tested.

## 3. Went-dark radar — sweep alerts + `/darkradar`

At the end of every sweep, `MonitorService._check_dark_radar` flags any active
account whose last delivered story/post/reel is older than `DARK_RADAR_DAYS`
(default 3; `0` disables). One alert when it goes dark, one when it returns —
state held in an `app_settings` flag per account (`dark_state:<id>`) so each
spell is announced exactly once. Accounts with no activity on record are skipped
(no baseline to judge from). `/darkradar` (and the 🌑 button in Status) lists
every target ranked by how long it's been quiet.

## 4. Telegram forum topics

Documented in [telegram-forum-topics.md](telegram-forum-topics.md): how to
enable Topics on a group, add the bot, point `TELEGRAM_CHAT_ID` at it, find a
topic's `message_thread_id`, and exactly where per-account topic routing slots
into the dispatcher if wanted later. Left out of the default build so the bot
keeps working in a plain 1:1 chat with zero config.

## Misc

- `app/database/models.py`: JSONB columns already use a sqlite JSON variant
  (added in the prior drop), letting the new service/scheduler tests run on
  sqlite.
- `crud.delete_setting` added for clearing per-account/stakeout state keys.

## Tests

- `scripts/test_rhythm.py` — bucketing, timezone correctness, render.
- `scripts/test_dark_radar.py` — going dark fires once, no duplicate, comeback
  fires once, report shape.
- `scripts/test_stakeout.py` — interval floor, duration cap, persistence,
  restart restore (future kept, expired dropped), tick runs a check, expired
  tick stops + notifies.
- Full pre-existing suite still passes (request shape incl. proxy routing,
  slim snapshots, bulk add, callback cleanup, notification retry, download all,
  interval parse/persist, migrate db).
