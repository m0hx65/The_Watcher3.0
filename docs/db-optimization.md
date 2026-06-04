# Database Optimization

## Problem

The bot was inserting a new snapshot into `account_snapshots` on **every single check**, regardless of whether anything changed. With 6 accounts on an 8h interval that's ~21 rows/day of pure noise. Scale that up and the 1 GB free-tier storage fills up fast.

There was also no cleanup — the DB grew forever.

---

## What Was Changed

### 1. No-duplicate snapshots (`app/monitor/service.py`)

**Before:** insert snapshot → then detect changes.
**After:** detect changes → only insert if something actually changed.

```
unchanged check  →  0 new rows  (was: 1)
changed check    →  1 new row   (same as before)
first-ever check →  1 new row   (baseline, always saved)
```

Failure snapshots follow the same rule: only saved when **transitioning from success**. Repeated failures in a streak produce no new rows (notifications still fire per existing throttle logic).

### 2. Daily auto-cleanup (`app/workers/scheduler.py` + `app/database/crud.py`)

A second APScheduler job runs every day at **03:00 UTC** and does three things in order:

| Step | What it does | Default threshold |
|------|-------------|-------------------|
| NULL `raw_response` | Clears the heavy JSONB blob on old rows without deleting them | 7 days |
| Delete old snapshots | Removes aged-out snapshot rows, always keeping the **most recent per account** | 30 days |
| Delete old notifications | Removes aged-out notification log rows | 90 days |

`profile_media_hashes` is never purged — it's a sparse dedup table.

### 3. Configurable retention (`app/config.py`)

All thresholds are env vars with safe defaults. Set any to `0` to disable that step.

| Env var | Default | Controls |
|---------|---------|---------|
| `SNAPSHOT_RETENTION_DAYS` | `30` | How long to keep snapshot rows |
| `NOTIFICATION_RETENTION_DAYS` | `90` | How long to keep notification log rows |
| `RAW_RESPONSE_RETENTION_DAYS` | `7` | When to NULL out the `raw_response` JSONB column |

---

## Files Changed

| File | Change |
|------|--------|
| `app/config.py` | Added 3 retention env vars |
| `app/database/crud.py` | Added `purge_old_data()` function |
| `app/monitor/service.py` | Gated snapshot inserts on actual changes |
| `app/workers/scheduler.py` | Added daily `watcher-cleanup` cron job |

---

## Storage Impact

At current usage (6 accounts, 8h interval, nothing changing):

| | Before | After |
|--|--------|-------|
| New rows/day (no changes) | ~21 | **0** |
| DB growth over 1 year (no changes) | ~7,600 rows | **0** |
| Existing data older than 30d | kept forever | auto-purged daily |
| `raw_response` JSONB after 7d | kept (10–50 KB/row) | NULLed |

---

## Commit

[`6501104`](https://github.com/m0hx65/The_Watcher3.0/commit/6501104) — pushed to `main`.
