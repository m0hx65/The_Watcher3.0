# Sweeps reporting failures a manual recheck couldn't reproduce — 2026-08-11

Every sweep named 9–13 accounts as failed. Re-checking any of them by hand a
minute later worked. This is the record of chasing that, including the diagnosis
that was wrong, because the wrong one shaped two commits before the logs killed
it.

---

## 1. The wrong diagnosis: bursts

The first read was that the sweep fired too many requests at once and tripped
Instagram's rate limiter, while a manual recheck — one request, alone — slipped
under it. That produced adaptive pacing, a circuit breaker, and paced retry
rounds.

It was wrong, and one log line proves it:

```
15:54  service boots
15:56:00  Checking @vrcuvr        <- first Instagram request after boot
15:56:09  HTTP 401
```

The **first** request of the sweep, two minutes after a cold start, with no
burst and no accumulated rate, was blocked. All 14 accounts then failed at ~22s
spacing — comfortably paced — and a manual recheck of one of them succeeded
three minutes later. A rate limiter cannot behave like that.

**Lesson:** pacing was tuned for a mechanism that was never operating. The
evidence that would have falsified it (the very first request failing) was in
the first log and went unread.

---

## 2. The real mechanism: amplification

One call through the Cloudflare Worker is **8 upstream attempts** with rotating
UAs and hosts. `fetch_profile` then asked the Worker *twice* on a 401. So one
blocked account cost 16 blocked Instagram requests, and each took ~9 seconds —
which is exactly what the timestamps showed.

Per sweep, with 14 accounts blocked:

| Phase | Blocked upstream requests |
|---|---|
| Profile checks (14 × 2 × 8) | 224 |
| Story-phase reel fallback (13 × 8) | 104 |
| Retry rounds (13 × 16) | 208 |
| **Total** | **~540 in 10 minutes** |

The retry rounds — added to *fix* the problem — were a third of the blocked
traffic. Then the sweep ended, two minutes of genuine silence passed, and a
manual check went through.

**Fixes:** a sweep asks the Worker once per blocked check; the guard now
distinguishes a throttle (some accounts answering → pause and continue) from an
outage (nothing answering → stop immediately, skip the retry rounds and the
per-account reel fallback). A fully-blocked sweep dropped from ~540 blocked
requests to ~80, and from ten minutes to about one.

---

## 3. Why Cloudflare gets blocked at all

Worth writing down, because "route it through the edge and Instagram can't
block it" shaped the original proxy design.

- Worker `fetch()` egress leaves from Cloudflare's **published** IP ranges under
  one well-known ASN. Every IP-intelligence dataset labels it datacenter.
  Classifying it is a list lookup.
- Those IPs are **shared** across every Worker on the platform, so their
  reputation is everyone's aggregate traffic. Hence blocks that flip per colo
  and differ per account.
- The 8 attempts inside one call all leave from the **same colo with the same
  TLS fingerprint**. They rotate UA and host — neither of which is what the gate
  keys on. Separate calls have a chance at a different colo, which is why the
  on-demand path re-asks and the sweep does not.
- A Worker hop **loses the Chrome TLS fingerprint** that `curl_cffi` exists to
  provide: Cloudflare's runtime handshake arrives carrying a header claiming to
  be Chrome. The mismatch is a stronger signal than either fact alone.

---

## 4. The second door (2026-08-12)

> **Outcome first:** the door is open, but not the one first built. The `og:`
> meta parser shipped, was withdrawn a day later for reporting **677 following
> where Instagram showed 577**, and was replaced by a parser reading the page's
> *embedded payload* — which matched ground truth exactly. Sections 4b and 4c
> are that reversal, in order.

Instagram's plain profile page is a different endpoint with different gating.
It is fetched **directly**, not through the Worker, so it carries the real
Chrome fingerprint, and it sends no `x-ig-app-id`, so it reads as a page view
rather than a private-API call.

The mistake was in *what part of the page* to read. The obvious surface is the
Open Graph block:

```html
<meta property="og:description"
      content="1,234 Followers, 567 Following, 89 Posts - Name (@user)…">
```

That is what the first version parsed.

### Handling partial data honestly

A page reading knows less than the API. Three rules keep that from becoming
false alerts, and all three survived the rewrite:

1. **Unseen fields carry forward** from the previous snapshot instead of being
   written as `None`. Otherwise the card shows a real bio as newly empty, and a
   change made *during* the outage is swallowed when the API returns.
2. **`None` is not `""`.** The change detector treats an unknown text field as
   "no information". A genuinely cleared bio still arrives as `""` and still
   reports.
3. **A count Instagram omits is absent, never `0`.** `all_media_count` is
   `null` for private accounts, and a zero there reads as "they deleted every
   post".

---

## 4b. Why the og: version was withdrawn

Two symptoms appeared in the first full day, and one screenshot ended it.

**Phantom changes.** Accounts flip-flopped — `@8dvuu` went 103 → 124 → 103 →
124 across sweeps — because the API and the page disagreed and the sweep
alternated between them. Each flip "detected" a change. The giveaway was
`Full name changed لِ → ‎لِ‎`: two visibly identical strings differing only by
the bidi marks Instagram adds to RTL names on some surfaces.

