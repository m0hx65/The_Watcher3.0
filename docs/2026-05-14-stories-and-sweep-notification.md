# Session Log — 2026-05-14: Sweep Notification + Stories/Highlights Monitoring

---

## 1. Sweep-Complete Notification

**Problem:** After every scheduled sweep the bot went silent — no signal that it had finished. You couldn't tell whether it was idle, stuck, or just had nothing to report.

**Fix:** Added a summary message at the end of `MonitorService.check_all()` that always fires, even when nothing changed.

```
👁 Sweep complete — 4 profiles checked.
```

If any account failed to fetch:

```
👁 Sweep complete — 4 profiles checked. 1 failed.
```

**File changed:** `app/monitor/service.py`

```python
# appended to check_all() after gather results are tallied
noun = "profile" if checked == 1 else "profiles"
summary = f"👁 Sweep complete — {checked} {noun} checked."
if failed:
    summary += f" {failed} failed."
await self.notifier.send_text(summary)
```

**Commit:** `3954c37`

---

## 2. Story and Highlight Monitoring

**Goal:** For every monitored account, fetch active stories and highlight items each sweep, download new ones, and send them to Telegram — photos as photos, videos as videos. Each item delivered exactly once (dedup by story PK).

**Inspiration:** [fabula](https://github.com/mrizkimaulidan/fabula) — a Go tool that does the same thing using the storiesig.info public API.

---

### 2a. Architecture

#### New file: `app/monitor/stories.py`

`StoryItem` dataclass:

| Field | Type | Description |
|---|---|---|
| `pk` | `str` | Instagram's internal unique ID — used as the dedup key |
| `taken_at` | `int` | Unix timestamp when the story was created |
| `media_type` | `str` | `"image"` or `"video"` |
| `url` | `str` | Direct CDN URL to the media file |
| `source` | `str` | `"story"` or `"highlight"` |
| `highlight_id` | `str \| None` | Highlight reel ID (highlights only) |
| `highlight_title` | `str \| None` | Reel label shown in the caption (highlights only) |

`StoriesClient` methods:

| Method | Description |
|---|---|
| `fetch_stories(username)` | Calls `/api/story?url=https://www.instagram.com/stories/{username}` |
| `fetch_highlights(username)` | Resolves PK → fetches highlight list → fetches each reel's items |
| `download(item, username)` | Saves to `{MEDIA_DIR}/{username}/stories/{pk}.jpg|mp4` |
| `_get_user_pk(username)` | Calls `/api/userInfoByUsername/{username}` |
| `_parse_content(data, source)` | Extracts `video_versions[0]` or `image_versions2.candidates[0]` |

All methods catch exceptions and return empty lists / `None` so a dead API never breaks the sweep.

Uses `curl_cffi.requests.AsyncSession(impersonate="chrome120")` — same Chrome TLS impersonation as the rest of the project.

---

#### New DB model: `SeenStory` in `app/database/models.py`

```python
class SeenStory(Base):
    __tablename__ = "seen_stories"
    id           = Column(Integer, primary_key=True)
    account_id   = Column(Integer, ForeignKey("monitored_accounts.id", ondelete="CASCADE"))
    story_pk     = Column(String(64), nullable=False)
    source       = Column(String(16), nullable=False)   # "story" | "highlight"
    highlight_id    = Column(String(64), nullable=True)
    highlight_title = Column(String(255), nullable=True)
    media_type   = Column(String(8), nullable=False)    # "image" | "video"
    taken_at     = Column(Integer, nullable=False)
    seen_at      = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_seen_stories_account_pk", "account_id", "story_pk", unique=True),
    )
```

Created automatically on startup by `init_db()` → `Base.metadata.create_all()`. No migration needed.

---

#### New CRUD helpers: `app/database/crud.py`

```python
async def get_seen_story_pks(session, account_id) -> set[str]:
    """Return all story PKs already delivered for this account."""

async def mark_story_seen(session, account_id, story_pk, source,
                          highlight_id, highlight_title, media_type, taken_at):
    """Record a delivered story item."""
```

---

#### New Telegram method: `NotificationDispatcher.send_video()` in `app/bot/notifications.py`

```python
async def send_video(self, path: Path, caption: Optional[str] = None) -> bool:
    async def _send():
        with open(path, "rb") as f:
            await self.bot.send_video(
                chat_id=self.chat_id, video=f,
                caption=caption or "", parse_mode=ParseMode.HTML,
                supports_streaming=True,
            )
    ok = await self._send_with_retry(_send)
    if ok and self.post_send_hook is not None:
        await self.post_send_hook()
    return ok
```

---

#### `MonitorService` changes: `app/monitor/service.py`

`__init__` now accepts an optional `StoriesClient`:

```python
def __init__(self, instagram, hasher, notifier, stories=None):
    ...
    self.stories = stories
```

`check_all()` runs story checks after profiles, concurrently:

```python
if self.stories is not None:
    await asyncio.gather(
        *(self._check_stories_and_highlights(aid, uname) for aid, uname in targets),
        return_exceptions=True,
    )
```

`_check_stories_and_highlights(account_id, username)`:
1. Fetches stories and highlights in parallel (`asyncio.gather`)
2. Loads seen PKs from DB
3. For each new item: downloads media, sends photo or video to Telegram, marks seen on success
4. Items that fail to download are marked seen anyway — prevents infinite retry on expired stories

Captions:
- Story: `📖 @username — new story`
- Highlight: `✨ @username — highlight: <title>`

---

#### `app/main.py`

```python
stories = StoriesClient()
monitor = MonitorService(instagram, hasher, dispatcher, stories)
# ...
await stories.close()  # in shutdown block
```

**Commit:** `4999ddd`

---

#### Test script: `scripts/test_stories.py`

End-to-end smoke test (run standalone, no server needed):

```bash
python scripts/test_stories.py saudibox1
```

Steps:
1. Resolve user PK via `userInfoByUsername`
2. Fetch active stories
3. Fetch highlights (list + items per reel)
4. Download one image and one video item to disk
5. Re-fetch stories — verify PKs are stable across calls (dedup key sanity check)

---

### 2b. API Investigation — Why It Doesn't Work Yet

The fabula tool uses `https://api-ig.storiesig.info/api` as its base URL. **This endpoint is dead.**

| Endpoint | Result |
|---|---|
| `GET api-ig.storiesig.info/api/userInfoByUsername/saudibox1` | `404 page not found` |
| `GET storiesig.info/api/userInfoByUsername/saudibox1` | `403 Cloudflare` |
| `GET storiesig.info/api/v2/userInfoByUsername/saudibox1` | `403 Cloudflare` |

The `storiesig.info` website itself is up and their [API FAQ page](https://storiesig.info/en/api-faq/) says:

> "To get access or get more information contact us at contact@storiesig.info"

The old free/open endpoint was shut down. The API is now access-controlled.

---

### 2c. Alternatives Investigated

| Service | Result | Reason |
|---|---|---|
| `api-ig.storiesig.info` | `404` | Old free endpoint — dead |
| `storiesig.info/api/*` | `403` | Cloudflare-gated; requires auth |
| `anonyig.com` | DNS failure | Not reachable from this network |
| `imginn.com` | `403` | Cloudflare bot protection |
| `picuki.com` | `403` | Cloudflare bot protection |
| `dumpor.com` | `200` but unusable | Phoenix LiveView SPA — no REST API, WebSocket-based SSR |
| Instagram mobile API (`i.instagram.com/api/v1/feed/user/.../story/`) | `403 login_required` | Requires authenticated session |
| `instaloader` Python library | `403` from IG GraphQL | Requires login for stories |
| `instagpy` Python library | `429 Too Many Requests` | Rate-limited before useful response |
| `instagram-private-api` library | Requires credentials | No anonymous mode |
| `gallery-dl` | Not installed | Could work but also requires auth for stories |

**Root cause:** Instagram locked story access behind authentication in 2024. All no-login approaches — whether direct or via third-party proxies — are either dead or blocked.

---

### 2d. Current Status

The code is written, merged, and pushed. It degrades gracefully:
- When the API is unreachable → `fetch_stories()` and `fetch_highlights()` log a warning and return `[]`
- The sweep completes normally, just without stories
- The `seen_stories` table is created on first boot

**What's needed to activate it:** An API key from storiesig.info.

---

### 2e. Next Steps — When the API Key Arrives

1. Add `STORIESIG_API_KEY` to `app/config.py` (optional string, default `None`)
2. Pass it as an `Authorization: Bearer <key>` header in `StoriesClient.__init__()`:

```python
headers = {}
if settings.storiesig_api_key:
    headers["Authorization"] = f"Bearer {settings.storiesig_api_key}"
self._session = AsyncSession(impersonate=_CHROME, timeout=30, headers=headers)
```

3. Add `STORIESIG_API_KEY=<key>` to `.env` and Render environment variables
4. Run `python scripts/test_stories.py saudibox1` to confirm all five checks pass
5. Push

---

### 2f. Email to Send

**To:** contact@storiesig.info  
**Subject:** API Access Request — Automated Story Monitoring Bot

> Hi,
>
> I'm building a private Telegram bot that monitors public Instagram accounts for my personal use. I'd like to use the StoriesIG API to fetch stories and highlights for a list of accounts (roughly 10–20 accounts, checked every 8 hours — very low volume).
>
> Could you let me know:
> - Is there a free tier or trial available?
> - If it's paid, what are the pricing plans?
> - What authentication method does the API use (API key, OAuth, etc.)?
>
> Thank you.

---

## 3. Sweep Interval

Use the existing `/interval` bot command — no code change needed:

```
/interval 8h
```

This updates `CHECK_INTERVAL` in the DB via `AppSetting` and reschedules the APScheduler job immediately.

---

## 4. Commits Pushed This Session

| Commit | Description |
|---|---|
| `3954c37` | Send sweep-complete notification after every scheduled check |
| `4999ddd` | Add story and highlight monitoring via storiesig.info |
| `a80e459` | Update README: sweep-complete notification and story/highlight monitoring |
