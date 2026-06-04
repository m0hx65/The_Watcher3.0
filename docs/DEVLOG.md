# Dev Log — Instagram Fetch Hardening & Proxy Setup

## Problem: Burp gets 200, curl gets 429/401

Instagram inspects the TLS handshake (JA3/JA4 fingerprint) before reading any HTTP headers.
`curl`'s fingerprint is trivially identified and blocked. Burp proxies through a browser's TLS stack so the fingerprint looks legitimate.

The fix already in the codebase was `curl_cffi` with Chrome impersonation — it replays Chrome's exact TLS ClientHello at the socket level.

---

## Fix 1: Switch impersonation target from `chrome146` to `chrome120`

Tested all available Chrome targets against Instagram:

| Target | Result |
|---|---|
| chrome120 | ✅ 200 |
| chrome124 | ✅ 200 |
| chrome131 | ✅ 200 |
| chrome133a | ❌ 401 |
| chrome136 | ✅ 200 |
| chrome142 | ❌ 401 |
| chrome145 | ✅ 200 |
| chrome146 | ✅ 200 (intermittent) |

`chrome120` is the most stable. `chrome133a` and `chrome142` are fingerprint-blocked by Instagram.

**Commit:** `8031ea3`

---

## Fix 2: Retry 401s immediately instead of sleeping

Instagram sometimes returns a transient 401 that resolves within a few retries.
Changed behavior: on 401/403, retry immediately up to 5 times with `random.uniform(1.0, 3.0)` jitter between attempts — no session rotation, no backoff.

Previously all 5 retries fired at the same timestamp (burst), which Instagram blocked. Adding jitter spreads them out.

**Commits:** `8031ea3`, `7f41914`

---

## Fix 3: Cloudflare Worker proxy for Render's blocked datacenter IPs

Even with the correct TLS fingerprint, Render's Frankfurt datacenter IPs are aggressively flagged by Instagram. Local requests succeed; server requests get consistent 401s.

### Solution: Cloudflare Worker as a transparent proxy

- Free tier: 100,000 requests/day
- Cloudflare's edge IPs are never blocked by Instagram
- Worker retries 8 times with rotating user agents on each attempt

**Worker URL:** `https://ig-proxy.m-asaad2005-ma.workers.dev`

**Worker logic:**
- Accepts `?username=<x>`
- Forwards to `https://www.instagram.com/api/v1/users/web_profile_info/?username=<x>`
- Rotates across 6 user agents on each retry
- Returns the raw JSON response as-is

**Bot config:** `IG_PROXY_URL=https://ig-proxy.m-asaad2005-ma.workers.dev` in Render env vars.
When set, the bot hits the worker instead of Instagram directly. No `curl_cffi` needed for the proxy path since Cloudflare handles the fetch natively.

**Worker source:** `C:\Users\Games king\Desktop\projects\ig-proxy-worker\src\index.js`
**Deploy:** `wrangler deploy` from the `ig-proxy-worker` directory (not git-based).

**Commit:** `6fbb0d0`

---

## Fix 4: Damascus time (UTC+3) with 12-hour AM/PM format

All timestamps were displayed in UTC 24-hour format. Changed to Damascus local time with 12-hour clock.

**Change in `app/utils/formatting.py`:**
```python
DAMASCUS_TZ = timezone(timedelta(hours=3))

def fmt_timestamp(dt):
    dt = dt.astimezone(DAMASCUS_TZ)
    return dt.strftime("%Y-%m-%d %I:%M:%S %p")
```

All timestamps in the bot flow through `fmt_timestamp`, so one change covers everything: last check time, profile picture captured, next run, notifications.

**Commit:** `054fe78`

---

## Key files changed

| File | What changed |
|---|---|
| `app/monitor/instagram.py` | `chrome120` fingerprint, 5 retries, jitter, proxy routing |
| `app/utils/formatting.py` | Damascus timezone, 12-hour AM/PM |
| `app/monitor/service.py` | Use `fmt_timestamp` for hardcoded timestamp string |
| `app/config.py` | Added `IG_PROXY_URL`, `IG_SESSION_COOKIE` settings |
| `render.yaml` | Added `IG_PROXY_URL` and `IG_SESSION_COOKIE` env var entries |
