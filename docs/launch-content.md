# The Watcher V3.0 — Launch Content

LinkedIn posts (Arabic + English) and the long-form Medium article. Engineering-first framing — no surveillance language.

---

## LinkedIn Post — النسخة العربية

أعدت بناء نفس المشروع ٣ مرات قبل ما أرضى عن الـ architecture.

هاد الـ writeup عن شو تعلّمت بـ Async Python و TLS fingerprinting و shipping production infra على $0/month.

المشروع: أداة real-time بتتبع التغييرات على public Instagram profile metadata — الـ bio، الصورة، عدد الـ followers، الـ public/private state. مبنية حتى أتعلّم كيف يصمد كود ضد API عدائي، مش حتى تنحوّل لمنتج.

كل شي بلش من endpoint واحد:

```
GET /api/v1/users/web_profile_info/?username=<username>
```

ومن هون بلشت الهندسة الحقيقية.

▸ **HTTP 401 على كل request من السيرفر.** الـ headers كانت صحيحة. طلع إنو Instagram يفحص الـ TLS fingerprint قبل حتى ما يقرا الـ HTTP request. الحل: `curl_cffi` + Chrome TLS impersonation.

▸ **يشتغل محلياً، ينهار على Render.** الـ datacenter IP ranges معظمها flagged. بنيت Cloudflare Worker proxy layer حتى يمر الـ traffic من edge IPs.

▸ **الـ database تضخّمت بسرعة.** الـ flow الأصلي كان يخزّن snapshot كامل بكل sweep. قلبته: `diff → store only if changed`. تقليل ~٩٥٪ بحجم الكتابة، بدون فقدان أي data.

الـ Stack: FastAPI · PostgreSQL · APScheduler · Async Python · Docker · Cloudflare Workers. كل البنية شغّالة على free-tier.

في مشكلتين تانيتين (Telegram image compression و scheduler race conditions) ما دخلوا هون — الـ writeup الكامل مع architecture diagram جاي على Medium.

GitHub: https://github.com/m0hx65/The_Watcher3.0

لسّا عم يطبخ 👁️

---

## LinkedIn Post — English Version

I rebuilt the same project three times before I was satisfied with the architecture.

Here's what I learned about Async Python, TLS fingerprinting, and shipping production infrastructure on $0/month.

The project: a real-time tool that tracks changes to public Instagram profile metadata — bio, picture, follower count, public/private state. Built to learn how to survive a hostile API, not to scale into a product.

It started with one endpoint:

```
GET /api/v1/users/web_profile_info/?username=<username>
```

Then the real engineering started.

▸ **HTTP 401 on every server-side request.** Headers were correct. Turned out Instagram validates the TLS fingerprint *before* it even parses the HTTP request. Fix: `curl_cffi` + Chrome TLS impersonation.

▸ **Worked locally, died on Render.** Datacenter IP ranges are heavily flagged. Built a Cloudflare Worker proxy layer to route through edge IPs.

▸ **Database ballooned.** Initial flow stored a full snapshot on every sweep. Flipped it: `diff → store only if changed`. ~95% less write volume, no information loss.

Stack: FastAPI · PostgreSQL · APScheduler · Async Python · Docker · Cloudflare Workers. Entire infra on free-tier.

Two more problems (Telegram image compression, scheduler race conditions) didn't fit here — full writeup with architecture diagram is coming on Medium.

GitHub: https://github.com/m0hx65/The_Watcher3.0

Still cooking 👁️

---

## Medium Article

### Three Rebuilds, One Lesson: Surviving Instagram's Hostile API

*TLS fingerprinting, edge proxies, and why `store → diff` was the wrong order*

---

A while back I was watching a show about surveillance — the kind of show that makes you spend the next week reading about TLS, proxies, and how platforms actually block bots. I looked at the existing Instagram monitoring tools and they fell into two camps: overpriced SaaS dashboards charging $50/month for what looked like wrapped curl calls, and open-source scraping scripts that died within a week of someone forking them.

