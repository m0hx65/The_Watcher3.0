"""Scheduler test: stakeout lifecycle — start, bounds, persist, restore, tick."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_stakeout.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")
os.environ["STAKEOUT_MIN_INTERVAL"] = "120"
os.environ["STAKEOUT_MAX_DURATION"] = "21600"

from app.config import settings  # noqa: E402
from app.database.models import Base  # noqa: E402
from app.database.session import dispose_engine, engine, get_session  # noqa: E402
from app.workers.scheduler import (  # noqa: E402
    SETTING_STAKEOUTS,
    WatcherScheduler,
    _stakeout_job_id,
)
from app.database import crud  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, cond: bool, detail: str = "") -> None:
    print(("ok" if cond else "FAIL") + f": {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def make_scheduler():
    service = SimpleNamespace(
        notifier=SimpleNamespace(send_text=AsyncMock(return_value=True)),
        check_username=AsyncMock(return_value={"ok": True}),
    )
    sched = WatcherScheduler(service)
    return sched, service


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sched, service = make_scheduler()
    sched.scheduler.start()  # APScheduler only; not the full WatcherScheduler.start

    # 1. Interval floor: request below the minimum, get clamped up.
    info = await sched.start_stakeout(1, "target", interval=5, duration=600)
    expect("interval floored to min", info["interval"] == settings.stakeout_min_interval,
           str(info["interval"]))
    expect("job was scheduled", sched.scheduler.get_job(_stakeout_job_id(1)) is not None)
    expect("stakeout_for finds it", sched.stakeout_for(1) is not None)
    expect("active_stakeouts has one", len(sched.active_stakeouts()) == 1)

    # 2. Duration cap.
    info2 = await sched.start_stakeout(2, "other", interval=180, duration=999999)
    dur = (info2["end"] - datetime.now(timezone.utc)).total_seconds()
    expect("duration capped to max", dur <= settings.stakeout_max_duration + 5, str(dur))

    # 3. Persisted to app_settings.
    async with get_session() as session:
        raw = await crud.get_setting(session, SETTING_STAKEOUTS)
    expect("persisted to settings", raw is not None and "target" in raw)

    # 4. Tick on a still-active stakeout runs a check.
    service.check_username.reset_mock()
    await sched._stakeout_tick(1)
    expect("active tick runs check_username", service.check_username.await_count == 1)

    # 5. Tick on an expired stakeout stops it and notifies.
    sched._stakeouts[1]["end"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    service.notifier.send_text.reset_mock()
    await sched._stakeout_tick(1)
    expect("expired tick removed the job", sched.scheduler.get_job(_stakeout_job_id(1)) is None)
    expect("expired tick cleared state", sched.stakeout_for(1) is None)
    expect("expired tick notified", service.notifier.send_text.await_count == 1)

    # 6. stop_stakeout on the remaining one.
    stopped = await sched.stop_stakeout(2)
    expect("stop returns True", stopped is True)
    expect("no stakeouts left", len(sched.active_stakeouts()) == 0)

    sched.scheduler.shutdown(wait=False)

    # 7. Restore after a "restart": persist one with a future end, new scheduler restores it.
    future = datetime.now(timezone.utc) + timedelta(seconds=600)
    past = datetime.now(timezone.utc) - timedelta(seconds=10)
    import json
    async with get_session() as session:
        await crud.set_setting(session, SETTING_STAKEOUTS, json.dumps([
            {"account_id": 7, "username": "survivor", "interval": 180, "end": future.isoformat()},
            {"account_id": 8, "username": "expired", "interval": 180, "end": past.isoformat()},
        ]))

    sched2, _ = make_scheduler()
    sched2.scheduler.start()
    await sched2._restore_stakeouts()
    expect("future stakeout restored", sched2.stakeout_for(7) is not None)
    expect("expired stakeout dropped", sched2.stakeout_for(8) is None)
    expect("restored job scheduled", sched2.scheduler.get_job(_stakeout_job_id(7)) is not None)
    sched2.scheduler.shutdown(wait=False)

    await dispose_engine()
    if DB_FILE.exists():
        DB_FILE.unlink()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {FAILURES}")
        return 1
    print("\nall good")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
