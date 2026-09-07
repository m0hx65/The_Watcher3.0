# PROMPT — Extend The Watcher to multiple platforms

> Copy everything below the line into a fresh Claude Code / agent session **run
> from the repo root** (`the_watcher_V3.0`). It is self-contained: what The
> Watcher is, the non-negotiable rules, the exact code contracts to satisfy, a
> phased plan, and how "done" is judged. It forces a **feasibility spike before
> any platform is promised** — because "make no mistake" means *not* claiming a
> capability that can't be delivered anonymously.

---

## ROLE

You are extending **The Watcher** — a login-free Instagram monitor that pushes
profile changes and media to Telegram — so it also watches a **curated, scoped**
set of other platforms. **Instagram remains the primary product and its behavior
must not change.** The other platforms are secondary and each has a *deliberately
limited* scope (below). Match Instagram's *quality* within each platform's scope;
do not try to make every platform do everything.

Work in small, reviewable commits on a feature branch. Do **not** push unless
explicitly told (Rule 5). Prove each claim with a test or a live probe script
under `scripts/`, exactly as the Instagram path is proven.

---

## AUTHORITATIVE SCOPE TABLE (do not widen or narrow without the user)

| Platform | Scope | Notes |
|---|---|---|
| **Instagram** | **FULL — primary, unchanged** | Profile fields, posts, reels, stories, highlights, avatar, live/story status, dark radar, stakeout, rhythm, bulk download. **This is the main goal of the tool. Do not regress or slow it.** |
| **TikTok** | **FULL — Instagram-quality parity** | Profile fields + new videos + media delivery + avatar, all login-free. The one platform besides IG that can realistically hit full parity. Expect strong anti-bot → needs edge proxying (Rule 3). |
| **X / Twitter** | **Profile picture + account status only** | Track avatar changes (IG-quality, below) **and** the public/protected (a.k.a. "private") flag. No posts, no media, no timeline. Anonymous X access is fragile — keep the surface tiny and live-only. |
| **SoundCloud** | **Profile picture only** | Avatar-change tracking only (IG-quality). No tracks, no reposts, no counts. |
| **Facebook** | **Profile picture only, PUBLIC accounts only** | Avatar-change tracking for public profiles/Pages. If the account isn't publicly readable anonymously, report it as unmonitorable — never log in to reach it. |
| ~~Tumblr~~ | **REMOVED** | Do not implement. |
| ~~Snapchat~~ | **REMOVED** | Ordinary accounts are private; dropped by the user. Do not implement. |
| ~~WhatsApp~~ | **REMOVED** | A user's profile photo has **no login-free public source** (viewing requires being a logged-in contact), which breaks Rule 1. Dropped by the user. Do not implement — not even "just the pic." |

**"Same quality as Instagram" = the profile-picture tracking on every pic-scoped
platform (X, SoundCloud, Facebook) must reuse Instagram's exact avatar pipeline**
(perceptual hash + second-download confirmation + highest-res anonymous URL). See
"Profile-pic parity" below. It does **not** mean giving those platforms IG's full
feature set.

---

## THE FIVE HARD RULES (violating any one is a failed task)

1. **100% anonymous — NO login, NO cookie, NO session, on ANY platform, always.**
   The product promise is "nothing that can get banned." `IG_SESSION_COOKIE`
   exists but is unused on purpose; add no login/cookie/session path for any new
   platform. If a platform's data is only reachable while authenticated **as a
   user**, that data is **out of scope** — drop it, do not build a login path.
   *(See `memory/instaloader-posts-reels-tradeoff.md`.)*

2. **Errors over stale data — never serve cached/old data as if it were
   current.** No "last known good" cache presented as live. An IP/colo block is
   an honest failure, not a silent stale read. Only *live-data* mitigations are
   allowed: host/UA rotation, pacing, retries, edge/residential proxying.
   *(See `memory/errors-over-stale-data.md`.)*

