# 2026-09-05 — The username API went behind a login wall; checks now open with the numeric id

## What the owner saw

Three sweeps in a row, eight hours apart, looked exactly alike:

```
👁 Sweep started — 17 profiles queued.
@whos.lisianna — ⚠️ story status unavailable
@opscn1 — ⚠️ story status unavailable
@7_lilivv — ⚠️ story status unavailable
👁 Sweep stopped — Instagram is blocking every request right now.
🚫 5 checks in a row came back blocked … left 12 account(s) unchecked
✅ 17 profiles did get through before that.
```

Plus one target, `@reine_.saad`, sitting at eight consecutive failures after
its owner changed the username — with a stored Instagram ID the bot never
used.

## What was actually happening

Every door was measured from three vantage points the same afternoon:

| Route | From Render | Through the Worker | From a residential IP |
|---|---|---|---|
| `web_profile_info?username=` (www and i. hosts) | 401 | 401 after every retry, even for `@instagram` | **401** `"Please wait a few minutes…", require_login: true` on the first request |
| public page `instagram.com/<user>/` | **429, 0 bytes** | never routed | 200 with the payload, intermittently |
| graphql reel query `?user_id=<pk>` | (skipped by the guard) | **200 in ~1s** | 200 |
| mobile `users/<pk>/info/` | — | 200, but only `pk`, `username`, `profile_pic_url` | 200 |

So this was not the selective datacenter gate the earlier write-ups describe.
The **username-keyed profile API is login-walled for anonymous clients
everywhere** we could look, while the **numeric-id route stays open** — and
the Worker reaches it fine.

Three things in the bot turned that into total blindness:

1. **The check began with the dead door.** `fetch_profile(username)` was step
   one; the reel query by id only ran *after* a successful profile fetch. So
   the route that worked was never reached.
2. **"Gate down" was inferred from the username route alone**, then used to
   skip the reel query in the story phase — the one call that would have
   answered. Hence "story status unavailable" for accounts whose status was
   one working request away.
3. **Rename recovery waited for a 404.** The username route now returns 401,
   never 404, so it never fired. And when it *did* resolve a new name it
   re-fetched by that name and threw the rename away if the fetch failed.
   404s were also classed as "gate noise" and never announced — eight silent
   failures.

And one honest-reporting bug: `checked` counted breaker-skipped accounts, so
a sweep that stopped after five reported that seventeen "did get through".

## What changed

**Every check now opens with the numeric id** (`InstagramClient.probe_by_id`).
One Worker call answers with the current username, the avatar URL, story/live
status and the highlight catalog, and — unlike the old `fetch_reel_user` — the
HTTP status, so a 404 (the id no longer resolves) is not confused with a 401
(blocked). The story phase reuses the same answer; the reel question is asked
once per check.

- **A rename is found through the id and persisted immediately**
  (`MonitorService._apply_rename`), announced once, and the profile fetch that
  follows uses the *new* name. If that fetch is blocked, the rename stays.
  The snapshot diff drops its own "Username changed" entry so the API's later
  return cannot announce the same rename twice.
- **An id-only reading is a live, partial reading** (`source="id_probe"`): it
  writes username and picture, carries every other field forward from the
  last full reading, alerts only on what it saw, and is labelled on the
  account card ("Checked by Instagram ID only … from the last full reading on
  <date>") and in the sweep summary. Counts and bio are never guessed.
- **The throttle books two doors.** `SWEEP_BREAKER_THRESHOLD` refused username
  lookups in a row with none answering close *that door* for the rest of the
  sweep — the remaining accounts go by id only, and the retry rounds re-ask
  anything still blocked by id only (the id route is refused per colo too, so
  a paced re-ask from another colo usually lands) — while `gate_down` now
  means the id route answered nothing either. A check where either route answered is a
  success for pacing.
- **A username 404 is surfaced**, worded by what the id route said: the id
  still answering under the same name is a glitch (quiet); the id gone means
  deactivated or deleted (announced at once, two routes agree); the id route
  blocked means a rename could not be confirmed (announced on the second
  consecutive miss, then every 5th).
- **The summary counts honestly**: `checked` excludes deferred accounts,
  "did get through" counts answers, and deferred accounts are named as
  deferred — not listed among the failures.
- `/probe` gained an **ID route** line, so "which door is open" is one
  command away.

## What the owner sees now, during this outage

```
👁 Sweep complete — 17 profiles checked.
🪪 17 checked by Instagram ID only — username, picture and story status are
live; followers, bio and counts couldn't be read this time and were not guessed.
🚪 Instagram refused every username lookup (5 in a row), so the rest of the
sweep asked by ID only.
```

Story status lines come back for public accounts, a renamed target arrives as
`🔁 @old changed username → @new`, and the card dates the numbers it cannot
refresh.

## What is still not possible anonymously from a datacenter

Follower/following/post counts, bio, name and the privacy flag need either
the username API (login-walled) or the public page (429 from Render's shared
IP, intermittent even from residential). Measured the same evening: the page
is refused from Cloudflare's edge too. A `?page=<username>` route on the
Worker sends the full Chrome navigation header set; Instagram answers with a
redirect to `/accounts/login/?next=…&is_from_rle` and that page is a 429 with
an empty body — for every account, `@instagram` included. The header set is
what makes Instagram embed the payload (plain curl with those headers gets it
from a residential IP; with only a User-Agent it gets an empty shell), but
only once the source IP is let in, and neither datacenter egress is. The
route stays deployed so the question is one request away.

The free way around it shipped the same evening: **the owner's own PC as a
fourth door.** The owner's curl from home — through Proton VPN, no less —
got the page every time — and so does Python's standard library with Chrome's
navigation headers, which turned out to be what Instagram reads (the TLS
fingerprint is not). So `tools/home_fetcher` runs on an old Android phone in
Termux (or a PC) and **polls the bot** for pages to fetch, posting Instagram's
answer back: nothing dials into the home network, which sits behind
carrier-grade NAT (a 10.x WAN address) where an unrooted phone can run neither
a port forward nor a Tailscale Funnel. The bot asks it after its own page
request is refused, parses the same payload, and books the API door by what
the API itself said (`api_status`) so a page answer never re-opens it. Proton
VPN offers no standalone SOCKS5 proxy; a Tailscale exit node inside the Render
container was rejected for needing container surgery and coupling the whole
bot to the device being on. A phone that is off costs one quick "not
connected" and an id-only sweep. Until then the bot says what it knows and
dates what it doesn't — per the standing rule: an honest gap over a stale
number.