So I decided to build one myself. To learn, not to ship a product.

I rebuilt it three times. The first two versions failed in ways I wasn't satisfied with — not "didn't work," but "worked badly." Each rebuild taught me something about async Python, network-level fingerprinting, or storage design that the previous version had hidden from me. This is a writeup of what broke, what I fixed, and what I'd do differently on the next try.

---

#### The endpoint that started it all

```
GET /api/v1/users/web_profile_info/?username=<username>
```

This is a public Instagram endpoint that returns a surprisingly rich JSON payload for any profile: bio, full name, follower count, post count, profile picture URL, public/private state, verification status, and more. It's the same endpoint the web app uses to render a profile page.

The naive plan: hit this endpoint on a schedule, diff the response against the previous snapshot, send a Telegram alert when something changes.

The naive plan did not survive contact with reality.

---

#### Problem #1: HTTP 401 on every server-side request

The first version ran fine in local development. Then I deployed it. Every request returned `401 Unauthorized`, even though my headers, cookies, and user agent were identical to a working browser session.

I spent a long time staring at request diffs before realizing I was looking at the wrong layer.

Instagram validates the **TLS fingerprint** of the connecting client before the HTTP request is even parsed. The way Python's `requests` library negotiates TLS — cipher suite order, extensions, ALPN values — is distinct from how Chrome does it. The handshake itself announces "I am a Python bot" before you've sent a single byte of HTTP.

