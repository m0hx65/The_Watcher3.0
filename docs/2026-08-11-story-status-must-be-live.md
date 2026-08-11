# Fix: story status is live data or an honest "unavailable" (2026-08-11)

## Symptom

One account (`@opscn1`) "always glitched out". In a single sweep the chat showed:

```
@opscn1 — 🎬 HAS STORY
👁 Sweep complete — 14 profiles checked. 11 failed: …, @opscn1, …
@opscn1 has no active story right now (or the account is private).   ← Story button
```

and its card read `Last check: 2026-08-08 10:57:59 PM · HTTP 200` — three days
old, with no failure count, while the sweep listed it as failing right now.

## Root cause

Three paths turned one 401 into that contradiction. `@opscn1` is a tiny account
(28 followers, 0 posts) and those are exactly the ones Instagram's anonymous
datacenter gate 401s (see `2026-06-12-never-401-stale-cache.md`).

1. **`_handle_failure` skipped its bookkeeping for 401/404** — it returned
   before `mark_checked`, so `last_checked_at`, `last_status_code` and
   `consecutive_failures` all froze at the last SUCCESS. Days of failed checks
   still rendered as "Last check … HTTP 200", zero failures.

2. **The story phase reported status out of a stored snapshot.**
   `_check_stories_and_highlights` read `reel_data` from
   `get_latest_snapshot(successful_only=True)`. For a blocked account that
   snapshot is days old, so every sweep re-announced "🎬 HAS STORY" from a story
   that had long since expired. The sweep runs the story phase for failed and
   breaker-deferred accounts too (deliberately — saveinsta still works when
   Instagram blocks us), so this fired on exactly the accounts whose data was
   stalest. Same seeding bug on the account card: with graphql blocked and the
   stored value True, the live saveinsta oracle was skipped entirely.

3. The Story button and the card's live path were the only honest voices in the
   room, which is why they disagreed with the sweep.

Separately, the "just posted a story!" transition compared against
`get_previous_snapshot()`. Unchanged checks refresh the existing snapshot row
in place instead of inserting, so that "previous" row is from the last profile
*change* — possibly weeks back. The alert could fire every sweep of one story,
or never.

## Fix

- **Status comes from this check's reel query, passed down explicitly.**
  `_handle_success` returns `reel_data`; `check_all` / `check_username` hand it
  to `_check_stories_and_highlights(reel_data=…)`. No snapshot is ever read for
  status. When the caller has none (failed check), one live fetch is attempted;
  if that fails the sweep says so:

  ```
  @opscn1 — ⚠️ story status unavailable
  Instagram didn't answer the live check — not repeating the last known status.
  ```

  Logged as `story_status_unknown`, which the digest counts (a per-window count
  of "couldn't check this one" is the signal a blocked target used to hide).

- **Transition baseline moved to `app_settings` (`story_state:<account_id>`)**,
  written only from an OBSERVED status. A blocked sweep can no longer
  manufacture or swallow a transition, and one story yields one alert.

- **401/404 now record `mark_checked`** (card shows the real time, status and
  failure count). They still store no snapshot row and still send no
  per-account failure alert — they're flaky by nature and the sweep summary
  already names them.

- **Card**: live reel query → saveinsta oracle → `⚠️ unavailable`, never the
  stored value. When checks are failing, the "Latest snapshot" block is dated
  so its numbers can't read as current.

- **The sweep's retry pass moved ahead of the story phase.** It used to run
  last, so an account that recovered on retry had already had its story status
  announced from the failed attempt — the sweep would say "status unavailable"
  and then report the same account as checked. Now the retry lands first and
  the story phase sees its fresh reel data.

## Tests

`scripts/test_story_status_freshness.py` — a blocked check never reports a
stored status, a block doesn't move the baseline, transitions alert exactly
once, and 401s are recorded. Full suite: 29/29 green.

## Consequence, by design

An account Instagram is blocking now shows "status unavailable" instead of a
confident wrong answer. That is the same trade the owner already chose when the
worker's stale cache was deleted: an honest error over old data. The only path
to "always live AND never an error" remains a residential proxy
(`PROXY_URL` / `settings.proxy`).
