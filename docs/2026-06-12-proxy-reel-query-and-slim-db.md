# Fix: /add-by-ID & highlights via proxy + slim snapshots (2026-06-12)

## Symptoms

1. `/add 62790675311` → "Could not resolve that Instagram ID to a username."
2. ✨ Highlights showed "@opscn1 has no highlights" even though the account has
   one ("حسوني moshi") and the raw graphql request returned it fine from a
   browser/Burp.

## Root cause

The graphql reel query (`query_id=9957820854288654&user_id=…`) powers three
things: username-by-id resolution (`/add <id>`), the highlight catalog, and
story/live status. Profile fetches were already routed through the Cloudflare
Worker proxy (`IG_PROXY_URL`) because Render's Frankfurt datacenter IPs are
hard-401'd by Instagram — **but the reel query still went direct to
instagram.com from Render**, got 401'd every time, tripped the client's
circuit breaker, and returned nothing. The request shape was never the
problem (verified: the bot's exact request returns 200 with the highlight
from a residential IP).

## Fixes

### 1. Worker proxies all three Instagram endpoints

`ig-proxy-worker/src/index.js` (sibling repo, deployed with `wrangler deploy`)
now accepts:

| Param | Upstream | Used for |
|---|---|---|
| `?username=` | web_profile_info | profile fields (existing) |
| `?user_id=` | graphql reel query | `/add <id>`, highlights, story/live |
| `?hd_user_id=` | i.instagram.com users/<id>/info | HD avatar (session-only) |

A 200 with a non-JSON body (login-wall HTML) is treated as blocked and
retried. Deployed version: `30692d84-efb2-478d-8873-66cf8674c4d2`.

### 2. Bot routes the reel query through the proxy

`app/monitor/instagram.py`:
- `fetch_reel_user()` → proxy first (`?user_id=`), direct fallback. Proxy
  200/404 answers are authoritative; 400 (old worker build) / 401 / network
  errors fall back to the direct request, so local dev without the worker
  still works.
- 90-second in-memory TTL cache for reel results — one sweep asks for the
  same user's reel data up to 3 times (profile check, story status, highlight
  catalog); only the first hits the network now.
- `fetch_hd_pic_url()` → proxy when no session cookie is configured.
- `fetch_profile()` 401/403 retries capped at 2 when proxied (each worker
  call is already 8 upstream attempts — 5×8=40 blocked requests was a
  401-amplifier, not a fix).
- `_handle_success()` skips the mobile HD-pic call entirely in the anonymous
  setup — `hd_profile_pic_url_info` only ever exists for logged-in sessions,
  so that was one guaranteed-wasted Instagram request per account per sweep.

### 3. Slim snapshots — the 0.5 GB Neon tier effectively never fills

`app/monitor/service.py` now persists only what later reads consume:

```json
{"data": {"user": {"id": "40427049386"}},
 "reel_data": {"has_public_story": true, "is_live": false, "highlights": {...}}}
```

instead of the full web_profile_info payload (50–200 KB → a few hundred
bytes per snapshot, ~99% smaller). Consumers (404 recovery, ID backfill,
story status, account card) only ever read `data.user.id` and `reel_data`.

This also fixes a hidden regression: the unchanged-sweep branch used to
refresh the latest row with the FULL payload **without** `reel_data`,
wiping the stored story/highlight state on every quiet sweep.

Existing fat rows are aged out by the daily cleanup job
(`RAW_RESPONSE_RETENTION_DAYS=7` nulls old blobs at 03:00 UTC).

`app/database/models.py`: JSONB columns became
`JSONB().with_variant(JSON, "sqlite")` so the service tests can run on
sqlite. No change on Postgres.

## Tests

- `scripts/test_instagram_request_shape.py` — extended: proxy routing,
  direct fallback on proxy 400, authoritative proxy 404, TTL cache.
- `scripts/test_slim_snapshots.py` — NEW: sqlite service test asserting the
  slim form (id + reel_data kept, heavy payload dropped, <2 KB) and the
  unchanged-sweep reel_data preservation.
- `scripts/test_proxy_live.py` — NEW: live end-to-end with the real worker:
  `/add 62790675311` → `@__ralanee__`, opscn1 highlights → 1 reel, cache,
  profile fetch.
- `scripts/probe_reel_query.py` — NEW: request-shape probe used to verify
  the root cause.
- All pre-existing offline scripts still pass (request shape, bulk add,
  callback cleanup, notification retry, download all, interval persist,
  migrate db) and `scripts/test_stories.py opscn1` passes live (catalog → 41
  highlight media items via saveinsta → downloads OK).
