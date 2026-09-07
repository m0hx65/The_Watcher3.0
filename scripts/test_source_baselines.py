"""A door's baseline must survive the outage that shut it (2026-09-07 audit).

Each source is diffed against its OWN history — the API and the public page
disagree about the same account at the same moment, so alternating between
them "detected" a change on every flip. `get_latest_snapshot_by_source`
promises that "when the API comes back it diffs against the last API reading,
so nothing that happened during an outage is lost."

Three separate mechanisms broke that promise, and all three only bite after
the outage has run for a while — which is why none of them ever showed up:

1. **The marker was destroyed.** `raw_response` is nulled after
   RAW_RESPONSE_RETENTION_DAYS, and the door marker lived inside it. A
   marker-less row reads as the API's, so every page snapshot older than a
   week started impersonating an API one — worse than losing the baseline,
   because the returning API check would diff its numbers against the page's.
2. **The scan was 25 rows deep.** Once this door had not answered for 25
   changes its last row fell outside the window, and a returning door got no
   baseline at all — then silently established one.
3. **The purge kept one row per ACCOUNT**, not per door. After
   SNAPSHOT_RETENTION_DAYS the last API row was simply deleted.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
DB_FILE = ROOT / "test_source_baselines.db"
if DB_FILE.exists():
    DB_FILE.unlink()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.database import crud  # noqa: E402
from app.database.models import AccountSnapshot, Base, MonitoredAccount  # noqa: E402
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


async def _account(username: str) -> int:
    async with get_session() as session:
        account = MonitoredAccount(username=username, active=True)
        session.add(account)
        await session.flush()
        return account.id


async def _snapshot(
    account_id: int, username: str, *, source: Optional[str],
    followers: int, age_days: float = 0.0, status: int = 200,
) -> int:
    """One stored reading from a given door, aged by `age_days`."""
    raw: dict = {"data": {"user": {"id": "42"}}}
    if source:
        raw["source"] = source
    async with get_session() as session:
        snap = AccountSnapshot(
            account_id=account_id, username=username, http_status=status,
            followers_count=followers, following_count=5, is_private=False,
            raw_response=raw,
            created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        )
        session.add(snap)
        await session.flush()
        return snap.id


# ---------- 1. the marker must survive the payload being stripped ---------

async def test_stripping_raw_response_keeps_the_door_marker() -> None:
    account_id = await _account("stripped")
    page_id = await _snapshot(account_id, "stripped", source="public_page",
                              followers=100, age_days=30)
    api_id = await _snapshot(account_id, "stripped", source=None,
                             followers=90, age_days=40)

    async with get_session() as session:
        totals = await crud.purge_old_data(
            session, snapshot_days=0, notification_days=0, raw_response_days=7
        )
    expect("both aged rows were stripped",
           totals["raw_responses_nulled"] == 2, repr(totals))

    async with get_session() as session:
        page = await session.get(AccountSnapshot, page_id)
        api = await session.get(AccountSnapshot, api_id)
        expect("the page row still says it came from the page",
               crud.snapshot_source(page) == "public_page",
               repr(page.raw_response))
        expect("and it no longer carries the payload",
               page.raw_response == {"source": "public_page"},
               repr(page.raw_response))
        expect("an API row is still nulled outright — it has no marker to keep",
               api.raw_response is None, repr(api.raw_response))

    # And a second run is a no-op rather than re-reporting the same rows.
    async with get_session() as session:
        again = await crud.purge_old_data(
            session, snapshot_days=0, notification_days=0, raw_response_days=7
        )
    expect("running it again strips nothing new",
           again["raw_responses_nulled"] == 0, repr(again))


async def test_a_stripped_page_row_never_answers_as_the_api() -> None:
    """The sharp end of it: with the marker gone the page row WAS the API's
    newest, so a returning API check diffed its counts against the page's."""
    account_id = await _account("impostor")
    await _snapshot(account_id, "impostor", source=None, followers=90, age_days=40)
    await _snapshot(account_id, "impostor", source="public_page",
                    followers=100, age_days=30)
    async with get_session() as session:
        await crud.purge_old_data(
            session, snapshot_days=0, notification_days=0, raw_response_days=7
        )
        api = await crud.get_latest_snapshot_by_source(
            session, account_id, source=None
        )
    expect("the API's baseline is the API's own row",
           api is not None and api.followers_count == 90, repr(api))


