"""Smoke test for the bulk /add flow (multiple accounts in one message).

Hand-rolled (no pytest), in the repo's existing style. Fakes Telegram + the
monitor service so the whole token-split -> resolve -> add -> background-check
path runs against a real SQLite-backed CRUD layer.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_bulk_add.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.bot import handlers  # noqa: E402
from app.database import crud  # noqa: E402
from app.database.models import MonitoredAccount  # noqa: E402
from app.database.session import dispose_engine, engine, get_session  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def make_update() -> SimpleNamespace:
    msg = SimpleNamespace(reply_text=AsyncMock(), message_id=1)
    return SimpleNamespace(
        callback_query=None,
        message=msg,
        effective_chat=SimpleNamespace(id=7),
        effective_user=SimpleNamespace(id=99),
    )


def make_context(service) -> SimpleNamespace:
    return SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock()),
        user_data={},
        args=[],
        application=SimpleNamespace(bot_data={"monitor": service}),
    )


def make_service() -> SimpleNamespace:
    return SimpleNamespace(
        check_username=AsyncMock(return_value={"ok": True}),
        instagram=SimpleNamespace(
            fetch_username_by_id=AsyncMock(return_value="resolveduser")
        ),
    )


def test_split_targets() -> None:
    cases = {
        "opscn1 whos.lisianna 7hiddenglow": ["opscn1", "whos.lisianna", "7hiddenglow"],
        "a,b , c": ["a", "b", "c"],
        "one\ntwo\nthree": ["one", "two", "three"],
        "  solo  ": ["solo"],
        "@u1, @u2\n@u3": ["@u1", "@u2", "@u3"],
        "https://instagram.com/nasa": ["https://instagram.com/nasa"],
    }
    for raw, want in cases.items():
        got = handlers._split_add_targets(raw)
        expect(f"split {raw!r}", got == want, f"got={got}")


async def test_bulk_add_creates_all() -> None:
    service = make_service()
    update = make_update()
    context = make_context(service)
    tokens = ["opscn1", "@whos.lisianna", "7hiddenglow", "rein__saad"]
    await handlers._do_add_bulk(update, context, tokens)

    async with get_session() as session:
        accounts = await crud.list_accounts(session, only_active=False)
    names = sorted(a.username for a in accounts)
    expect(
        "all four accounts persisted",
        names == ["7hiddenglow", "opscn1", "rein__saad", "whos.lisianna"],
        f"names={names}",
    )
    # Background baseline checks were scheduled — let them run.
    await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    expect(
        "first check run for each new account",
        service.check_username.await_count == 4,
        f"count={service.check_username.await_count}",
    )
    expect("a summary reply was sent", update.message.reply_text.await_count == 1)


async def test_bulk_add_dedupes_and_reports_existing() -> None:
    service = make_service()
    update = make_update()
    context = make_context(service)
    # opscn1 already exists from the previous test; whos.lisianna repeated twice.
    tokens = ["opscn1", "newone", "whos.lisianna", "whos.lisianna"]
    await handlers._do_add_bulk(update, context, tokens)
    await asyncio.sleep(0.05)
    async with get_session() as session:
        acc = await crud.get_account(session, "newone")
    expect("the genuinely new account was added", acc is not None)
    expect(
        "only the one new account is baseline-checked (dupes/existing skipped)",
        service.check_username.await_count == 1
        and service.check_username.call_args.args[0] == "newone",
        f"count={service.check_username.await_count}",
    )


async def test_bulk_add_flags_invalid() -> None:
    service = make_service()
    update = make_update()
    context = make_context(service)
    tokens = ["good_user", "not a valid !!", "also$bad"]
    # _split already happened upstream; here we pass raw-ish tokens. The two bad
    # ones fail _parse_add_target and must be reported, not added.
    await handlers._do_add_bulk(update, context, ["good_user", "also$bad"])
    await asyncio.sleep(0.05)
    async with get_session() as session:
        good = await crud.get_account(session, "good_user")
        bad = await crud.get_account(session, "also$bad")
    expect("valid token added", good is not None)
    expect("invalid token not added", bad is None)


async def test_numeric_id_resolved_in_bulk() -> None:
    service = make_service()
    update = make_update()
    context = make_context(service)
    await handlers._do_add_bulk(update, context, ["123456789", "plainuser"])
    await asyncio.sleep(0.05)
    expect(
        "numeric id was resolved to a username via the IG client",
        service.instagram.fetch_username_by_id.await_count == 1,
    )
    async with get_session() as session:
        acc = await crud.get_account(session, "resolveduser")
    expect("resolved id persisted under its username", acc is not None)


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(MonitoredAccount.__table__.create)

    test_split_targets()
    await test_bulk_add_creates_all()
    await test_bulk_add_dedupes_and_reports_existing()
    await test_bulk_add_flags_invalid()
    await test_numeric_id_resolved_in_bulk()

    await dispose_engine()
    if DB_FILE.exists():
        DB_FILE.unlink()

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
    sys.exit(asyncio.run(main()))
