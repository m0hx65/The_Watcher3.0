"""Service test: went-dark radar fires once on going dark and once on return."""

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

DB_FILE = ROOT / "test_dark_radar.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")
os.environ["DARK_RADAR_DAYS"] = "3"

from app.database.models import Base, MonitoredAccount, SeenStory  # noqa: E402
from app.database.session import dispose_engine, engine, get_session  # noqa: E402
from app.monitor.service import MonitorService  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, cond: bool, detail: str = "") -> None:
    print(("ok" if cond else "FAIL") + f": {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


async def add_seen(account_id: int, pk: str, when: datetime) -> None:
    async with get_session() as session:
        session.add(SeenStory(
            account_id=account_id, story_pk=pk, source="story",
            media_type="image", taken_at=0, seen_at=when,
        ))


def count_calls(mock: AsyncMock, needle: str) -> int:
    return sum(1 for c in mock.call_args_list if needle in (c.args[0] if c.args else ""))


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with get_session() as session:
        session.add(MonitoredAccount(id=1, username="darko", active=True))
        session.add(MonitoredAccount(id=2, username="freshie", active=True))

    notifier = AsyncMock()
    notifier.send_text = AsyncMock(return_value=True)
    service = MonitorService(
        instagram=AsyncMock(), hasher=AsyncMock(), notifier=notifier, stories=None
    )

    now = datetime.now(timezone.utc)
    # darko's last activity was 5 days ago → should be flagged dark.
    await add_seen(1, "old1", now - timedelta(days=5))
    # freshie posted an hour ago → should stay green.
    await add_seen(2, "new1", now - timedelta(hours=1))

    await service._check_dark_radar()
    expect("went-dark alert fired for darko", count_calls(notifier.send_text, "gone dark") == 1)
    expect("no dark alert for freshie", count_calls(notifier.send_text, "@freshie") == 0)

    # Second sweep, nothing changed → must NOT re-alert (state remembered).
    notifier.send_text.reset_mock()
    await service._check_dark_radar()
    expect("no duplicate dark alert", count_calls(notifier.send_text, "gone dark") == 0)

    # darko posts again → should clear and announce the comeback once.
    await add_seen(1, "back1", now)
    notifier.send_text.reset_mock()
    await service._check_dark_radar()
    expect("comeback alert fired", count_calls(notifier.send_text, "active again") == 1)

    # And not again on the next sweep.
    notifier.send_text.reset_mock()
    await service._check_dark_radar()
    expect("no duplicate comeback", count_calls(notifier.send_text, "active again") == 0)

    # Report lists both, quietest first, freshie green.
    report = await service.dark_radar_report()
    usernames = [r["username"] for r in report["accounts"]]
    expect("report covers both accounts", set(usernames) == {"darko", "freshie"}, str(usernames))
    expect("report threshold is 3", report["threshold_days"] == 3)

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
