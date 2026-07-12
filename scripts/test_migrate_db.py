"""Unit checks for the DB migration helpers — no live database required.

Covers the two error-prone pieces: connection-string normalization (so a Neon
URL pastes in cleanly) and the per-row encode/decode that survives a JSON
round-trip (datetimes + JSONB columns).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db_url import normalize_db_url  # noqa: E402
from app.database.models import AccountSnapshot, AppSetting  # noqa: E402
from scripts.migrate_db import _decode_row, _encode_row  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def test_normalize_render_bare_postgres() -> None:
    out = normalize_db_url("postgres://u:p@host:5432/db")
    expect(
        "bare postgres:// -> postgresql+asyncpg://",
        out == "postgresql+asyncpg://u:p@host:5432/db",
        out,
    )


def test_normalize_postgresql_prefix() -> None:
    out = normalize_db_url("postgresql://u:p@host/db")
    expect(
        "postgresql:// -> postgresql+asyncpg://",
        out == "postgresql+asyncpg://u:p@host/db",
        out,
    )


def test_normalize_neon_sslmode() -> None:
    # A real Neon connection string shape.
    neon = "postgresql://user:pw@ep-cool-name-123.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
    out = normalize_db_url(neon)
    expect(
        "neon URL becomes asyncpg with ssl=require",
        out.startswith("postgresql+asyncpg://") and "ssl=require" in out,
        out,
    )
    expect(
        "libpq-only sslmode param is gone",
        "sslmode" not in out,
        out,
    )
    expect(
        "libpq-only channel_binding param is dropped",
        "channel_binding" not in out,
        out,
    )


def test_normalize_already_asyncpg_untouched() -> None:
    url = "postgresql+asyncpg://u:p@host/db"
    expect("already-asyncpg URL is unchanged", normalize_db_url(url) == url)


def test_normalize_sqlite_passthrough() -> None:
    url = "sqlite+aiosqlite:///./x.db"
    expect("sqlite URL passes through untouched", normalize_db_url(url) == url)


def test_encode_decode_datetime_and_jsonb_roundtrip() -> None:
    table = AccountSnapshot.__table__
    now = datetime(2026, 6, 11, 13, 47, 29, tzinfo=timezone.utc)
    raw = {
        "id": 7,
        "account_id": 3,
        "username": "someone",
        "http_status": 200,
        "created_at": now,
        "raw_response": {"reel_data": {"has_public_story": True}, "n": 5},
    }
    # Encode -> JSON text -> back to dict -> decode, exactly like the tool does.
    encoded = _encode_row(table, raw)
    reloaded = json.loads(json.dumps(encoded, ensure_ascii=False))
    decoded = _decode_row(table, reloaded)
    expect(
        "datetime survives the JSON round-trip as a datetime",
        decoded["created_at"] == now,
        f"{decoded['created_at']!r}",
    )
    expect(
        "JSONB dict survives untouched",
        decoded["raw_response"] == {"reel_data": {"has_public_story": True}, "n": 5},
        f"{decoded['raw_response']!r}",
    )
    expect("scalar fields preserved", decoded["id"] == 7 and decoded["username"] == "someone")


def test_decode_drops_unknown_columns() -> None:
    table = AppSetting.__table__
    row = {"key": "panel_msg_id", "value": "42", "updated_at": None, "legacy_col": "x"}
    decoded = _decode_row(table, row)
    expect(
        "unknown column from an old backup is dropped",
        "legacy_col" not in decoded and decoded["key"] == "panel_msg_id",
        f"{decoded!r}",
    )


def test_encode_handles_none_datetime() -> None:
    table = AccountSnapshot.__table__
    encoded = _encode_row(table, {"created_at": None, "username": "x"})
    expect("None datetime stays None", encoded["created_at"] is None)


def main() -> int:
    test_normalize_render_bare_postgres()
    test_normalize_postgresql_prefix()
    test_normalize_neon_sslmode()
    test_normalize_already_asyncpg_untouched()
    test_normalize_sqlite_passthrough()
    test_encode_decode_datetime_and_jsonb_roundtrip()
    test_decode_drops_unknown_columns()
    test_encode_handles_none_datetime()

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {FAILURES}")
        return 1
    print("\nall good")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    raise SystemExit(main())
