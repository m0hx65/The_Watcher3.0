"""Adding one account answers with its account card, not "No changes detected".

A first check has no baseline to diff against, so it can never find a change —
the old "No changes detected" reply said nothing. The add flow now runs that
check silently and turns its "Running first check…" note into the account card.

Hand-rolled (no pytest), in the repo's existing style. Fakes Telegram + the
monitor service; the card is rendered from a real SQLite-backed CRUD layer.
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

DB_FILE = ROOT / "test_add_card.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.bot import handlers  # noqa: E402
from app.database import crud  # noqa: E402
from app.database.models import AccountSnapshot, Base  # noqa: E402
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


def make_update():
    status_msg = SimpleNamespace(edit_text=AsyncMock(), message_id=2, chat_id=7)
    msg = SimpleNamespace(reply_text=AsyncMock(return_value=status_msg), message_id=1)
    update = SimpleNamespace(
        callback_query=None,
        message=msg,
        effective_chat=SimpleNamespace(id=7),
        effective_user=SimpleNamespace(id=99),
    )
    return update, status_msg


def make_context(service) -> SimpleNamespace:
    return SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock()),
        user_data={},
        args=[],
        application=SimpleNamespace(bot_data={"monitor": service}),
    )


def make_service(result: dict, *, is_private: bool = True) -> SimpleNamespace:
    async def check_username(username: str, *, notify_unchanged: bool = False):
        # Stand in for the real first check: it stores the baseline reading.
        if result.get("ok"):
            async with get_session() as session:
                account = await crud.get_account(session, username)
                session.add(AccountSnapshot(
                    account_id=account.id, username=username, http_status=200,
                    full_name="New Account", followers_count=1234,
                    following_count=56, posts_count=7, is_private=is_private,
                ))
        return result

    return SimpleNamespace(
        check_username=AsyncMock(side_effect=check_username),
        instagram=SimpleNamespace(
            fetch_reel_user=AsyncMock(return_value={"has_public_story": True})
        ),
        stories=None,
    )


async def test_add_sends_the_account_card() -> None:
    service = make_service({"ok": True, "username": "newacct", "status": 200})
    update, status_msg = make_update()
    context = make_context(service)
    await handlers._do_add(update, context, "newacct")

    kwargs = service.check_username.call_args.kwargs
    expect(
        "the first check runs without the 'No changes detected' reply",
        kwargs.get("notify_unchanged") is False,
        f"kwargs={kwargs}",
    )
    expect("the status note was edited once", status_msg.edit_text.await_count == 1)
    text = status_msg.edit_text.call_args.args[0] if status_msg.edit_text.call_args else ""
    expect("the reply says the account is now monitored", "Now monitoring" in text, text)
    expect("the reply carries the account card", "<b>@newacct</b>" in text, text)
    expect("the card shows the first reading", "1,234" in text and "New Account" in text, text)
    expect("no 'No changes' wording", "No changes" not in text, text)
    markup = status_msg.edit_text.call_args.kwargs.get("reply_markup")
    buttons = [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []
    expect(
        "the card has the account's action buttons",
        "acc:pause:newacct" in buttons,
        f"buttons={buttons}",
    )
    expect("no separate message was sent", context.bot.send_message.await_count == 0)
    expect("a private card has no Story line", "Story:" not in text, text)


async def test_public_card_is_drawn_before_the_story_check() -> None:
    service = make_service({"ok": True, "username": "openacct", "status": 200},
                           is_private=False)
    update, status_msg = make_update()
    context = make_context(service)
    await handlers._do_add(update, context, "openacct", instagram_id="4242")

    text = status_msg.edit_text.call_args.args[0] if status_msg.edit_text.call_args else ""
    expect(
        "the card goes up with the Story line still checking",
        f"Story: {handlers.STORY_CHECKING}" in text,
        text,
    )
    await asyncio.sleep(0.05)  # let the background story check answer
    filled = context.bot.edit_message_text.call_args.kwargs \
        if context.bot.edit_message_text.call_args else {}
    expect(
        "the Story line is filled in on the same message",
        filled.get("message_id") == status_msg.message_id
        and "Story: 🎬 has an active story" in filled.get("text", "")
        and "Now monitoring" in filled.get("text", ""),
        f"filled={filled}",
    )


async def test_failed_first_check_reports_the_error() -> None:
    service = make_service({"ok": False, "username": "brokenacct", "error": "HTTP 401"})
    update, status_msg = make_update()
    context = make_context(service)
    await handlers._do_add(update, context, "brokenacct")

    expect("the status note is left alone on failure", status_msg.edit_text.await_count == 0)
    sent = context.bot.send_message.call_args.kwargs.get("text", "") \
        if context.bot.send_message.call_args else ""
    expect("the failure is reported", "failed" in sent and "HTTP 401" in sent, sent)


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await test_add_sends_the_account_card()
    await test_public_card_is_drawn_before_the_story_check()
    await test_failed_first_check_reports_the_error()

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
