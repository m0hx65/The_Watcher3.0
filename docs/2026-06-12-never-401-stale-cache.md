# Fix: kill the recurring 401s — durable last-known-good cache + paced sweeps (2026-06-12)

## Symptoms

Even with every Instagram call routed through the Cloudflare Worker, sweeps
kept reporting random failures ("Sweep complete — 11 profiles checked.
2 failed: @taima.md, @whos.lisianna") and manual rechecks showed
"Check for @65xim failed: HTTP 401". Which accounts failed changed sweep to
sweep.

## Root cause (measured, not guessed)

Instagram's anonymous gate on datacenter IPs is **selective and flaky**:

- From a residential IP, `web_profile_info` returned 200 for every monitored
  account.
- Through the Worker at the same moment, `@instagram` returned 200 while all
  five small monitored accounts returned hard 401s — mega-accounts are served
  from any IP, ordinary accounts only while the colo's IP reputation is
  currently good, and that flips on and off per colo.

So retrying with rotated user agents (the old Worker did 8 attempts per call)
can never be sufficient — and worse, a sweep that bursts 11 accounts × ~2
calls at a blocked colo multiplies into 100+ blocked requests, keeping the IP
hot. The 401s were structural, not bad luck.

## Fixes

### 1. Worker: durable last-known-good cache (the actual "never 401" part)

`ig-proxy-worker` (sibling repo) now answers in this order:

1. in-isolate memory cache, < 2 min old (dedups the bot's own retries)
2. KV cache, < 2 min old
3. live Instagram, up to 6 attempts **rotating between www.instagram.com and
   i.instagram.com** — the two hosts are gated separately
4. in-isolate memory, < 45 min old
5. **KV, < 24 h old** ← survives isolate recycling, deploys, and colo moves
6. only then 401

Successful bodies are written to KV at most once per hour per key
(free tier: 1k writes/day; one sweep touches ~22 keys → ~530 writes/day).
Responses carry `x-proxy-cache: fresh|kv-fresh|miss|stale|kv-stale` for
debugging. A stale body is safe by construction: `detect_changes` ignores
None/absent transitions, so the bot just sees "no changes" until the block
lifts — no false alerts, no failures.

KV namespace `CACHE` (id `bc12e5cfefdc4de59d9f02ecc23fdfaa`) is bound in
`wrangler.toml`. The cache was seeded once from a residential IP (all 10
known usernames + their reel queries) so the fix took effect immediately;
from now on production's own successful fetches keep it warm.

Verified live: the five accounts that 401'd through the Worker all served
`200 x-proxy-cache: kv-stale` during an active block, both routes.

### 2. Bot: paced sweeps (`_staggered_check`)

`check_all` no longer fires every account at once — launches are spread
~2 s apart (`_SWEEP_STAGGER_SECONDS`), which stays far under Instagram's
burst threshold. 11 accounts ≈ 20 s of spread; irrelevant next to the
30-minute cadence.

### 3. Bot: second-pass retry after a cooldown

Accounts whose profile fetch ended in 401/403/429/timeout get one more
`_run_check` after the story phase plus a 30 s cooldown
(`_SWEEP_RETRY_COOLDOWN_SECONDS`), sequentially with 2–5 s gaps. Anonymous
throttle windows are short, so the retry usually lands — the sweep summary
stops reporting phantom failures.

### 4. Bot: one fewer Instagram call per account per sweep

`_check_stories_and_highlights` reused the reel query's highlight catalog
only via the 90 s client cache, which the now-paced sweep often outlives —
so the story phase was re-fetching data the profile check had just stored.
It now reads `highlights` straight from the snapshot's `reel_data`, falling
back to a live fetch only for pre-existing snapshots that predate the field.

## Residual risk (honest accounting)

A user-visible 401 now requires **all** of: live Instagram blocked for 6
rotated attempts × 2 bot attempts, again on the post-sweep retry, AND no
KV copy newer than 24 h (i.e. a brand-new account added during a block, or a
block lasting a full day). For monitored accounts in steady state that's
effectively never. If a multi-day colo block ever shows up, the systemic
answer remains a residential proxy via `settings.proxy`.

## UPDATE — same day: stale serving REMOVED (owner decision)

The owner's call: **"I would rather choose nothing + an error over old
data."** The Watcher is a monitoring tool — a 200 must mean live, current
Instagram data, never a stored copy. So:

- The worker's memory + KV cache layers were removed entirely; the KV
  namespace (`bc12e5cfefdc4de59d9f02ecc23fdfaa`) was deleted along with the
  seeded data. The worker now returns live JSON, a real 404, or an honest
  401 — nothing else. Deployed version `9dd66fc2-d6a6-4863-a398-db95e3456531`.
- The bot-side stale-marker plumbing (never committed) was dropped.

What REMAINS from this work, because it gets *real* data more often rather
than papering over failures:

- worker host rotation (www ↔ i.instagram.com, separately gated) + UA
  rotation with jittered backoff,
- paced sweep launches (`_SWEEP_STAGGER_SECONDS`),
- the post-sweep retry pass for rate-limited accounts,
- the story phase reusing the profile check's highlight catalog (one fewer
  Instagram call per account per sweep).

Consequence, by design: when Instagram blocks the colo for longer than a
sweep, the sweep summary reports those accounts as failed — truthfully. The
only path to "always live data AND never an error" is a residential proxy
(`PROXY_URL` / `settings.proxy`).
