"""Three ways work quietly went missing (2026-09-07 audit, P2).

None of these announced themselves. A story vanished, a request went out
where the pacer could not see it, and a whole sweep could stop mid-flight
with its traceback going nowhere.

1. **A failed download retired the story for good.** One saveinsta hiccup and
   the item was marked seen — although the next sweep, half an hour later and
   well inside the 24 hours a story lives, would very likely have got it.
2. **Off-schedule checks bypassed the pacer.** `_SweepThrottle` only knows
   about its own sweep, so a stakeout ticking every two minutes fired unpaced
   requests into the middle of a paced one and the burst guard never saw them.
3. **Background tasks were unreferenced.** The loop keeps only a weak
   reference to a task, so one nobody holds can be collected mid-flight — and
   a sweep launched from the button or `POST /sweep` ran exactly that way,
   with any exception surfacing only as a GC warning, if at all.

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
DB_FILE = ROOT / "test_dropped_work.db"
if DB_FILE.exists():
    DB_FILE.unlink()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.database import crud  # noqa: E402
from app.database.models import Base, MonitoredAccount  # noqa: E402
from app.database.session import engine, get_session  # noqa: E402
from app.monitor.service import (  # noqa: E402
    _DOWNLOAD_ATTEMPTS, _OFF_SCHEDULE_MIN_GAP_SECONDS, MonitorService,
)
from app.monitor.stories import StoryItem  # noqa: E402
from app.utils import tasks  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def _item(pk: str) -> StoryItem:
    return StoryItem(pk=pk, taken_at=0, media_type="image",
                     url=f"https://dl.snapcdn.app/{pk}", source="story")


class _Stories:
    """Downloads fail until `working` is set."""

    def __init__(self) -> None:
        self.working = False
        self.attempts: list[str] = []

    async def download(self, item, username):
        self.attempts.append(item.pk)
        return Path("/tmp/whatever.jpg") if self.working else None


def _service(stories=None) -> MonitorService:
    notifier = AsyncMock()
    notifier.send_text = AsyncMock(return_value=True)
    notifier.send_photo = AsyncMock(return_value=True)
    notifier.send_video = AsyncMock(return_value=True)
    notifier.create_forum_topic = AsyncMock(return_value=None)
    return MonitorService(
        instagram=AsyncMock(), hasher=AsyncMock(), notifier=notifier,
        stories=stories,
    )


async def _account(username: str) -> int:
    async with get_session() as session:
        account = MonitoredAccount(username=username, active=True)
        session.add(account)
        await session.flush()
        return account.id


# ---------- 1. a hiccup must not retire a story ---------------------------

async def test_a_failed_download_is_retried_before_being_given_up_on() -> None:
    account_id = await _account("flaky")
    stories = _Stories()
    service = _service(stories)

    seen: set[str] = set()
    await service._deliver_story_items(account_id, "flaky", [_item("s1")], seen)
    expect("one failure does not mark the story seen", "s1" not in seen, repr(seen))
    async with get_session() as session:
        stored = await crud.get_seen_story_pks(session, account_id)
    expect("and nothing is persisted for it", "s1" not in stored, repr(stored))

    # The next sweep gets it.
    stories.working = True
    seen = set()
    sent = await service._deliver_story_items(
        account_id, "flaky", [_item("s1")], seen
    )
    expect("so the next check delivers it", sent == 1, repr(sent))
    async with get_session() as session:
        stored = await crud.get_seen_story_pks(session, account_id)
    expect("and only then is it recorded", "s1" in stored, repr(stored))


async def test_a_permanently_broken_item_is_eventually_retired() -> None:
    """The other half: an item that will never download must stop being
    asked for, or every sweep re-tries it forever."""
    account_id = await _account("broken")
    stories = _Stories()
    service = _service(stories)

    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        seen: set[str] = set()
        await service._deliver_story_items(
            account_id, "broken", [_item("b1")], seen
        )
        async with get_session() as session:
            stored = await crud.get_seen_story_pks(session, account_id)
        retired = "b1" in stored
        if attempt < _DOWNLOAD_ATTEMPTS:
            expect(f"attempt {attempt} leaves it for next time", not retired,
                   repr(stored))
        else:
            expect(f"attempt {attempt} retires it", retired, repr(stored))
    expect("and it was actually re-attempted each time",
           len(stories.attempts) == _DOWNLOAD_ATTEMPTS, repr(stories.attempts))


async def test_an_unmonitored_account_has_no_next_check_to_retry_on() -> None:
    """An ad-hoc /story for an account nobody monitors persists nothing, so
    there is no later sweep — retrying would just repeat inside this call."""
    stories = _Stories()
    service = _service(stories)
    seen: set[str] = set()
    await service._deliver_story_items(None, "stranger", [_item("x1")], seen)
    expect("it gives up at once rather than pretending it will retry",
           "x1" in seen, repr(seen))


# ---------- 2. nothing outruns the pacer ---------------------------------

async def _instant_check(service: MonitorService) -> None:
    async def _do_check(account_id, username, notify_unchanged, **kw):
        return {"ok": True, "username": username}
    service._do_check = _do_check  # type: ignore[assignment]


async def test_off_schedule_checks_are_spaced() -> None:
    service = _service()
    await _instant_check(service)

    started = time.monotonic()
    await service._run_check(1, "a")
    await service._run_check(2, "b")
    gap = time.monotonic() - started
    expect("a second off-schedule check waits out the gap",
           gap >= _OFF_SCHEDULE_MIN_GAP_SECONDS - 0.05, f"{gap:.2f}s")

    # Two coming due at the same instant must leave one after the other.
    service2 = _service()
    await _instant_check(service2)
    started = time.monotonic()
    await asyncio.gather(
        service2._run_check(1, "a"), service2._run_check(2, "b"),
    )
    together = time.monotonic() - started
    expect("and two firing at once do not leave together",
           together >= _OFF_SCHEDULE_MIN_GAP_SECONDS - 0.05, f"{together:.2f}s")


async def test_a_swept_check_paces_itself_and_still_stamps_the_clock() -> None:
    """The sweep has its own throttle, so it must not be double-paced — but
    its traffic still has to be visible to the gate, or a stakeout could land
    on top of a sweep check."""
    service = _service()
    await _instant_check(service)

    started = time.monotonic()
    await service._run_check(1, "a", paced=True)
    await service._run_check(2, "b", paced=True)
    swept = time.monotonic() - started
    expect("sweep checks are not held back by the off-schedule gate",
           swept < _OFF_SCHEDULE_MIN_GAP_SECONDS, f"{swept:.2f}s")

    started = time.monotonic()
    await service._run_check(3, "c")  # off-schedule, right after a sweep check
    after = time.monotonic() - started
    expect("but an off-schedule check still waits for the sweep's own traffic",
           after >= _OFF_SCHEDULE_MIN_GAP_SECONDS - 0.05, f"{after:.2f}s")


# ---------- 3. a background task is held, and reports itself -------------

async def test_a_background_task_is_held_until_it_finishes() -> None:
    done = asyncio.Event()

    async def work() -> None:
        await asyncio.sleep(0.05)
        done.set()

    before = tasks.pending()
    task = tasks.spawn(work(), name="test:held")
    expect("the task is referenced while it runs",
           tasks.pending() == before + 1, repr(tasks.pending()))
    await task
    await asyncio.sleep(0)
    expect("it ran to completion", done.is_set())
    expect("and the reference is released afterwards",
           tasks.pending() == before, repr(tasks.pending()))


async def test_a_failing_background_task_does_not_disappear_quietly() -> None:
    async def boom() -> None:
        raise RuntimeError("sweep fell over")

    before = tasks.pending()
    task = tasks.spawn(boom(), name="test:boom")
    await asyncio.sleep(0.05)
    expect("the failure does not propagate into the caller", task.done())
    expect("the exception is retrieved rather than left for the collector",
           isinstance(task.exception(), RuntimeError), repr(task.exception()))
    expect("and the reference is released", tasks.pending() == before,
           repr(tasks.pending()))


async def test_a_cancelled_task_is_released_too() -> None:
    async def forever() -> None:
        await asyncio.sleep(30)

    before = tasks.pending()
    task = tasks.spawn(forever(), name="test:cancelled")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0)
    expect("a cancelled task leaves no reference behind",
           tasks.pending() == before, repr(tasks.pending()))


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await test_a_failed_download_is_retried_before_being_given_up_on()
    await test_a_permanently_broken_item_is_eventually_retired()
    await test_an_unmonitored_account_has_no_next_check_to_retry_on()
    await test_off_schedule_checks_are_spaced()
    await test_a_swept_check_paces_itself_and_still_stamps_the_clock()
    await test_a_background_task_is_held_until_it_finishes()
    await test_a_failing_background_task_does_not_disappear_quietly()
    await test_a_cancelled_task_is_released_too()

    await engine.dispose()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("All dropped-work checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
