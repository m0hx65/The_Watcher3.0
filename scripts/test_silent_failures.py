"""Two ways this bot can go quiet without saying so (2026-09-07 audit).

Both were found by reading the code against a real log, and neither shows up
as an error anywhere — that is exactly what makes them worth a test.

1. **A database blip latched the sweep off forever.** The "last sweep at"
   timestamp was written between `_sweep_in_flight = True` and the `try`, so a
   refused connection — a managed free tier that suspends when idle, hit at
   the coldest moment there is — raised straight past the `finally`. The flag
   stayed True for the life of the process and every later sweep answered
   "another sweep is already in progress", while /status went on reporting a
   running scheduler and a next run time.

2. **New posts stopped being delivered.** Delivery is triggered by the post
   count RISING, and the profile page never carries a count
   (`all_media_count` is null on every capture). So from the day the username
   API was login-walled, nothing rose, nothing was listed, and no post was
   ever sent — with no error, no warning and no missing field on any card.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
DB_FILE = ROOT / "test_silent_failures.db"
if DB_FILE.exists():
    DB_FILE.unlink()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.config import settings  # noqa: E402
from app.database import crud  # noqa: E402
from app.database.models import Base, MonitoredAccount  # noqa: E402
from app.database.session import engine, get_session  # noqa: E402
from app.monitor.change_detector import Change, ChangeSet  # noqa: E402
from app.monitor.service import MonitorService  # noqa: E402
from app.monitor.stories import StoryItem  # noqa: E402
from app.workers import scheduler as scheduler_mod  # noqa: E402
from app.workers.scheduler import WatcherScheduler  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


# ---------- 1. a database blip must not latch the sweep off ---------------

class _RefusedSession:
    """A session context manager that fails the way a suspended database
    does: on the way in, before anything can be written."""

    async def __aenter__(self):
        raise RuntimeError("connection refused")

    async def __aexit__(self, *exc) -> bool:  # pragma: no cover - never reached
        return False


class _CountingService:
    def __init__(self) -> None:
        self.sweeps = 0
        self.raises = False

    async def check_all(self, *, backfill_ids: bool = False) -> dict:
        self.sweeps += 1
        if self.raises:
            raise RuntimeError("Instagram fell over")
        return {"checked": 0, "changed": 0, "failed": 0}


async def test_a_refused_database_does_not_latch_the_sweep_off() -> None:
    service = _CountingService()
    sched = WatcherScheduler(service)  # type: ignore[arg-type]
    real_get_session = scheduler_mod.get_session
    try:
        scheduler_mod.get_session = lambda: _RefusedSession()  # type: ignore[assignment]
        await sched._sweep_wrapper()
        expect("the sweep still runs when the timestamp cannot be written",
               service.sweeps == 1, repr(service.sweeps))
        expect("and the in-flight flag is released",
               not sched.sweep_in_flight)
        await sched._sweep_wrapper()
        expect("so the NEXT sweep is not refused as 'already in progress'",
               service.sweeps == 2, repr(service.sweeps))
    finally:
        scheduler_mod.get_session = real_get_session  # type: ignore[assignment]


async def test_the_flag_is_released_however_the_sweep_ends() -> None:
    """The three exits that must all leave the door unlocked: a clean run, a
    crash inside check_all, and the hard timeout."""
    service = _CountingService()
    sched = WatcherScheduler(service)  # type: ignore[arg-type]

    await sched._sweep_wrapper()
    expect("a clean sweep releases the flag", not sched.sweep_in_flight)
    async with get_session() as session:
        stamped = await crud.get_setting(session, scheduler_mod.SETTING_LAST_SWEEP_AT)
    expect("and records when it ran", stamped is not None, repr(stamped))

    service.raises = True
    await sched._sweep_wrapper()
    expect("a crashing sweep releases it too", not sched.sweep_in_flight)

    class _Hanging:
        async def check_all(self, *, backfill_ids: bool = False) -> dict:
            await asyncio.sleep(30)
            return {}

    hung = WatcherScheduler(_Hanging())  # type: ignore[arg-type]
    old_timeout = settings.sweep_timeout_seconds
    try:
        settings.sweep_timeout_seconds = 0  # clamped to the 60s floor…
        # …so drive the timeout directly rather than waiting a minute.
        task = asyncio.ensure_future(hung._sweep_wrapper())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        expect("and a cancelled sweep releases it as well",
               not hung.sweep_in_flight)
    finally:
        settings.sweep_timeout_seconds = old_timeout


# ---------- 2. new posts, when no count can be read -----------------------

class _Grid:
    """A saveinsta stand-in: a fixed grid, and a record of every listing."""

    def __init__(self, posts: list[StoryItem]) -> None:
        self.posts = posts
        self.listings: list[str] = []

    async def fetch_posts(self, username: str, limit: int = 12) -> list[StoryItem]:
        self.listings.append(username)
        return list(self.posts)

    async def download(self, item, username):
        return None  # delivery is exercised elsewhere; here we count listings

    async def fetch_stories(self, username):
        return []

    async def fetch_profile_pic_url(self, username):
        return None


def _post(pk: str) -> StoryItem:
    return StoryItem(pk=pk, taken_at=0, media_type="image",
                     url=f"https://dl.snapcdn.app/{pk}", source="post")


def _service(stories) -> MonitorService:
    notifier = AsyncMock()
    notifier.send_text = AsyncMock(return_value=True)
    notifier.send_photo = AsyncMock(return_value=True)
    notifier.send_video = AsyncMock(return_value=True)
    notifier.create_forum_topic = AsyncMock(return_value=None)
    return MonitorService(
        instagram=AsyncMock(), hasher=AsyncMock(), notifier=notifier,
        stories=stories,
    )


async def _new_account(username: str) -> int:
    async with get_session() as session:
        account = MonitoredAccount(username=username, active=True, instagram_id="42")
        session.add(account)
        await session.flush()
        return account.id


async def test_a_page_reading_still_finds_new_posts() -> None:
    """The page carries no count, so nothing can rise. The listing has to be
    the detector instead, or a new post is never seen at all."""
    account_id = await _new_account("poster")
    grid = _Grid([_post("p1"), _post("p2")])
    service = _service(grid)
    quiet = ChangeSet(username="poster")  # no posts_count change: none was read

    # The old behaviour, and still correct when a count WAS readable: no rise,
    # nothing to do, no third-party call.
    await service._handle_new_posts(
        account_id, "poster", quiet, first_seen=False, counts_seen=True,
    )
    expect("a readable count that did not rise lists nothing",
           grid.listings == [], repr(grid.listings))

    # No count readable: the grid is listed and the unseen posts are found.
    await service._handle_new_posts(
        account_id, "poster", quiet, first_seen=False, counts_seen=False,
    )
    expect("an unreadable count falls back to listing the grid",
           grid.listings == ["poster"], repr(grid.listings))
    sent = [c.args[0] for c in service.notifier.send_text.await_args_list]
    expect("and the new posts are announced",
           any("2 new posts" in t for t in sent), repr(sent))


async def test_the_fallback_listing_is_paced_not_every_sweep() -> None:
    """One saveinsta round trip per public account per sweep is the cost, so
    it obeys POST_SCAN_INTERVAL — and a listing that came back stamps the
    clock even when nothing in it was new, because that IS the answer."""
    account_id = await _new_account("paced")
    grid = _Grid([_post("q1")])
    service = _service(grid)
    quiet = ChangeSet(username="paced")

    await service._handle_new_posts(
        account_id, "paced", quiet, first_seen=False, counts_seen=False,
    )
    expect("the first listing happens", len(grid.listings) == 1, repr(grid.listings))

    await service._handle_new_posts(
        account_id, "paced", quiet, first_seen=False, counts_seen=False,
    )
    expect("the next sweep does not list again inside the interval",
           len(grid.listings) == 1, repr(grid.listings))

    # Age the clock past the interval.
    async with get_session() as session:
        await crud.set_setting(
            session, service._post_scan_key(account_id),
            str(time.time() - settings.post_scan_interval - 60),
        )
    await service._handle_new_posts(
        account_id, "paced", quiet, first_seen=False, counts_seen=False,
    )
    expect("and does once the interval has passed",
           len(grid.listings) == 2, repr(grid.listings))


async def test_a_failed_listing_is_retried_not_recorded() -> None:
    """An empty answer is a failed source or an empty grid — not a reading.
    Stamping it would make the next sweep skip a listing that never happened."""
    account_id = await _new_account("empty")
    grid = _Grid([])
    service = _service(grid)
    quiet = ChangeSet(username="empty")

    await service._handle_new_posts(
        account_id, "empty", quiet, first_seen=False, counts_seen=False,
    )
    await service._handle_new_posts(
        account_id, "empty", quiet, first_seen=False, counts_seen=False,
    )
    expect("an empty listing does not stamp the clock",
           len(grid.listings) == 2, repr(grid.listings))
    async with get_session() as session:
        stamp = await crud.get_setting(session, service._post_scan_key(account_id))
    expect("so nothing is recorded for it", stamp is None, repr(stamp))


async def test_the_fallback_says_what_it_did() -> None:
    """A detector nobody can see working is the thing this whole fallback
    exists to replace. "Nothing new" is the common answer and the one that
    has to be visible, or the only proof posts are still watched is a post
    actually arriving — and a source that returns nothing every sweep looks
    exactly the same."""
    from loguru import logger as _logger

    lines: list[str] = []
    sink = _logger.add(lambda m: lines.append(str(m)), level="INFO")
    try:
        # A grid that lists, with nothing new on it.
        account_id = await _new_account("talkative")
        grid = _Grid([_post("t1")])
        service = _service(grid)
        async with get_session() as session:
            await crud.mark_story_items_seen(session, account_id, [_post("t1")])
        lines.clear()
        await service._handle_new_posts(
            account_id, "talkative", ChangeSet(username="talkative"),
            first_seen=False, counts_seen=False,
        )
        expect("a listing with nothing new still reports",
               any("grid listed" in ln and "talkative" in ln for ln in lines),
               repr(lines))

        # A source that answers nothing at all — the case that retries forever.
        quiet_id = await _new_account("mute")
        empty = _service(_Grid([]))
        lines.clear()
        await empty._handle_new_posts(
            quiet_id, "mute", ChangeSet(username="mute"),
            first_seen=False, counts_seen=False,
        )
        expect("and an empty answer is distinguishable from it",
               any("came back empty" in ln for ln in lines), repr(lines))
    finally:
        _logger.remove(sink)


async def test_the_fallback_can_be_turned_off() -> None:
    account_id = await _new_account("optout")
    grid = _Grid([_post("r1")])
    service = _service(grid)
    quiet = ChangeSet(username="optout")
    old = settings.post_scan_interval
    try:
        settings.post_scan_interval = 0
        await service._handle_new_posts(
            account_id, "optout", quiet, first_seen=False, counts_seen=False,
        )
        expect("POST_SCAN_INTERVAL=0 lists nothing", grid.listings == [],
               repr(grid.listings))
    finally:
        settings.post_scan_interval = old


async def test_a_rising_count_still_wins_immediately() -> None:
    """The count is still the trigger when there IS one — it must not have to
    wait out the fallback's interval."""
    account_id = await _new_account("rising")
    grid = _Grid([_post("s1")])
    service = _service(grid)
    async with get_session() as session:  # clock freshly stamped
        await crud.set_setting(
            session, service._post_scan_key(account_id), str(time.time())
        )
    rose = ChangeSet(username="rising", changes=[
        Change(field="posts_count", old=4, new=5, label="posts"),
    ])
    await service._handle_new_posts(
        account_id, "rising", rose, first_seen=False, counts_seen=True,
    )
    expect("a rise lists at once, whatever the fallback clock says",
           grid.listings == ["rising"], repr(grid.listings))


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await test_a_refused_database_does_not_latch_the_sweep_off()
    await test_the_flag_is_released_however_the_sweep_ends()
    await test_a_page_reading_still_finds_new_posts()
    await test_the_fallback_listing_is_paced_not_every_sweep()
    await test_a_failed_listing_is_retried_not_recorded()
    await test_the_fallback_says_what_it_did()
    await test_the_fallback_can_be_turned_off()
    await test_a_rising_count_still_wins_immediately()

    await engine.dispose()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("All silent-failure checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
