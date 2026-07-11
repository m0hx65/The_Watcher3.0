"""Regression tests for on-disk + media-hash data retention.

Two unbounded-growth holes the daily cleanup now closes:

1. profile_media_hashes: the CDN serves byte-different re-encodes of the same
   avatar on almost every fetch, so every sweep could add a new row AND a new
   file per account, forever. purge_old_media_hashes trims each account to the
   newest N rows and returns the dead rows' file paths for disk deletion.

2. data/media/<user>/stories/: every downloaded story/post/highlight file
   stayed on disk forever. _purge_story_files deletes files older than the
   retention window (they were already delivered to Telegram); avatar files in
   the account root are untouched (governed by the ledger above).

Runs offline on sqlite with a temp media dir — no Telegram, no network.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_data_retention.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from sqlalchemy import select  # noqa: E402

from app.database import crud  # noqa: E402
from app.database.models import (  # noqa: E402
    Base,
    MonitoredAccount,
    ProfileMediaHash,
)
from app.database.session import engine, get_session  # noqa: E402
from app.workers.scheduler import _purge_story_files, _unlink_files  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


async def test_media_hash_trim(tmp: Path) -> None:
    async with get_session() as session:
        session.add(MonitoredAccount(id=1, username="alpha", active=True))
        session.add(MonitoredAccount(id=2, username="beta", active=True))

    # 30 avatar re-encodes for alpha (over the cap), 5 for beta (under it).
    # created_at collides on sqlite (whole seconds), so the id tiebreaker in
    # purge_old_media_hashes is what must keep the NEWEST rows.
    paths: dict[int, list[Path]] = {1: [], 2: []}
    async with get_session() as session:
        for account_id, count in ((1, 30), (2, 5)):
            for i in range(count):
                f = tmp / f"acct{account_id}_avatar{i:02d}.jpg"
                f.write_bytes(b"avatar-bytes-%d" % i)
                paths[account_id].append(f)
                session.add(
                    ProfileMediaHash(
                        account_id=account_id,
                        sha256=f"{account_id:02d}{i:062x}",
                        source_url="http://cdn/x.jpg",
                        local_path=str(f),
                        byte_size=f.stat().st_size,
                        content_type="image/jpeg",
                    )
                )

    async with get_session() as session:
        doomed = await crud.purge_old_media_hashes(session, keep_per_account=25)

    async with get_session() as session:
        rows = (
            (await session.execute(select(ProfileMediaHash))).scalars().all()
        )
    per_account: dict[int, list[ProfileMediaHash]] = {1: [], 2: []}
    for r in rows:
        per_account[r.account_id].append(r)

    expect("over-cap account trimmed to 25 rows", len(per_account[1]) == 25,
           f"{len(per_account[1])} rows")
    expect("under-cap account untouched", len(per_account[2]) == 5,
           f"{len(per_account[2])} rows")
    expect("5 paths returned for disk deletion", len(doomed) == 5, repr(doomed))

    # The OLDEST five (lowest ids = first inserted) must be the doomed ones,
    # so /photo's latest_media_hash keeps working on the newest row.
    oldest_five = {str(p) for p in paths[1][:5]}
    expect("the oldest rows were the ones trimmed",
           set(doomed) == oldest_five, repr(doomed))

    async with get_session() as session:
        newest = await crud.latest_media_hash(session, 1)
    expect("latest_media_hash still returns the newest row",
           newest is not None and newest.local_path == str(paths[1][-1]),
           repr(newest.local_path if newest else None))

    removed = _unlink_files(doomed)
    expect("trimmed files removed from disk",
           removed == 5 and not any(Path(p).exists() for p in doomed),
           f"removed={removed}")
    expect("kept files still on disk", paths[1][-1].exists() and paths[1][5].exists())

    # Second run is a no-op (idempotent).
    async with get_session() as session:
        doomed2 = await crud.purge_old_media_hashes(session, keep_per_account=25)
    expect("second trim run is a no-op", doomed2 == [], repr(doomed2))


def test_story_file_retention(tmp: Path) -> None:
    media_root = tmp / "media"
    stories = media_root / "alpha" / "stories"
    stories.mkdir(parents=True)

    old_file = stories / "111.mp4"
    old_file.write_bytes(b"x" * 2048)
    stale = time.time() - 30 * 86_400
    os.utime(old_file, (stale, stale))

    fresh_file = stories / "222.jpg"
    fresh_file.write_bytes(b"y" * 100)

    # Avatar in the account ROOT must never be touched by the story sweep.
    avatar = media_root / "alpha" / "deadbeef.jpg"
    avatar.write_bytes(b"z")
    os.utime(avatar, (stale, stale))

    files, freed = _purge_story_files(media_root, older_than_days=14)
    expect("old story file deleted", files == 1 and not old_file.exists(),
           f"files={files}")
    expect("freed bytes counted", freed == 2048, f"freed={freed}")
    expect("fresh story file kept", fresh_file.exists())
    expect("account-root avatar untouched", avatar.exists())

    files2, _ = _purge_story_files(media_root, older_than_days=14)
    expect("story sweep is idempotent", files2 == 0, f"files={files2}")


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        await test_media_hash_trim(tmp)
        test_story_file_retention(tmp)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All data-retention tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