**A private account got a story check.** `bool(parsed.get("is_private"))` turned
the page's *absent* privacy flag into `False`, so a private target was reported
public, entered the story phase, and got a "⭕ NO STORY" line for stories the
bot cannot and must not see. `bool(None)` is `False`, and "I could not see it"
is not "it is false".

Source-scoped diffing fixed the flip-flop, and the privacy flag now falls back
to the last known value. But then the owner checked a profile by hand:
Instagram said **577 following**, the bot's card said **677**. Followers and
posts matched. That settled it — nothing distinguished a good number from a bad
one at read time, so the surface was removed from the data path entirely.

**Lesson:** "live, from a different endpoint" is not the same as "correct". A
second source needs its values verified against ground truth before it is
allowed to back an alert — being fresh and being right are independent
properties, and only one of them was ever checked.

---

## 4c. The same page, the right data

The withdrawal was right; the diagnosis was incomplete. A full captured
response for `/65xim`, logged out, showed the **same page carrying two
different following numbers**:

| Where | followers | following | posts |
|---|---|---|---|
| `og:description` | 118 | **677** ← stale | 0 |
| embedded Relay payload | 118 | **577** | `null` |
| rendered HTML | 118 | 577 | 0 |
| Instagram app | 118 | **577** | 0 |

The parser was never buggy. The `og:` block is a **stale cache**, and nothing
in it marks it as stale — which is exactly why no read-time test could have
saved it. Meanwhile the page inlines the payload it renders from:

```json
"xig_user_by_username":{"pk":"7880052534","username":"65xim","is_private":true,
  "biography":"…","full_name":"Mohamad","is_verified":false,
  "follower_count":118,"following_count":577,"all_media_count":null}
```

Same shape as the API's response, matching ground truth, and carrying
`is_private` — which fixes the private-account bug at its source rather than
papering over it with a carry-forward. Note `pk` is the numeric id the rest of
the bot keys on; the sibling `id` is a different app-scoped identifier and
storing it would break the reel query.

So the fallback is back, reading the payload. What carried over unchanged:
source-scoped diffing, bidi stripping, carry-forward for unseen fields, and the
`/probe` split that measures each door alone.

**What is still unverified.** The payload matched ground truth for *one*
account, on numbers checked by hand — the check that was missing the first
time, but a sample of one. And whether the page is reachable at all from
Render's datacenter IP has never been measured: every earlier probe either died
or ran before the fallback existed. If it answers with a login wall the parse
returns `None` and the original error stands. `/probe` on a few mixed
public/private accounts is the next measurement.

## 5. The incident: a regex that took the service down

The first live `/probe` reached the public page and the instance died — health
check timed out after 5 seconds, Render replaced it, the probe froze forever.

The meta-tag parser used a lazy `(.*?)` under `re.DOTALL` spanning two
attributes. For every `<meta>` that did **not** match, the group expanded across
the entire remaining document before failing. Measured on the old pattern:

| Decoy tags | Page size | Time |
|---|---|---|
| 20 | 205 KB | 0.05s |
| 100 | 223 KB | 0.31s |
| 200 | 246 KB | 0.75s |

Cost grows with tags × page size, and a real profile page is ~400 tags over
several MB — tens of seconds of solid CPU. It ran on the event loop, so
`/health` stopped answering along with everything else.

**Fixes:** the scan was bounded and moved off the event loop. The payload
parser that replaced it (§4c) uses no regex at all — it finds one key, walks
the object once with a brace matcher that tracks string and escape state, and
stops at the matching close brace, a 4 MB scan window or a 200 KB object,
whichever comes first. The regression suite parses a 3 MB+ page under a second
and asserts that unbalanced braces terminate instead of scanning forever — a
budget the old implementation could not meet.

**Lesson:** a fallback path only runs when something is already broken, which is
the worst possible time to discover it is more dangerous than the failure it
handles. It needs the same stress testing as the primary path.

---

## 6. Alert volume

One story used to produce, per sweep it survived: a status line, a "just posted
a story!" alert, a "posted N new story items" header, and the media itself. The
status is now announced only when it **changes**, and never on top of the media
that already announced it — the media message is the notification. Every status
is still logged, so the digest is unaffected. `STORY_STATUS_HEARTBEAT=true`
restores the old behavior.

The sweep summary also stopped listing every account when the gate is shut.
Naming 13 accounts implies 13 separate problems; there was one, and it was not
theirs.

---

## 7. Diagnostics

`/probe <username>` tests each source separately and reports which answer — the
API, the public page (with HTTP status and byte count), and the media
downloader. Every outcome is logged at INFO, because this path only runs when
something is already blocked and the log is where it gets diagnosed.

The distinction it exists to draw:

- `HTTP 302, 0 bytes` → redirected to a login wall; that door is shut too
- `served 523000 bytes but no profile payload` → reachable, parser needs adjusting
- `followers: 1,234` → working

Startup and `/status` now also state the route in words (Worker hostname, or
`DIRECT` with a warning). Previously "is it even using the proxy?" had to be
inferred from retry counts in the logs, and a misconfigured `IG_PROXY_URL` was
indistinguishable from Instagram blocking us.
