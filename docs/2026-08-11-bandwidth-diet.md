# Bandwidth diet: stop paying for answers we already have (2026-08-11)

## Starting point

Render reported **990 MB for the month, 989 MB of it "Service-Initiated"** —
i.e. our own outbound fetches, not traffic to the bot. Every monitored account
was costing four kinds of request on every 30-minute sweep:

| per account per sweep | payload | compressible? |
|---|---|---|
| `web_profile_info` JSON (via the worker) | 50–200 KB | yes (~10×) |
| graphql reel query | a few KB | yes |
| **avatar image download** | **50 KB – 300 KB** | **no — JPEG** |
| saveinsta story listing + one listing per highlight reel | ~10–30 KB each | yes |

Text compresses on the wire; JPEG does not. The avatar was the bill.

## What was actually wasteful

**The avatar was re-downloaded every single sweep** — at full resolution, since
`hash_url` strips the CDN size constraint first — purely to recompute a
perceptual hash that had not changed. 14 accounts × 48 sweeps/day × a
non-compressible image is the whole invoice. Worse, each download also wrote a
new file to `data/media/` and a new `ProfileMediaHash` row, because the CDN
re-encodes the image on every signed URL so its sha256 differs each time.

**The story media listing ran even when Instagram had just said there is no
story**, and the sweep had already printed "⭕ NO STORY" from that same flag.

**Every highlight reel was re-listed every sweep**, to re-discover items that
only change when the owner adds a story to a reel — a story the bot had already
detected and delivered live minutes earlier.

## The cuts

### 1. Don't re-download an avatar we've already fingerprinted (the big one)

An Instagram avatar URL's numeric asset id changes if and only if a new picture
is uploaded (`media_hasher.pic_asset_id`), and `_pic_changed` cannot report a
change without perceptual evidence. So when the current URL's asset id equals
the fingerprinted baseline's, the download is provably outcome-neutral: skip it.

This is not a heuristic — it removes work whose result is already determined.
Escape hatches all still download: no stored fingerprint, a stored hash in a
legacy format, an unparseable id on either side, a first sighting, or a
different id (a real upload). The confirmation re-download still runs on a real
change.

Side effects, both good: `data/media/` stops accumulating one avatar copy per
account per sweep, and the card's "Profile picture captured" now shows when the
current avatar was actually captured instead of always saying "just now".

### 2. Skip the story listing when the live flag says there's no story

`fetch_stories` is skipped only when THIS check's reel query returned
`has_public_story = False`. If the reel query didn't answer, saveinsta is the
story oracle and is still asked — that path is untouched.

### 3. Re-list highlight reels on a cadence, not every sweep

`HIGHLIGHT_SCAN_INTERVAL` (default 6 h, `0` restores per-sweep listing). A reel
that is NEW to the catalog is always listed immediately, whatever the setting;
existing reels are re-listed once per interval. Muting/unmuting is unaffected —
unmuting already baselines the reel's items at that moment.

This is the one cut that trades a little latency for bandwidth: an item added to
an existing highlight is now noticed within the interval rather than within 30
minutes. Stories themselves are still checked every sweep.

### 4. Fetch the saveinsta token page once, not once per parallel request

`_get_tokens` had no lock, so a gather over an account's highlight reels could
fetch the heaviest response in the flow (a full HTML page) once per reel. It is
now refreshed under a lock, with the TTL raised from 5 to 25 minutes — still
self-healing, since a stale token makes ajaxSearch answer non-200, which clears
the cache.

## Expected effect

Avatar traffic drops to roughly zero between actual picture changes; story
listings drop to the sweeps where a story exists; highlight listings drop ~12×
at the default interval. The remaining steady-state cost per account per sweep
is the profile JSON plus the reel query — both gzip'd text.

## Not done here

The `web_profile_info` response is the largest remaining item, and the biggest
further win would be trimming it **in the worker** (return only the ~15 fields
the bot parses instead of the full 50–200 KB document). That lives in the
sibling `ig-proxy-worker` repo, not this one.

## Tests

`scripts/test_bandwidth_efficiency.py` — proves each skip removes only
determined work: a rotated URL for the same upload downloads zero times across
five sweeps while a new asset id still downloads, still detects the change, and
still delivers the photo; the story listing follows the live flag and is still
asked when the status is unknown; the highlight cadence lists new reels
immediately and re-lists everything once the interval elapses. 30/30 suites
pass.
