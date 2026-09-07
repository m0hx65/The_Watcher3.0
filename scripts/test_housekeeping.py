"""Three small things that were quietly wrong (2026-09-07 audit, P3).

None of these lost data or cost a request. They are here because each one is
the kind of detail that reads as deliberate until someone checks:

1. the shut-door verdict was written and logged TWICE every sweep — once by
   the mid-sweep latch, once again at the end with the same value;
2. a fresh event loop cleared the broker's jobs but left the previous loop's
   waiters and answers behind, so "fresh" was not;
3. one semaphore stood in for two different upstreams, so a card Recheck
   queued behind three accounts' saveinsta media work for no reason.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
DB_FILE = ROOT / "test_housekeeping.db"
if DB_FILE.exists():
    DB_FILE.unlink()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.config import settings  # noqa: E402
from app.database import crud  # noqa: E402
from app.database.models import Base, MonitoredAccount  # noqa: E402
from app.database.session import engine, get_session  # noqa: E402
from app.monitor import home_fetch  # noqa: E402
from app.monitor.home_fetch import HomeFetchBroker, PageResult  # noqa: E402
from app.monitor.instagram import IdProbe, ProfileFetchResult  # noqa: E402
from app.monitor.service import MonitorService  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


# ---------- 1. one verdict, written once ---------------------------------

class _ShutDoor:
    """Every username lookup refused; the page answers."""

    def __init__(self) -> None:
        self.direct_page_door_failing = True

    async def fetch_profile(self, username: str, **kw) -> ProfileFetchResult:
        return ProfileFetchResult(
            username=username, http_status=200, source="public_page",
            api_status=401,
            parsed={"username": username, "followers_count": 10,
                    "following_count": 5, "is_private": True,
                    "instagram_id": "42"},
        )

    async def probe_by_id(self, user_id: str, **kw) -> IdProbe:
        return IdProbe(user_id=user_id, status=401)

    async def fetch_reel_user(self, user_id: str):
        return None

    def reel_in_hand(self, user_id: str):
        return None

    async def fetch_hd_pic_url(self, user_id: str):
        return None


def _service(instagram=None, stories=None) -> MonitorService:
    notifier = AsyncMock()
    notifier.send_text = AsyncMock(return_value=True)
    notifier.send_photo = AsyncMock(return_value=True)
    notifier.create_forum_topic = AsyncMock(return_value=None)
    return MonitorService(
        instagram=instagram or AsyncMock(),
        hasher=AsyncMock(hash_url=AsyncMock(return_value=None)),
        notifier=notifier, stories=stories,
    )


async def _account(username: str) -> int:
    async with get_session() as session:
        account = MonitoredAccount(username=username, active=True, instagram_id="42")
        session.add(account)
        await session.flush()
        return account.id


async def test_the_shut_door_verdict_is_written_once_per_sweep() -> None:
    for name in ("dooru1", "dooru2"):
        await _account(name)
    async with get_session() as session:
        await crud.delete_setting(session, "username_api_closed_at")

    service = _service(_ShutDoor())
    writes: list[tuple[bool, bool]] = []
    real = service._remember_username_api_door

    async def counting(*, closed: bool, answered: bool) -> None:
        writes.append((closed, answered))
        await real(closed=closed, answered=answered)

    service._remember_username_api_door = counting  # type: ignore[assignment]
    await service.check_all()

    expect("the verdict is recorded exactly once", len(writes) == 1, repr(writes))
    expect("and it is the shut one", writes == [(True, False)], repr(writes))
    async with get_session() as session:
        stored = await crud.get_setting(session, "username_api_closed_at")
    expect("and it did reach the database", stored is not None, repr(stored))


async def test_a_sweep_that_never_closes_the_door_still_reports() -> None:
    """The end-of-sweep write is skipped only when the latch already fired —
    a sweep whose door ANSWERED must still be able to clear the verdict."""
    account_id = await _account("openu1")
    async with get_session() as session:
        await crud.set_setting(
            session, "username_api_closed_at",
            datetime.now(timezone.utc).isoformat(),
        )
        for a in await crud.list_accounts(session, only_active=True):
            if a.id != account_id:
                await crud.set_account_active(session, a.username, False)

    class _OpenDoor(_ShutDoor):
        async def fetch_profile(self, username: str, **kw) -> ProfileFetchResult:
            return ProfileFetchResult(
                username=username, http_status=200, api_status=200,
                parsed={"username": username, "followers_count": 10,
                        "following_count": 5, "is_private": True,
                        "instagram_id": "42"},
            )

    service = _service(_OpenDoor())
    await service.check_all()
    async with get_session() as session:
        stored = await crud.get_setting(session, "username_api_closed_at")
    expect("an answering API clears the stored verdict", stored is None,
           repr(stored))


# ---------- 2. a fresh loop starts genuinely fresh ------------------------

def test_a_new_loop_leaves_nothing_of_the_old_one() -> None:
    broker = HomeFetchBroker()

    async def first_loop() -> "asyncio.Future":
        broker._last_poll = time.monotonic()
        broker.prefetch(["ghost"])
        # An answer nobody collected, and a check still waiting on one.
        broker._results[("page", "stale")] = PageResult(200, "old")
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        broker._waiters[("page", "pending")] = [waiter]
        return waiter

    waiter = asyncio.run(first_loop())
    expect("the first loop left state behind",
           broker._results and broker._waiters and broker._jobs)

    async def second_loop() -> None:
        broker._get_queue()

    asyncio.run(second_loop())
    expect("a new loop drops the old loop's jobs", not broker._jobs,
           repr(broker._jobs))
    expect("and its waiters, which nothing could ever resolve",
           not broker._waiters, repr(broker._waiters))
    expect("and its answers, which nobody is asking for any more",
           not broker._results, repr(broker._results))
    expect("the orphaned future is left unresolved, not resolved cross-loop",
           not waiter.done())


# ---------- 3. a check does not queue behind media work ------------------

async def test_a_check_does_not_wait_on_the_story_phase() -> None:
    """The story phase holds its lane for a long body — listings, downloads,
    sends. A check talks to a different service entirely and should not be
    stuck behind it."""
    service = _service()
    expect("the two phases have separate lane budgets",
           service._semaphore is not service._story_semaphore)

    # Fill every story lane, then confirm a check can still take one.
    held = asyncio.Event()
    release = asyncio.Event()

    async def occupy() -> None:
        async with service._story_semaphore:
            held.set()
            await release.wait()

    holders = [
        asyncio.ensure_future(occupy())
        for _ in range(settings.max_concurrent_fetches)
    ]
    await held.wait()
    await asyncio.sleep(0)

    got_lane = False
    try:
        await asyncio.wait_for(service._semaphore.acquire(), timeout=0.5)
        got_lane = True
        service._semaphore.release()
    except asyncio.TimeoutError:
        pass
    finally:
        release.set()
        await asyncio.gather(*holders)
    expect("a check gets a lane while the story phase is saturated", got_lane)


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await test_the_shut_door_verdict_is_written_once_per_sweep()
    await test_a_sweep_that_never_closes_the_door_still_reports()
    await test_a_check_does_not_wait_on_the_story_phase()

    await engine.dispose()


def run() -> int:
    asyncio.run(main())
    # Drives its own event loops, so it has to be outside asyncio.run().
    test_a_new_loop_leaves_nothing_of_the_old_one()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("All housekeeping checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
