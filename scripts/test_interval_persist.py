"""Round-trip the interval through DB + scheduler in-process. No Telegram."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["TELEGRAM_BOT_TOKEN"] = "x"
os.environ["TELEGRAM_CHAT_ID"] = "x"
db_file = Path("./interval_smoke.db").resolve()
if db_file.exists():
    db_file.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file.as_posix()}"

from app.database.models import AppSetting
from app.database.session import dispose_engine, engine
from app.workers.scheduler import (
    WatcherScheduler,
    load_persisted_interval,
    persist_interval,
)


class _NullService:
    async def check_all(self) -> None:
        pass


async def main() -> int:
    # JSONB columns elsewhere don't compile on sqlite. We only need app_settings
    # for this round-trip.
    async with engine.begin() as conn:
        await conn.run_sync(AppSetting.__table__.create)

    # 1) Default — no row yet, should fall back to env (defaults to 1800).
    initial = await load_persisted_interval()
    print(f"initial (no row) = {initial}s")
    assert initial == 1800, initial

    # 2) Persist 900, reload.
    await persist_interval(900)
    after = await load_persisted_interval()
    print(f"after persist(900) = {after}s")
    assert after == 900, after

    # 3) Scheduler picks it up on start, and set_interval re-arms it.
    sched = WatcherScheduler(_NullService())
    await sched.start()
    print(f"scheduler interval_seconds = {sched.interval_seconds}")
    assert sched.interval_seconds == 900

    applied = await sched.set_interval(7200)
    print(f"after set_interval(7200) = {applied}s, scheduler={sched.interval_seconds}")
    assert applied == 7200 and sched.interval_seconds == 7200

    applied = await sched.set_interval(5)  # below MIN_INTERVAL=60
    print(f"clamped set_interval(5) -> {applied}s")
    assert applied == 60

    applied = await sched.set_interval(10**9)  # above MAX_INTERVAL=86400
    print(f"clamped set_interval(huge) -> {applied}s")
    assert applied == 86400

    persisted = await load_persisted_interval()
    print(f"final persisted = {persisted}s")
    assert persisted == 86400

    await sched.shutdown()
    await dispose_engine()
    print("OK")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    raise SystemExit(asyncio.run(main()))
