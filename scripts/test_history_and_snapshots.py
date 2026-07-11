"""Tests for crud.get_previous_snapshot and the /history HTML-escaping fix.

- get_previous_snapshot returns the most recent snapshot OTHER than a given id,
  with an id tiebreaker so colliding whole-second timestamps are deterministic.
- _render_history_message must truncate raw text BEFORE escaping, so a long
  value full of HTML-special chars can never leave a sliced "&amp;" entity that
  Telegram would reject.

Runs offline on sqlite.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_history_and_snapshots.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.bot import handlers  # noqa: E402
from app.database import crud  # noqa: E402
from app.database.models import (  # noqa: E402
    AccountSnapshot,
    Base,
    MonitoredAccount,
)
from app.database.session import engine, get_session  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


async def test_get_previous_snapshot() -> None:
    async with get_session() as session:
        session.add(MonitoredAccount(id=1, username="alpha", active=True))

    # Three snapshots. #2 and #3 share a created_at (whole-second collision) to
    # exercise the id tiebreaker.
    same_ts = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    async with get_session() as session:
        session.add(AccountSnapshot(
            id=1, account_id=1, username="alpha", http_status=200,
            created_at=datetime(2026, 7, 11, 11, 0, 0, tzinfo=timezone.utc),
        ))
        session.add(AccountSnapshot(
            id=2, account_id=1, username="alpha", http_status=200,
            created_at=same_ts,
        ))
        session.add(AccountSnapshot(
            id=3, account_id=1, username="alpha", http_status=200,
            created_at=same_ts,
        ))

    async with get_session() as session:
        latest = await crud.get_latest_snapshot(session, 1)
        prev = await crud.get_previous_snapshot(session, 1, latest.id)

    expect("latest is the highest id on a ts tie", latest.id == 3, repr(latest.id))
    expect("previous excludes the current row", prev is not None and prev.id != 3)
    expect("previous is the next-newest by id tiebreaker",
           prev is not None and prev.id == 2, repr(prev.id if prev else None))

    # Excluding the only-remaining row eventually yields None.
    async with get_session() as session:
        only = await crud.get_previous_snapshot(session, 1, 1)
    expect("previous of the oldest still finds a newer row", only is not None)


async def test_history_escapes_safely() -> None:
    async with get_session() as session:
        session.add(MonitoredAccount(id=2, username="htmluser", active=True))
    # A payload whose raw 'old' is long AND full of '&' — the exact case where
    # truncating AFTER escaping would slice a "&amp;" entity in half.
    async with get_session() as session:
        await crud.log_notification(
            session, account_id=2, change_type="biography",
            payload={"old": "&" * 300, "new": "clean"},
            message="x", delivered=True,
        )
        await crud.log_notification(
            session, account_id=2, change_type="full_name",
            payload={"old": "a & b", "new": "c <d> e"},
            message="x", delivered=True,
        )

    text = await handlers._render_history_message("htmluser")

    # The core invariant: every '&' in the output starts a COMPLETE entity. A
    # sliced entity (the old escape-then-truncate bug) would leave a dangling
    # '&' not followed by a valid entity — which is what Telegram rejects.
    bad_amp = re.findall(r"&(?!amp;|lt;|gt;|quot;|#\d+;)", text)
    expect(
        "no sliced/dangling HTML entity in history output",
        not bad_amp,
        f"{len(bad_amp)} bad ampersand(s)",
    )
    expect("special chars are escaped", "&lt;d&gt;" in text, text[-200:])
    expect("the 300-& value became complete &amp; entities",
           "&amp;" in text and "&am " not in text and not text.endswith("&am"))
    expect("history has the header", "Recent changes" in text)


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await test_get_previous_snapshot()
    await test_history_escapes_safely()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All history/snapshot tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
