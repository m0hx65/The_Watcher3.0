"""Regression tests for sweep pacing: the rate-limit guard and the retry rounds.

All of this changes PACING ON FAILURE only — never a request. A sweep checks
one account at a time by default (the same rhythm as a manual Recheck, which is
what Instagram's anonymous gate answers reliably); as consecutive 401/403 blocks
pile up the gap widens, a run of them pauses the sweep outright, and only if the
pauses don't help are the rest deferred — so a burst can't turn 4 blocks into 9.

Anything still blocked afterwards goes through paced retry rounds, because the
block lands on a REQUEST, not an account: that pass is what stops the summary
from naming "failures" the owner can re-check by hand and watch succeed.

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

from app.config import settings  # noqa: E402
from app.monitor import service as service_mod  # noqa: E402
from app.monitor.service import MonitorService, _SweepThrottle  # noqa: E402

# Real cooldowns are 30s+ by design; the retry tests assert the SHAPE of the
# pass (who is re-checked, how often, when it stops), not the wall clock.
service_mod._SWEEP_RETRY_COOLDOWN_SECONDS = 0.01
service_mod._SWEEP_RETRY_COOLDOWN_MAX_SECONDS = 0.02
service_mod._SWEEP_RETRY_GAP_SECONDS = (0.0, 0.0)

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


async def test_slot_spaces_checks() -> None:
    t = _SweepThrottle(base_stagger=0.2, max_stagger=0.2, breaker_threshold=0)
    start = time.monotonic()
    # First check is immediate; each later one waits ~base (+ up to 0.8 jitter)
    # measured from the END of the previous check, not from its launch.
    async with t.slot():
        first = time.monotonic() - start
    async with t.slot():
        second = time.monotonic() - start
    expect("first check is immediate", first < 0.2, f"{first:.3f}s")
    expect("second check is spaced out", second >= 0.2, f"{second:.3f}s")


async def test_slot_serializes_by_default() -> None:
    """Concurrency 1 = one account at a time, the manual-recheck rhythm."""
    t = _SweepThrottle(base_stagger=0.0, max_stagger=0.0, breaker_threshold=0)
    in_flight = 0
    peak = 0

    async def one() -> None:
        nonlocal in_flight, peak
        async with t.slot():
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1

    await asyncio.gather(*(one() for _ in range(5)))
    expect("default sweep never runs two checks at once", peak == 1, f"peak={peak}")

    t2 = _SweepThrottle(
        base_stagger=0.0, max_stagger=0.0, breaker_threshold=0, concurrency=3
    )
    in_flight = peak = 0

    async def two() -> None:
        nonlocal in_flight, peak
        async with t2.slot():
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1

    await asyncio.gather(*(two() for _ in range(6)))
    expect("configured concurrency is honored", peak == 3, f"peak={peak}")


async def test_guard_pauses_before_it_defers() -> None:
    """A run of blocks pauses the sweep; only a run that survives every pause
    opens the breaker and defers the rest."""
    t = _SweepThrottle(
        base_stagger=0.0, max_stagger=0.0, breaker_threshold=2,
        cooldown=0.3, max_pauses=1,
    )
    t.record(401)
    t.record(401)  # hits the threshold -> pause, NOT open
    expect("first block run pauses instead of deferring", not t.is_open())
    expect("the pause is counted", t.pauses == 1, repr(t.pauses))

    # The pause is a real gap: the next slot waits it out.
    start = time.monotonic()
    async with t.slot():
        waited = time.monotonic() - start
    expect("the next check waits out the cooldown", waited >= 0.3, f"{waited:.3f}s")

    t.record(401)
    t.record(401)  # pauses are spent -> now it opens
    expect("a second block run opens the breaker", t.is_open())
    expect("pause count did not grow past the max", t.pauses == 1, repr(t.pauses))


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


async def test_retry_rounds_recover_blocked_accounts() -> None:
    """The 401 that a sweep reports is usually transient: a paced re-check gets
    a 200. Without this pass the sweep names accounts as failed that the owner
    can re-check by hand a minute later and see work fine."""
    service = MonitorService(
        instagram=AsyncMock(), hasher=AsyncMock(),
        notifier=AsyncMock(), stories=None,
    )
    # @flaky answers on the 2nd retry, @hard never does.
    attempts: dict[str, int] = {}

    async def fake_run_check(account_id, username, **kw):
        attempts[username] = attempts.get(username, 0) + 1
        if username == "flaky" and attempts[username] >= 2:
            return {"ok": True, "username": username, "status": 200}
        return {"ok": False, "username": username, "status": 401, "error": "blocked"}

    service._run_check = fake_run_check  # type: ignore[assignment]

    outcomes = [
        (1, "good", {"ok": True, "username": "good", "status": 200}),
        (2, "flaky", {"ok": False, "username": "flaky", "status": 401}),
        (3, "hard", {"ok": False, "username": "hard", "status": 401}),
        (4, "gone", {"ok": False, "username": "gone", "status": 404}),
    ]
    settings.sweep_retry_rounds = 2
    try:
        recovered = await service._retry_blocked(outcomes)
    finally:
        settings.sweep_retry_rounds = 3

    expect("the transient block recovers", recovered == 1, repr(recovered))
    expect("the recovered account is marked ok in place",
           outcomes[1][2].get("ok") is True, repr(outcomes[1][2]))
    expect("a real block stays failed", outcomes[2][2].get("ok") is False)
    expect("a 404 is never retried (rename recovery handles it)",
           "gone" not in attempts, repr(attempts))
    expect("a healthy account is never re-checked", "good" not in attempts)
    expect("retries stop once an account recovers", attempts["flaky"] == 2,
           repr(attempts))
    expect("a hard block is retried every round", attempts["hard"] == 2,
           repr(attempts))


async def test_retry_budget_stops_a_long_outage() -> None:
    """A genuine outage must not stretch the sweep past its wall-clock budget."""
    service = MonitorService(
        instagram=AsyncMock(), hasher=AsyncMock(),
        notifier=AsyncMock(), stories=None,
    )

    async def always_blocked(account_id, username, **kw):
        return {"ok": False, "username": username, "status": 401}

    service._run_check = always_blocked  # type: ignore[assignment]
    outcomes = [(i, f"u{i}", {"ok": False, "username": f"u{i}", "status": 401})
                for i in range(3)]

    settings.sweep_retry_budget_seconds = 0  # no room for even one round
    try:
        started = time.monotonic()
        recovered = await service._retry_blocked(outcomes)
    finally:
        settings.sweep_retry_budget_seconds = 300
    expect("a spent budget retries nothing", recovered == 0)
    expect("a spent budget returns immediately",
           time.monotonic() - started < 0.5)


async def main() -> int:
    test_healthy_sweep_stays_at_base()
    test_stagger_widens_then_relaxes()
    test_stagger_caps_at_max()
    test_breaker_opens_at_threshold()
    test_success_resets_the_streak()
    await test_slot_spaces_checks()
    await test_slot_serializes_by_default()
    await test_guard_pauses_before_it_defers()
    await test_retry_rounds_recover_blocked_accounts()
    await test_retry_budget_stops_a_long_outage()
    await test_staggered_check_defers_after_open()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All sweep-breaker tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