# ---------- 2. the scan must widen rather than report no baseline ---------

async def test_the_scan_widens_past_a_long_outage() -> None:
    account_id = await _account("outage")
    await _snapshot(account_id, "outage", source=None, followers=90, age_days=40)
    # 40 page readings since — well past the 25-row fast scan.
    for i in range(40):
        await _snapshot(account_id, "outage", source="public_page",
                        followers=100 + i, age_days=30 - i * 0.5)

    async with get_session() as session:
        shallow = await crud.get_latest_snapshot_by_source(
            session, account_id, source=None, scan_limit=25, deep_scan_limit=25,
        )
        expect("the fast scan alone cannot see past the outage",
               shallow is None, repr(shallow))

        found = await crud.get_latest_snapshot_by_source(
            session, account_id, source=None
        )
    expect("widening finds the API's last reading",
           found is not None and found.followers_count == 90, repr(found))

    # The page's own baseline is still the newest page row, from the fast path.
    async with get_session() as session:
        page = await crud.get_latest_snapshot_by_source(
            session, account_id, source="public_page"
        )
    expect("and the page still diffs against its own newest",
           page is not None and page.followers_count == 139, repr(page))


# ---------- 3. the purge must keep the newest row of each door ------------

async def test_the_purge_keeps_a_baseline_for_every_door() -> None:
    account_id = await _account("purged")
    api_id = await _snapshot(account_id, "purged", source=None,
                             followers=90, age_days=60)
    stale_api = await _snapshot(account_id, "purged", source=None,
                                followers=80, age_days=70)
    page_old = await _snapshot(account_id, "purged", source="public_page",
                               followers=95, age_days=50)
    page_new = await _snapshot(account_id, "purged", source="public_page",
                               followers=100, age_days=1)

    async with get_session() as session:
        await crud.purge_old_data(
            session, snapshot_days=30, notification_days=0, raw_response_days=0
        )
    async with get_session() as session:
        survivors = {
            row.id for row in (await crud.recent_snapshots(session, account_id, limit=50))
        }
    expect("the API's newest row survives the cutoff", api_id in survivors,
           repr(sorted(survivors)))
    expect("the page's newest row survives too", page_new in survivors,
           repr(sorted(survivors)))
    expect("an older row of the same door is still purged",
           stale_api not in survivors and page_old not in survivors,
           repr(sorted(survivors)))

    async with get_session() as session:
        api = await crud.get_latest_snapshot_by_source(
            session, account_id, source=None
        )
    expect("so the API still has a baseline to diff against",
           api is not None and api.followers_count == 90, repr(api))


async def test_a_failed_reading_is_not_mistaken_for_a_baseline() -> None:
    """Only successful readings are baselines, so a run of failures must not
    keep a 401 row alive in place of the real one."""
    account_id = await _account("failing")
    good = await _snapshot(account_id, "failing", source=None,
                           followers=90, age_days=60)
    bad = await _snapshot(account_id, "failing", source=None, followers=0,
                          age_days=50, status=401)
    await _snapshot(account_id, "failing", source="public_page",
                    followers=100, age_days=1)

    async with get_session() as session:
        await crud.purge_old_data(
            session, snapshot_days=30, notification_days=0, raw_response_days=0
        )
    async with get_session() as session:
        survivors = {
            row.id for row in (await crud.recent_snapshots(session, account_id, limit=50))
        }
    expect("the successful API row is what gets kept", good in survivors,
           repr(sorted(survivors)))
    expect("the failed one is purged like any other aged row",
           bad not in survivors, repr(sorted(survivors)))


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await test_stripping_raw_response_keeps_the_door_marker()
    await test_a_stripped_page_row_never_answers_as_the_api()
    await test_the_scan_widens_past_a_long_outage()
    await test_the_purge_keeps_a_baseline_for_every_door()
    await test_a_failed_reading_is_not_mistaken_for_a_baseline()

    await engine.dispose()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("All source-baseline checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
