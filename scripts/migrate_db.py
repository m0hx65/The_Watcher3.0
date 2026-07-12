"""Migrate The Watcher's Postgres data between databases — no pg_dump needed.

Built on the app's own SQLAlchemy models, so it needs only the dependencies
already in requirements.txt (SQLAlchemy + asyncpg). Three modes:

  # 1) Safety-net backup to a local file (no target DB required):
  python scripts/migrate_db.py --source "<OLD_DATABASE_URL>" --dump-json watcher-backup.json

  # 2) Restore a backup file into a fresh database:
  python scripts/migrate_db.py --from-json watcher-backup.json --target "<NEW_DATABASE_URL>"

  # 3) Direct copy, old -> new, in one shot:
  python scripts/migrate_db.py --source "<OLD_DATABASE_URL>" --target "<NEW_DATABASE_URL>"

Any postgres:// / postgresql:// URL works (Neon/Supabase sslmode params are
handled automatically by app.config.normalize_db_url). The target schema is
created if missing, primary keys are preserved so foreign keys stay intact,
and Postgres identity sequences are reset afterwards so the app's next INSERT
doesn't collide with a migrated id.

Get the OLD url from Render -> watcher-db -> "External Database URL".
Get the NEW url from Neon -> your project -> Connection string (asyncpg/psql).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import BigInteger, DateTime, Integer, Table, func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db_url import normalize_db_url
from app.database.models import Base


def _datetime_columns(table: Table) -> set[str]:
    return {c.name for c in table.columns if isinstance(c.type, DateTime)}


def _encode_row(table: Table, mapping: dict) -> dict:
    """Row (as read from the DB) -> JSON-safe dict. Datetimes become ISO text;
    JSONB columns are already JSON-native dicts/lists and pass through."""
    dt_cols = _datetime_columns(table)
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if key in dt_cols and isinstance(value, datetime):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


def _decode_row(table: Table, row: dict) -> dict:
    """JSON dict -> row ready to INSERT. ISO datetime text becomes datetimes;
    unknown keys are dropped so an old backup still loads after a schema add."""
    dt_cols = _datetime_columns(table)
    valid = set(table.columns.keys())
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key not in valid:
            continue
        if key in dt_cols and isinstance(value, str):
            out[key] = datetime.fromisoformat(value)
        else:
            out[key] = value
    return out


async def _read_all(source_url: str) -> dict[str, list[dict]]:
    engine = create_async_engine(normalize_db_url(source_url))
    data: dict[str, list[dict]] = {}
    try:
        async with engine.connect() as conn:
            for table in Base.metadata.sorted_tables:
                result = await conn.execute(select(table))
                rows = [_encode_row(table, dict(m)) for m in result.mappings().all()]
                data[table.name] = rows
                print(f"  read  {table.name}: {len(rows)} rows")
    finally:
        await engine.dispose()
    return data


async def _write_all(
    target_url: str, data: dict[str, list[dict]], *, force: bool
) -> None:
    engine = create_async_engine(normalize_db_url(target_url))
    is_pg = engine.url.get_backend_name().startswith("postgresql")
    try:
        # Build the schema (idempotent) so a brand-new Neon DB is ready.
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Refuse to load on top of existing data unless --force, so a second
        # accidental run can't duplicate-insert and collide on primary keys.
        async with engine.connect() as conn:
            existing = await conn.execute(
                select(func.count()).select_from(Base.metadata.tables["monitored_accounts"])
            )
            count = existing.scalar() or 0
        if count and not force:
            raise SystemExit(
                f"Target already has {count} monitored_accounts row(s). "
                "Refusing to load on top of it — point at an empty database, "
                "or pass --force to insert anyway (may collide)."
            )

        async with engine.begin() as conn:
            for table in Base.metadata.sorted_tables:  # parents before children
                rows = [_decode_row(table, r) for r in data.get(table.name, [])]
                if not rows:
                    print(f"  write {table.name}: 0 rows (skip)")
                    continue
                await conn.execute(table.insert(), rows)
                print(f"  write {table.name}: {len(rows)} rows")

            # Reset autoincrement sequences so the app's next INSERT continues
            # past the highest migrated id instead of starting at 1.
            if is_pg:
                for table in Base.metadata.sorted_tables:
                    for col in table.primary_key.columns:
                        if not isinstance(col.type, (Integer, BigInteger)):
                            continue
                        max_id = (
                            await conn.execute(
                                select(func.max(table.c[col.name]))
                            )
                        ).scalar()
                        if max_id is None:
                            continue
                        await conn.execute(
                            text(
                                "SELECT setval(pg_get_serial_sequence(:t, :c), :v)"
                            ),
                            {"t": table.name, "c": col.name, "v": int(max_id)},
                        )
                        print(f"  seq   {table.name}.{col.name} -> {int(max_id)}")
    finally:
        await engine.dispose()


def _totals(data: dict[str, list[dict]]) -> str:
    total = sum(len(v) for v in data.values())
    return f"{total} rows across {len(data)} tables"


async def main_async(args: argparse.Namespace) -> int:
    if args.dump_json and args.source:
        print("Reading from source database…")
        data = await _read_all(args.source)
        Path(args.dump_json).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n✅ Backup written: {args.dump_json} ({_totals(data)})")
        return 0

    if args.from_json and args.target:
        raw = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        print(f"Loaded backup file {args.from_json} ({_totals(raw)})")
        print("Writing to target database…")
        await _write_all(args.target, raw, force=args.force)
        print(f"\n✅ Restore complete into target ({_totals(raw)})")
        return 0

    if args.source and args.target:
        print("Reading from source database…")
        data = await _read_all(args.source)
        print("Writing to target database…")
        await _write_all(args.target, data, force=args.force)
        print(f"\n✅ Direct copy complete ({_totals(data)})")
        return 0

    print(
        "Nothing to do. Use one of:\n"
        "  --source URL --dump-json FILE   (backup)\n"
        "  --from-json FILE --target URL   (restore)\n"
        "  --source URL --target URL       (direct copy)",
        file=sys.stderr,
    )
    return 2


def main() -> int:
    # asyncpg's TLS upgrade fails on Windows' default Proactor loop with
    # "[WinError 64] The specified network name is no longer available" when
    # talking to SSL-required hosts (Render, Neon). The Selector loop does the
    # upgrade correctly, so force it on Windows for this one-shot tool.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    parser = argparse.ArgumentParser(description="Migrate The Watcher's DB data.")
    parser.add_argument("--source", help="Source DATABASE_URL (the old/Render DB)")
    parser.add_argument("--target", help="Target DATABASE_URL (the new/Neon DB)")
    parser.add_argument("--dump-json", help="Write source rows to this JSON file")
    parser.add_argument("--from-json", help="Read rows from this JSON file")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Insert even if the target already has data (may collide).",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(main_async(args))
    except SystemExit as exc:  # surfaced from _write_all guard
        print(f"\n✋ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    raise SystemExit(main())