3. **Every call a datacenter IP would be blocked on must be routable through an
   edge proxy.** Instagram hard-401s Render's datacenter IP, so all IG calls go
   through a Cloudflare Worker (`IG_PROXY_URL`; worker source at
   `C:\Users\Games king\Desktop\projects\ig-proxy-worker\`, deploy with
   `npx wrangler deploy`). Any new platform that blocks datacenter IPs (TikTok
   will; X/FB may) needs the **same treatment**: add its route to the worker (or
   a sibling worker) *first*, then route the bot through it with a direct-request
   fallback. *(See `memory/ig-proxy-worker.md`.)*

4. **Pause must preserve target data.** Pausing may only flip `active=False`.
   Never delete rows or null the resolved platform id on pause.
   *(See `memory/pause-must-preserve-target-data.md`.)*

5. **Push only with the `m0hx65` credential**, and only when the user asks. The
   origin URL pins it. DB is Neon free tier; use `scripts/migrate_db.py` +
   `app/db_url.py::normalize_db_url` for migrations, and confirm before running
   one against live Neon. *(See `memory/push-only-with-m0hx65.md`,
   `memory/db-on-neon-free-tier.md`.)*

---

## WHAT ALREADY EXISTS (the Instagram path is your reference implementation)

Pipeline: **fetch → hash → diff → persist → notify**, fanned out over a jittered
schedule. Read these before writing anything:

| File | Role you must generalize |
|---|---|
| `app/monitor/instagram.py` | `InstagramClient.fetch_profile(username) -> ProfileFetchResult` (normalized profile dict) + reel/story-status + hd-avatar. **The "profile provider" template.** |
| `app/monitor/stories.py` | `StoriesClient` + `StoryItem`: the login-free **media downloader**. **The "media provider" template** (only TikTok needs a full media provider). |
| `app/monitor/media_hasher.py` | `MediaHasher` — perceptual (dHash/aHash) avatar fingerprint. **Platform-agnostic already; every pic-scoped platform reuses it verbatim.** |
| `app/monitor/service.py` | `MonitorService`: the orchestrator — diff, snapshot persistence, the **profile-pic confirmation pass** (`_handle_success`), story/highlight phase, dark-radar, forum topics, `/kill`. Instagram-coupled today; must learn `platform`. |
| `app/monitor/change_detector.py` | `detect_changes()` → `ChangeSet`. `pic_fingerprints_differ()` is the avatar diff. |
| `app/database/models.py` | Instagram-shaped tables; need a `platform` dimension. |
| `app/bot/handlers.py` / `keyboards.py` / `notifications.py` | Telegram UI + dispatch. |
| `app/workers/scheduler.py` | APScheduler sweep worker. |
| `app/config.py` | Pydantic settings; add per-platform toggles + proxy URLs. |

Normalized profile fields the diff understands: `username / full_name /
biography / followers_count / following_count / posts_count / reels_count /
story_count / is_private / is_verified / is_business / profile_pic_url /
external_url / instagram_id`. Pic-scoped platforms only populate
`profile_pic_url` (+ `is_private` for X); leave the rest null.

### Profile-pic parity (the exact flow every pic-scoped platform must reuse)

Instagram's avatar tracking (in `service.py::_handle_success`) is the quality bar:

1. Resolve the **highest-resolution avatar URL reachable anonymously** for that
   platform.
2. `MediaHasher.hash_url(url, handle)` → perceptual `phash` (+ sha256 + disk
   archive). **Diff on `phash`, never sha256** — CDNs re-encode the same image
   per signed URL, so sha256 false-positives every sweep.
3. **Confirmation pass:** a tentative change (fresh `phash` differs from the
   stored baseline) must be confirmed by a **second independent download** whose
   fingerprint (a) also differs from the baseline and (b) agrees with the first.
   Otherwise suppress this sweep; a real change re-confirms next sweep. This is
   what keeps it from crying wolf.
4. Send the new avatar as a **document** (full quality) with old/new
   fingerprints in the caption.

Do not reinvent this per platform — route each pic-scoped provider's avatar URL
into this same code path.

---

## STEP 0 — FEASIBILITY SPIKE (do this FIRST, gate everything on it)

For each **in-scope** platform (TikTok, X, SoundCloud, Facebook) write a
throwaway probe `scripts/probe_<platform>.py` that answers, anonymously and from
a datacenter-like vantage where possible:

- **TikTok:** Can you anonymously fetch a public profile's fields + video list +
  a downloadable video URL + avatar? Which host/endpoint? Datacenter-blocked?
  Anti-bot/JS challenge?
- **X:** Can you anonymously fetch a public account's **avatar URL** and its
  **protected/public flag**? Via what (syndication endpoint, oEmbed, public
  profile HTML, a login-free third-party)? How stable is it?
- **SoundCloud / Facebook:** Can you anonymously fetch the **avatar URL** for a
  public user/Page? (Facebook: confirm the account is publicly readable without
  login; if not, it's unmonitorable — that's fine and expected.)

Write findings to `docs/platform-capability-matrix.md`, one row per platform:
`avatar URL source | (TikTok: fields+video+media) | (X: status flag) |
datacenter-blocked? | anti-bot? | verdict`. Verdict ∈ **READY / READY-VIA-PROXY /
PARTIAL (list gaps) / NOT FEASIBLE ANONYMOUSLY**.

**Report the matrix to the user and stop for confirmation before Step 2** if any
in-scope platform comes back worse than its intended scope (e.g. TikTok can't do
media anonymously, or X's status flag isn't anonymously readable). An honest
downgrade beats a faked capability — that is the project's whole ethos.

---

## STEP 1 — PROVIDER ABSTRACTION (refactor, zero behavior change)

1. `app/monitor/providers/base.py`:
   - `Platform` enum: `INSTAGRAM`, `TIKTOK`, `X`, `SOUNDCLOUD`, `FACEBOOK`.
   - `Capability` enum: `PROFILE_FIELDS`, `AVATAR`, `PRIVACY_STATUS`, `POSTS`,
     `MEDIA`, `STORIES`, `HIGHLIGHTS`, `LIVE_STATUS`, `DARK_RADAR`, `STAKEOUT`,
     `RHYTHM`.
   - `PLATFORM_CAPABILITIES: dict[Platform, set[Capability]]` — the single source
     of truth the orchestrator and the bot UI consult so a platform only ever
     does/offers what it supports. Seed it from the scope table:
     - Instagram → everything.
     - TikTok → PROFILE_FIELDS, AVATAR, PRIVACY_STATUS, POSTS, MEDIA, DARK_RADAR,
       STAKEOUT, RHYTHM (map "posts" to videos).
     - X → AVATAR, PRIVACY_STATUS.
     - SoundCloud → AVATAR.
     - Facebook → AVATAR (public only).
   - `ProfileProvider` Protocol: `async fetch_profile(handle) -> ProfileFetchResult`,
     `async resolve_id(handle) -> str | None`.
   - `MediaProvider` Protocol (only TikTok needs a real one):
     `async fetch_media(handle, kind) -> list[MediaItem]`,
     `async download(item, handle) -> Path | None`. Generalize `StoryItem` into a
     platform-neutral `MediaItem` (keep `StoryItem` as an alias so IG code is
     untouched).
2. Make `InstagramClient` / `StoriesClient` implement these protocols
   (mechanical — they already match). Add `get_profile_provider(platform)` /
   `get_media_provider(platform)` factories.
3. `MonitorService` takes providers **by platform** and **capability-gates every
   phase**: the story/highlight phase, post/reel phase, live status, dark-radar
   "activity" definition, and each on-demand command only run when the account's
   platform has the capability. A pic-only platform runs **only** the avatar
   confirmation flow.
4. **Acceptance for Step 1: every existing Instagram `scripts/test_*.py` passes
   unchanged and a live IG add→sweep→notify still works.** Ship as its own
   no-op-for-users commit.

---

## STEP 2 — DATA MODEL + MIGRATION

1. Add `platform` (String, not null, default `"instagram"`) to
   `MonitoredAccount`; change uniqueness to **`(platform, username)`**.
2. Generalize the id: keep `instagram_id` working but access it via a
   `platform_user_id` accessor (new nullable column + lossless backfill, or
   repurpose). Every existing row becomes `platform="instagram"` with its id
   intact.
3. `AccountSnapshot`: common columns stay; pic-only platforms populate just
   `profile_pic_url`/`profile_pic_hash` (+ `is_private` for X). Keep snapshots
   **featherweight (~300 bytes)** — never store raw platform payloads.
4. Migration = idempotent `scripts/` script using the `migrate_db.py` pattern +
   `normalize_db_url`; runs clean and re-runnably on Neon; **test on a scratch
   copy first**; confirm with the user before touching live Neon (Rule 5).
5. Verify pause/resume, seen-item dedup, and highlight tables are platform-scoped
   via the account FK; Rule 4 must still hold per platform.

---

## STEP 3 — IMPLEMENT PROVIDERS (one platform per PR, easiest first)

Order: **SoundCloud (avatar) → Facebook (avatar, public) → X (avatar + status) →
TikTok (full).**

- **Pic-only platforms (SoundCloud, Facebook, X):** implement only
  `app/monitor/providers/<platform>.py::fetch_profile` returning
  `profile_pic_url` (highest-res anonymous URL) — plus `is_private` for X. The
  orchestrator routes that URL straight into the shared avatar confirmation flow.
  No media provider. Add a live probe + a parse test on a captured fixture. Add a
  proxy route if datacenter-blocked and verify it live.
- **TikTok (full):** implement both providers — profile fields + video list +
  login-free media download (same category as saveinsta: third-party/public,
  degrades to `[]`/`None` on failure, never raises into the sweep) + avatar via
  the shared flow. Wire it into the post/media, dark-radar, stakeout, and rhythm
  phases through the capability gate. Proxy route required (Rule 3), deployed and
  verified from a datacenter-like IP before "done."

Every provider handles handle normalization (URL / @handle / id), 404/rename
recovery where an id exists, rate-limit backoff, and honest failure (Rule 2).

---

## STEP 4 — BOT UX

- `/add` accepts a platform: `/add tiktok @user`, a full profile URL
  (auto-detect platform from the domain), or an inline platform-picker keyboard.
  **Default stays Instagram** when unspecified — current users see no change.
- Account cards show a **platform badge** and render **only capability-supported
  buttons** (a SoundCloud card shows essentially just the avatar/recheck; a
  TikTok card looks like the IG card). `/list`, `/status`, forum topics become
  platform-aware (topic title includes the platform).
- On pic-only platforms, on-demand media commands (`/story`, `/highlights`, bulk
  download) are hidden or reply with an honest "not supported on <platform>".

---

## STEP 5 — TESTING & NON-REGRESSION (the "make no mistake" gate)

Before any platform is "done", ALL must hold:

1. **Instagram parity untouched:** every existing `scripts/test_*.py` passes; a
   live IG add→sweep→notify works end to end; IG sweep timing isn't degraded.
2. **New platform proven live** anonymously; parse test passes on a fixture.
3. **Datacenter reality check:** datacenter-blocked platforms return live data
   through the proxied path from a Render-like environment (Rule 3). No proxy ⇒
   documented as local/residential-only, not silently broken in prod.
4. **Avatar quality check (X/SoundCloud/Facebook):** re-encode the same image →
   **no** false "changed" alert (phash + confirmation pass working); swap to a
   genuinely different image → exactly one alert, full-res document delivered.
5. **Rule audit per platform:** no login/cookie path (1); no stale-cache read
   (2); pause preserves data (4); migration lossless + re-runnable (5).
6. **Graceful degradation:** kill the source mid-run (simulate) → sweep still
   completes, account not corrupted, user gets an honest "unavailable", never a
   crash, never stale data.
7. Update `README.md`, `PROJECT_DOCS.md`, `.env.example`, and add a dated
   `docs/YYYY-MM-DD-<platform>.md` runbook per shipped platform.

---

## DEFINITION OF DONE

- Provider abstraction in place; Instagram runs on it with zero behavior change
  and remains the full-featured primary.
- `docs/platform-capability-matrix.md` states honestly what each in-scope
  platform delivers.
- TikTok ships at full parity; X ships avatar + public/private status;
  SoundCloud and Facebook (public) ship avatar-only — all at Instagram-grade
  avatar quality, all live-proven, proxied where needed, capability-gated,
  migration-safe, documented.
- Tumblr, Snapchat, WhatsApp are absent.
- No hard rule is violated anywhere.

## DO NOT

- Do **not** add any login/cookie/session path for any platform — drop gated
  data and say so.
- Do **not** implement Tumblr, Snapchat, or WhatsApp.
- Do **not** give X/SoundCloud/Facebook anything beyond their scoped surface.
- Do **not** introduce a "last known good" cache presented as live data.
- Do **not** break, slow, or reduce the Instagram path — it is the main goal.
- Do **not** store raw platform payloads in snapshots (keep them featherweight).
- Do **not** push, and do **not** migrate live Neon, without explicit user OK.

## START HERE

Reply with: (a) the Step 0 capability matrix filled from real anonymous probes
of TikTok, X, SoundCloud, and Facebook, and (b) a one-paragraph go/no-go per
platform against its intended scope. Then stop for confirmation before touching
the data model.
