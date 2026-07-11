"""Regression tests for the digest roll-up.

Covers the pure renderer (grouping, per-sweep-status exclusion, empty window),
the crud window query, and the scheduler orchestration (mode gating, weekly
weekday gate, since-window, and the last-digest marker advancing). Reads only
the already-logged NotificationLog — no new tracking storage.

Runs offline on sqlite with a fake notifier — no Telegram, no network.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_digest.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.bot.notifications import render_digest  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import crud  # noqa: E402
from app.database.models import Base, MonitoredAccount, NotificationLog  # noqa: E402
from app.database.session import engine, get_session  # noqa: E402
from app.monitor.service import MonitorService  # noqa: E402
from app.workers.scheduler import (  # noqa: E402
    SETTING_DIGEST_LAST_AT,
    WatcherScheduler,
)

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str, **kwargs) -> bool:
        self.sent.append(text)
        return True


def _note(change_type: str) -> NotificationLog:
    return NotificationLog(
        account_id=1, change_type=change_type, payload=None,
        message="x", delivered=True,
    )


def test_render_digest() -> None:
    since = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    rows = [
        (_note("followers_count"), "alpha"),
        (_note("followers_count"), "alpha"),
        (_note("biography"), "alpha"),
        (_note("story_status"), "alpha"),   # excluded heartbeat
        (_note("story_status"), "alpha"),   # excluded heartbeat
        (_note("profile_picture"), "beta"),
        (_note("follower_anomaly"), "beta"),
    ]
    text = render_digest(rows, since=since)
    expect("digest has a header", "Digest" in text, text)
    expect("alpha listed", "@alpha" in text, text)
    expect("beta listed", "@beta" in text, text)
    expect("repeated type is aggregated (×2)", "followers ×2" in text, text)
    expect("bio labelled", "bio" in text, text)
    expect("anomaly labelled", "follower jump" in text, text)
    # story_status heartbeats are excluded, so alpha shows 3 events (2 followers
    # + 1 bio), NOT 5, and beta shows 2.
    expect("story_status excluded from counts", "— 3:" in text, text)
    # alpha (3 events) sorts before beta (2 events).
    expect("busiest account sorts first",
           text.index("@alpha") < text.index("@beta"), text)
    total_line_ok = "5 events across 2 accounts" in text
    expect("event/account totals exclude heartbeats", total_line_ok, text)


def test_render_empty() -> None:
    since = datetime(2026, 7, 10, 9, 0, tzinfo=timezone.utc)
    text = render_digest([], since=since)
    expect("empty window says nothing to report",
           "No changes to report" in text, text)
    # A window with ONLY excluded heartbeats is also 'nothing to report'.
    only_noise = [(_note("story_status"), "alpha")]
    expect("heartbeat-only window is empty too",
           "No changes to report" in render_digest(only_noise, since=since))


async def test_notifications_since() -> None:
    async with get_session() as session:
        session.add(MonitoredAccount(id=1, username="alpha", active=True))

    now = datetime.now(timezone.utc)
    async with get_session() as session:
        # One old row (outside a 24h window), two recent.
        session.add(NotificationLog(
            account_id=1, change_type="biography", payload=None,
            message="old", delivered=True,
            created_at=now - timedelta(days=3),
        ))
        session.add(NotificationLog(
            account_id=1, change_type="followers_count", payload=None,
            message="recent1", delivered=True, created_at=now - timedelta(hours=2),
        ))
        session.add(NotificationLog(
            account_id=1, change_type="profile_picture", payload=None,
            message="recent2", delivered=True, created_at=now - timedelta(hours=1),
        ))

    async with get_session() as session:
        rows = await crud.notifications_since(session, now - timedelta(hours=24))
    expect("window query excludes the old row", len(rows) == 2, f"{len(rows)} rows")
    expect("window rows carry the username",
           all(u == "alpha" for _, u in rows))


async def test_scheduler_run_digest() -> None:
    notifier = FakeNotifier()
    service = MonitorService(
        instagram=AsyncMock(), hasher=AsyncMock(), notifier=notifier, stories=None,
    )
    sched = WatcherScheduler(service)

    # Mode off → nothing sent.
    await sched.set_digest_mode("off")
    res = await sched.run_digest()
    expect("mode off sends nothing", res["sent"] is False and not notifier.sent)

    # Daily → sends and advances the marker.
    await sched.set_digest_mode("daily")
    res = await sched.run_digest()
    expect("daily digest sends", res["sent"] is True and len(notifier.sent) == 1,
           repr(res))
    async with get_session() as session:
        marker = await crud.get_setting(session, SETTING_DIGEST_LAST_AT)
    expect("last-digest marker was written", marker is not None)

    # Weekly gate: only fires on the configured weekday (scheduled path).
    now = datetime.now(timezone.utc)
    notifier.sent.clear()
    await sched.set_digest_mode("weekly")
    original_weekday = settings.digest_weekday
    try:
        settings.digest_weekday = (now.weekday() + 1) % 7  # NOT today
        res = await sched.run_digest()
        expect("weekly skips on the wrong weekday", res["sent"] is False)
        settings.digest_weekday = now.weekday()  # today
        res = await sched.run_digest()
        expect("weekly fires on the right weekday", res["sent"] is True)
    finally:
        settings.digest_weekday = original_weekday

    # force_mode bypasses both the off-check and the weekday gate.
    await sched.set_digest_mode("off")
    notifier.sent.clear()
    res = await sched.run_digest(force_mode="daily")
    expect("force_mode sends even when mode is off",
           res["sent"] is True and len(notifier.sent) == 1)


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    test_render_digest()
    test_render_empty()
    await test_notifications_since()
    await test_scheduler_run_digest()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All digest tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
