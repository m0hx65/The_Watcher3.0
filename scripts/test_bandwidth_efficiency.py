"""Regression tests for the egress cuts — no fetch may cost detection.

Render bills service-initiated bandwidth, and nearly all of the bot's was spent
re-fetching things it already knew. Three cuts, each verified here to remove
only work whose outcome is already determined:

1. The avatar is NOT re-downloaded when the CDN URL carries the same numeric
   asset id as the fingerprinted baseline. Instagram assigns a new id if and
   only if a new picture is uploaded, and _pic_changed can never report a change
   without perceptual evidence — so the skipped download could not have changed
   any outcome. Every escape hatch (no baseline, legacy hash, new id, non-CDN
   URL) still downloads.
2. The story media listing is skipped only when Instagram's own live flag says
   there is no story. A blocked/absent reel query still asks saveinsta, which is
   the story oracle in that case.
3. Highlight reels are re-listed on a cadence instead of every sweep, but a reel
   that is NEW to the catalog is always listed immediately.

Runs offline on sqlite with fakes — no network.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_bandwidth.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.config import settings  # noqa: E402
from app.database import crud  # noqa: E402
from app.database.models import Base, MonitoredAccount  # noqa: E402
from app.database.session import engine, get_session  # noqa: E402
from app.monitor.instagram import IdProbe, ProfileFetchResult  # noqa: E402
from app.monitor.media_hasher import HashedMedia, PHASH_PREFIX  # noqa: E402
from app.monitor.service import MonitorService  # noqa: E402

FAILURES: list[str] = []

# Same avatar upload, different shard / size variant / signature — what the CDN
# hands back on every single sweep.
URL_A = (
    "https://scontent-mad1-1.cdninstagram.com/v/t51.2885-19/s320x320/"
    "111111111_2222222222222222_3333333333333333333_n.jpg"
    "?stp=dst-jpg_e0_s320x320&_nc_ht=scontent.cdninstagram.com&oh=aaa&oe=111"
)
URL_A_ROTATED = (
    "https://scontent-lhr8-1.cdninstagram.com/v/t51.2885-19/s150x150/"
    "111111111_2222222222222222_3333333333333333333_s.jpg"
    "?stp=dst-jpg_e0_s150x150&_nc_ht=other.cdninstagram.com&oh=bbb&oe=222"
)
# A genuinely new upload.
URL_B = (
    "https://scontent-mad1-1.cdninstagram.com/v/t51.2885-19/s320x320/"
    "444444444_5555555555555555_6666666666666666666_n.jpg"
    "?stp=dst-jpg_e0_s320x320&_nc_ht=scontent.cdninstagram.com&oh=ccc&oe=333"
)


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def _fp(dhash: int = 0, ahash: int = 0, mean: int = 0x80) -> str:
    return f"{PHASH_PREFIX}{dhash:064x}:{ahash:064x}:{mean:02x}"


class CountingHasher:
    """Counts downloads; returns a scripted fingerprint per call."""

    def __init__(self, phashes: list, path: Path) -> None:
        self._phashes = phashes
        self._path = path
        self.calls = 0

    async def hash_url(self, url: str, username: str):
        phash = self._phashes[min(self.calls, len(self._phashes) - 1)]
        self.calls += 1
        if phash is None:
            return None
        return HashedMedia(
            sha256=f"{self.calls:064x}",
            byte_size=100,
            content_type="image/jpeg",
            local_path=self._path,
            source_url=url,
            phash=phash,
        )


class FakeInstagram:
    """Serves one mutable parsed profile; no reel data."""

    def __init__(self, parsed: dict) -> None:
        self.parsed = parsed

    async def fetch_profile(self, username: str, **kw) -> ProfileFetchResult:
        return ProfileFetchResult(
            username=username, http_status=200,
            parsed=dict(self.parsed),
            raw_response={"data": {"user": {"id": self.parsed["instagram_id"]}}},
        )

    async def fetch_reel_user(self, user_id):
        return None

    async def fetch_hd_pic_url(self, user_id):
        return None

    async def probe_by_id(self, user_id):
        # The client's numeric-id probe; this fake has no reel data, so the
        # id route reads as blocked and the check proceeds by username.
        return IdProbe(user_id=str(user_id), status=401)


class CountingStories:
    """Counts saveinsta listings."""

    def __init__(self) -> None:
        self.story_calls = 0
        self.highlight_calls: list[str] = []

    async def fetch_stories(self, username):
        self.story_calls += 1
        return []

    async def fetch_highlight_items(self, username, highlight_id, title):
        self.highlight_calls.append(str(highlight_id))
        return []

    async def fetch_profile_pic_url(self, username):
        return None


def _parsed(username: str, ig_id: str, pic_url: str) -> dict:
    return {
        "username": username, "full_name": "T", "biography": "",
        "followers_count": 10, "following_count": 5, "posts_count": 0,
        "reels_count": 0, "story_count": 0, "is_private": True,
        "is_verified": False, "is_business": False,
        "profile_pic_url": pic_url, "external_url": None,
        "instagram_id": ig_id,
    }


def _service(instagram, hasher, stories=None) -> MonitorService:
    notifier = AsyncMock()
    notifier.send_text = AsyncMock(return_value=True)
    notifier.send_document = AsyncMock(return_value=True)
    notifier.send_photo = AsyncMock(return_value=True)
    notifier.create_forum_topic = AsyncMock(return_value=None)
    return MonitorService(
        instagram=instagram, hasher=hasher, notifier=notifier, stories=stories,
    )


# ---------- 1. The avatar download ----------

async def test_same_upload_is_not_redownloaded(tmp: Path) -> None:
    async with get_session() as session:
        session.add(MonitoredAccount(username="steady", active=True))

    parsed = _parsed("steady", "3001", URL_A)
    hasher = CountingHasher([_fp()], tmp / "p.jpg")
    service = _service(FakeInstagram(parsed), hasher)

    await service.check_username("steady")
    expect("first sighting downloads the avatar", hasher.calls == 1,
           f"calls={hasher.calls}")

    # Every later sweep sees a rotated URL for the SAME upload.
    parsed["profile_pic_url"] = URL_A_ROTATED
    for _ in range(5):
        r = await service.check_username("steady")
        expect("unchanged avatar reports no change", r.get("changed") is False,
               repr(r))
    expect("5 more sweeps download the avatar ZERO more times",
           hasher.calls == 1, f"calls={hasher.calls}")

    async with get_session() as session:
        stored_hash, stored_url = await crud.get_latest_pic_baseline(
            session, (await crud.get_account(session, "steady")).id
        )
    expect("the baseline fingerprint survives the skip", stored_hash == _fp(),
           repr(stored_hash))
    expect("the baseline URL refreshes to the current signed one",
           stored_url == URL_A_ROTATED, repr(stored_url))


async def test_new_upload_still_downloads(tmp: Path) -> None:
    async with get_session() as session:
        session.add(MonitoredAccount(username="swapper", active=True))

    parsed = _parsed("swapper", "3002", URL_A)
    changed = _fp(dhash=(1 << 200) - 1, ahash=(1 << 200) - 1, mean=0x10)
    hasher = CountingHasher([_fp(), changed, changed], tmp / "p.jpg")
    service = _service(FakeInstagram(parsed), hasher)

    await service.check_username("swapper")          # baseline, 1 download
    parsed["profile_pic_url"] = URL_A_ROTATED
    await service.check_username("swapper")          # skipped
    expect("rotation alone never downloads", hasher.calls == 1,
           f"calls={hasher.calls}")

    parsed["profile_pic_url"] = URL_B                # a real new upload
    r = await service.check_username("swapper")
    expect("a new asset id downloads again", hasher.calls > 1,
           f"calls={hasher.calls}")
    expect("the real change is still detected", r.get("changed") is True, repr(r))
    expect("the photo is still delivered",
           service.notifier.send_document.call_count
           + service.notifier.send_photo.call_count == 1)


async def test_skip_needs_a_v2_baseline(tmp: Path) -> None:
    async with get_session() as session:
        session.add(MonitoredAccount(username="legacy", active=True))

    parsed = _parsed("legacy", "3003", URL_A)
    # A legacy (pre-v2) stored hash must not authorize a skip — the baseline has
    # to be re-established in the current format first.
    hasher = CountingHasher([None, _fp()], tmp / "p.jpg")
    service = _service(FakeInstagram(parsed), hasher)

    await service.check_username("legacy")   # download fails -> no fingerprint
    expect("a failed download is still attempted", hasher.calls == 1)
    parsed["profile_pic_url"] = URL_A_ROTATED
    await service.check_username("legacy")   # no baseline yet -> must retry
    expect("no stored fingerprint means no skip", hasher.calls == 2,
           f"calls={hasher.calls}")
    parsed["profile_pic_url"] = URL_A
    await service.check_username("legacy")   # now a v2 baseline exists -> skip
    expect("the skip engages once a v2 baseline exists", hasher.calls == 2,
           f"calls={hasher.calls}")


async def test_non_cdn_url_always_downloads(tmp: Path) -> None:
    async with get_session() as session:
        session.add(MonitoredAccount(username="odd", active=True))

    parsed = _parsed("odd", "3004", "http://example.test/avatar.jpg")
    hasher = CountingHasher([_fp()], tmp / "p.jpg")
    service = _service(FakeInstagram(parsed), hasher)

    await service.check_username("odd")
    await service.check_username("odd")
    expect("an unparseable avatar URL disables the skip", hasher.calls == 2,
           f"calls={hasher.calls}")


# ---------- 2. The story media listing ----------

async def _story_phase(service, account_id, username, reel_data) -> None:
    await service._check_stories_and_highlights(
        account_id, username, instagram_id="555", reel_data=reel_data
    )


async def test_story_listing_follows_the_live_flag() -> None:
    async with get_session() as session:
        account = MonitoredAccount(username="quiet", active=True, instagram_id="555")
        session.add(account)
        await session.flush()
        account_id = account.id

    stories = CountingStories()
    service = _service(FakeInstagram(_parsed("quiet", "555", URL_A)), AsyncMock(),
                       stories=stories)

    await _story_phase(service, account_id, "quiet",
                       {"has_public_story": False, "is_live": False, "highlights": {}})
    expect("no story -> no media listing", stories.story_calls == 0,
           f"calls={stories.story_calls}")

    await _story_phase(service, account_id, "quiet",
                       {"has_public_story": True, "is_live": False, "highlights": {}})
    expect("an active story IS listed", stories.story_calls == 1,
           f"calls={stories.story_calls}")

    # Reel query blocked: saveinsta is the oracle, so it must still be asked.
    service.instagram.fetch_reel_user = AsyncMock(return_value=None)
    await _story_phase(service, account_id, "quiet", None)
    expect("an unknown status still asks saveinsta", stories.story_calls == 2,
           f"calls={stories.story_calls}")


# ---------- 3. The highlight re-scan cadence ----------

async def test_highlight_scan_cadence() -> None:
    async with get_session() as session:
        account = MonitoredAccount(username="reels", active=True, instagram_id="777")
        session.add(account)
        await session.flush()
        account_id = account.id

    service = _service(AsyncMock(), AsyncMock(), stories=CountingStories())
    catalog = {"h1": "One", "h2": "Two"}
    original = settings.highlight_scan_interval
    try:
        settings.highlight_scan_interval = 3600

        scan, full = await service._due_highlight_scan(account_id, catalog, catalog)
        expect("the first pass scans every reel", scan == catalog and full,
               f"{scan} full={full}")

        async with get_session() as session:
            await crud.set_setting(
                session, service._highlight_scan_key(account_id), str(time.time())
            )
        scan, full = await service._due_highlight_scan(account_id, catalog, catalog)
        expect("a fresh scan stamp skips the re-list", scan == {} and not full,
               f"{scan} full={full}")

        # A reel that appeared since the last catalog is listed immediately.
        grown = {**catalog, "h3": "New"}
        scan, full = await service._due_highlight_scan(account_id, grown, catalog)
        expect("a NEW reel is listed without waiting",
               scan == {"h3": "New"} and not full, f"{scan} full={full}")

        # Once the interval has elapsed, everything is re-listed again.
        async with get_session() as session:
            await crud.set_setting(
                session, service._highlight_scan_key(account_id),
                str(time.time() - 7200),
            )
        scan, full = await service._due_highlight_scan(account_id, catalog, catalog)
        expect("an elapsed interval re-lists every reel", scan == catalog and full,
               f"{scan} full={full}")

        # 0 disables the cadence entirely (old per-sweep behavior).
        settings.highlight_scan_interval = 0
        async with get_session() as session:
            await crud.set_setting(
                session, service._highlight_scan_key(account_id), str(time.time())
            )
        scan, full = await service._due_highlight_scan(account_id, catalog, catalog)
        expect("interval 0 restores per-sweep scanning", scan == catalog and full,
               f"{scan} full={full}")
    finally:
        settings.highlight_scan_interval = original


async def test_story_phase_applies_the_cadence() -> None:
    async with get_session() as session:
        account = MonitoredAccount(username="hilite", active=True, instagram_id="888")
        session.add(account)
        await session.flush()
        account_id = account.id
        # Pre-seed the catalog so this isn't a baseline pass.
        await crud.replace_highlight_catalog(session, account_id, {"h1": "One"})

    stories = CountingStories()
    service = _service(FakeInstagram(_parsed("hilite", "888", URL_A)), AsyncMock(),
                       stories=stories)
    reel = {"has_public_story": False, "is_live": False, "highlights": {"h1": "One"}}
    original = settings.highlight_scan_interval
    try:
        settings.highlight_scan_interval = 3600
        await _story_phase(service, account_id, "hilite", reel)
        expect("the first sweep lists the reel", stories.highlight_calls == ["h1"],
               repr(stories.highlight_calls))
        await _story_phase(service, account_id, "hilite", reel)
        await _story_phase(service, account_id, "hilite", reel)
        expect("later sweeps inside the window list nothing",
               stories.highlight_calls == ["h1"], repr(stories.highlight_calls))

        reel = {**reel, "highlights": {"h1": "One", "h2": "Fresh"}}
        await _story_phase(service, account_id, "hilite", reel)
        expect("a newly added reel is listed right away",
               stories.highlight_calls == ["h1", "h2"],
               repr(stories.highlight_calls))
    finally:
        settings.highlight_scan_interval = original


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    tmp = ROOT / "data" / "media_test"
    tmp.mkdir(parents=True, exist_ok=True)
    pic = tmp / "p.jpg"
    if not pic.exists():
        pic.write_bytes(b"\xff\xd8\xfffake")

    await test_same_upload_is_not_redownloaded(tmp)
    await test_new_upload_still_downloads(tmp)
    await test_skip_needs_a_v2_baseline(tmp)
    await test_non_cdn_url_always_downloads(tmp)
    await test_story_listing_follows_the_live_flag()
    await test_highlight_scan_cadence()
    await test_story_phase_applies_the_cadence()

    await engine.dispose()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All bandwidth-efficiency tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