Fix: [`curl_cffi`](https://github.com/yifeikong/curl_cffi). It's a Python binding over `curl-impersonate`, which patches `libcurl` to mimic real browser TLS fingerprints byte-for-byte.

```python
from curl_cffi import requests

response = requests.get(
    "https://i.instagram.com/api/v1/users/web_profile_info/",
    params={"username": target},
    impersonate="chrome124",
)
```

That one-line change — `impersonate="chrome124"` — moved the success rate from ~0% to ~95% in cold-start conditions. The remaining 5% was a different problem.

---

#### Problem #2: It worked on my machine. Then I deployed it.

Same code, same headers, same TLS fingerprint. Deployed to Render. Instant 401s.

Instagram heavily flags datacenter IP ranges. AWS, GCP, Azure, DigitalOcean, Render, Fly — all of them get treated as suspect by default. The TLS trick gets you in *if* your IP looks residential or at least isn't on a known datacenter list. From a hosting provider's IP, it doesn't matter how Chrome-like your handshake is.

I needed traffic to exit through a different IP than my application server.

Solution: a **Cloudflare Workers proxy layer**. Requests originating from Workers exit through Cloudflare's edge IPs, which most platforms don't aggressively flag (flagging them would block half the legitimate web). The Worker:

1. Accepts an authenticated request from my backend
2. Forwards it to Instagram with headers preserved
3. Streams the response back

```javascript
export default {
  async fetch(request, env) {
    const auth = request.headers.get("X-Proxy-Auth");
    if (auth !== env.PROXY_SECRET) {
      return new Response("unauthorized", { status: 401 });
    }

    const target = new URL(request.headers.get("X-Target-URL"));
    const upstream = await fetch(target, {
      method: request.method,
      headers: filterHeaders(request.headers),
    });

    return new Response(upstream.body, {
      status: upstream.status,
      headers: upstream.headers,
    });
  },
};
```

Cloudflare's free tier gives 100k Worker invocations per day. For a personal tool sweeping a handful of profiles every few minutes, that ceiling is comfortably out of reach.

---

#### Problem #3: The database wouldn't stop growing

The first storage design was the obvious one: every sweep, store a full snapshot of every profile. Run a diff query when generating alerts.

This is fine for a week. It is not fine for a month.

After a few weeks the Postgres database had millions of rows that were ~99% identical to the row before them. Most profiles were idle, being polled every few minutes for completeness.

The architecture flip was simple to describe and unobvious in advance:

**Before:** `fetch → store snapshot → diff against previous snapshot → maybe alert`

**After:** `fetch → diff against last stored snapshot → store only if changed → alert`

The diff happens in memory before anything hits the database. If nothing changed, nothing is written. Write volume dropped by ~95% on real-world data, and storage cost started growing with *actual profile activity* instead of with *polling frequency*.

The lesson: storage decisions aren't about how much data you *could* store. They're about which axis you want your storage cost to grow on. Polling frequency is an axis you control. Real-world profile activity is an external signal you can't.

---

#### Problem #4: Telegram was destroying image quality

Profile pictures and image-based change events get delivered to Telegram. The first version used `sendPhoto`, which gets you nice inline previews.

It also aggressively recompresses the image. Detail you'd need to actually see a profile picture change gets smoothed out by Telegram's photo pipeline.

Two fixes, combined:

1. **Use `sendDocument` instead of `sendPhoto`.** Documents aren't recompressed. They render as a downloadable file rather than an inline image, but the quality survives intact.
2. **Pull the HD version of the profile picture from Instagram's mobile API endpoints** rather than the web profile endpoint. The web endpoint returns a downsized variant; the mobile API returns closer to the original upload.

Combined, a human reviewing alerts could actually see what changed instead of squinting at JPEG artifacts.

---

#### Problem #5: Rapid restarts produced overlapping jobs

APScheduler runs sweeps on a fixed interval. If the application restarted while a sweep was in flight, or if a user hammered the manual "sweep now" button, you'd end up with two or three sweeps running concurrently against the same profile set. Same data fetched in parallel, same diffs computed, occasional duplicate alerts.

Two layers of defense:

1. **Per-profile locking.** Before sweeping a profile, acquire a lock keyed on that profile's ID. If another sweep already holds the lock, skip — don't queue, just skip. A redundant sweep five seconds later has near-zero new information.
2. **Scheduler state tracking.** On startup, the scheduler reads the last-run timestamp for each job. Jobs that ran less than their interval ago are skipped on boot. This kills the "restart triggers an immediate full sweep" problem.

```python
async def sweep_profile(profile_id: str):
    acquired = await redis.set(
        f"sweep:lock:{profile_id}", "1", ex=60, nx=True
    )
    if not acquired:
        return
    try:
        await do_sweep(profile_id)
    finally:
        await redis.delete(f"sweep:lock:{profile_id}")
```

The `nx=True` (set if not exists) is doing all the work. If two coroutines race for the lock, exactly one wins and the other returns immediately.

---

#### The stack, in one place

- **FastAPI** — admin / control API
- **PostgreSQL** — snapshots, diffs, alert history
- **APScheduler** — sweep orchestration
- **Async Python** end-to-end — `curl_cffi` supports async natively
- **Docker** — deployment portability
- **Cloudflare Workers** — proxy layer
- **Telegram Bot API** — alerting

Everything runs on free tiers. Render's free web service, free Postgres, Cloudflare Workers free plan, Telegram bots are free. Total monthly cost: $0.

---

#### What I'd do differently

If I started a fourth rebuild tomorrow:

1. **Build the diff layer first, store layer second.** I wasted weeks on storage schema decisions that became obsolete the moment I flipped to `diff → store`. The diff is the core of the system; storage is a side effect of the diff.
2. **Treat the network layer as a first-class concern from day one.** TLS and IP problems weren't edge cases — they were the project. Architecting around them from the start, instead of discovering them in production, would have saved months.
3. **Lock per-resource, not per-job.** The first lock I added was scheduler-level: "only one sweep at a time." That was too coarse — it serialized profiles that had nothing to do with each other. Per-profile locking is the right granularity.

---

#### What's next

I'm experimenting with lightweight ML-assisted analysis of the media flowing through the pipeline — scene classification on profile pictures, OCR on bio text, similarity scoring across image hashes — to make alerts more useful. The current SHA-256 hashing catches exact-match changes; the next iteration is about detecting *meaningful* changes, not just *any* changes.

GitHub: https://github.com/m0hx65/The_Watcher3.0

If you spot something in the architecture I should have done differently, the issues tab is open.
