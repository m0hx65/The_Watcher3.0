"""Regression tests for the sweep adaptive stagger + 401 circuit breaker.

The breaker changes PACING ON FAILURE only — it never alters a request. On a
healthy sweep it behaves like the old fixed stagger; as consecutive 401/403
blocks pile up the launch gap widens, and past a threshold the remaining
accounts are deferred (returned as retriable failures) so a burst can't turn 4
blocks into 9.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.monitor.service import MonitorService, _SweepThrottle  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def test_healthy_sweep_stays_at_base() -> None:
    t = _SweepThrottle(base_stagger=2.0, max_stagger=12.0, breaker_threshold=5)
    for _ in range(10):
        t.record(200)
    expect("all-200 sweep never opens the breaker", not t.is_open())
    expect("all-200 sweep keeps the base stagger", t.current_stagger == 2.0,
           repr(t.current_stagger))
    expect("no accounts skipped on a healthy sweep", t.skipped == 0)


def test_stagger_widens_then_relaxes() -> None:
    t = _SweepThrottle(base_stagger=2.0, max_stagger=12.0, breaker_threshold=0)
    base = t.current_stagger
    t.record(401)
    after_one = t.current_stagger
    t.record(401)
    after_two = t.current_stagger
    expect("stagger widens after a block", after_one > base, f"{base}->{after_one}")
    expect("stagger keeps widening", after_two > after_one)
    expect("stagger is capped at max", t.current_stagger <= 12.0)
    # A success relaxes it back toward base.
    t.record(200)
    expect("stagger relaxes after a success", t.current_stagger < after_two)
    # breaker_threshold=0 disables the breaker entirely.
    for _ in range(20):
        t.record(401)
    expect("threshold 0 never opens the breaker", not t.is_open())


def test_stagger_caps_at_max() -> None:
    t = _SweepThrottle(base_stagger=2.0, max_stagger=5.0, breaker_threshold=0)
    for _ in range(50):
        t.record(401)
    expect("widened stagger never exceeds max", t.current_stagger == 5.0,
           repr(t.current_stagger))


def test_breaker_opens_at_threshold() -> None:
    t = _SweepThrottle(base_stagger=1.0, max_stagger=8.0, breaker_threshold=4)
    for i in range(3):
        t.record(401)
        expect(f"closed after {i + 1} blocks", not t.is_open())
    t.record(401)  # 4th consecutive block
    expect("breaker opens on the 4th consecutive block", t.is_open())
    expect("peak consecutive tracked", t.peak_consecutive_blocks == 4)


def test_success_resets_the_streak() -> None:
    t = _SweepThrottle(base_stagger=1.0, max_stagger=8.0, breaker_threshold=3)
    t.record(401)
    t.record(401)
    t.record(200)  # streak broken before the breaker could open
    t.record(401)
    expect("a success resets the consecutive-block streak", not t.is_open())
    # 404 / 429 / 0 don't count toward the breaker (not the datacenter block).
    t2 = _SweepThrottle(base_stagger=1.0, max_stagger=8.0, breaker_threshold=2)
    t2.record(404)
    t2.record(429)
    t2.record(0)
    expect("non-auth statuses don't trip the breaker", not t2.is_open())


async def test_await_slot_spaces_launches() -> None:
    t = _SweepThrottle(base_stagger=0.2, max_stagger=0.2, breaker_threshold=0)
    start = time.monotonic()
    # First launch is immediate; each subsequent one waits ~base (+ up to 0.8 jitter).
    await t.await_slot()
    first = time.monotonic() - start
    await t.await_slot()
    second = time.monotonic() - start
    expect("first launch is immediate", first < 0.2, f"{first:.3f}s")
    expect("second launch is spaced out", second >= 0.2, f"{second:.3f}s")


async def test_staggered_check_defers_after_open() -> None:
    # A MonitorService whose _run_check is stubbed to a scripted status, so we
    # can drive the breaker without any DB or network.
    service = MonitorService(
        instagram=AsyncMock(), hasher=AsyncMock(),
        notifier=AsyncMock(), stories=None,
    )
    calls: list[str] = []

    async def fake_run_check(account_id, username, **kw):
        calls.append(username)
        return {"ok": False, "username": username, "status": 401, "error": "blocked"}

    service._run_check = fake_run_check  # type: ignore[assignment]

    t = _SweepThrottle(base_stagger=0.0, max_stagger=0.0, breaker_threshold=3)
    results = []
    for i in range(6):
        results.append(await service._staggered_check(t, i, f"user{i}"))

    expect("breaker opened during the run", t.is_open())
    # First 3 actually fetched (they were the blocks that opened it); the rest
    # are deferred WITHOUT calling _run_check.
    expect("only pre-breaker accounts hit the network", len(calls) == 3,
           f"calls={calls}")
    deferred = [r for r in results if r.get("skipped")]
    expect("post-breaker accounts are deferred", len(deferred) == 3,
           f"{len(deferred)} deferred")
    expect("deferred accounts look retriable (status 401)",
           all(r["status"] == 401 for r in deferred))
    expect("throttle counted the skips", t.skipped == 3, repr(t.skipped))


async def main() -> int:
    test_healthy_sweep_stays_at_base()
    test_stagger_widens_then_relaxes()
    test_stagger_caps_at_max()
    test_breaker_opens_at_threshold()
    test_success_resets_the_streak()
    await test_await_slot_spaces_launches()
    await test_staggered_check_defers_after_open()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All sweep-breaker tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
