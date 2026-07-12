"""Exercise the new callback / prompt cleanup helpers in app.bot.handlers.

This is a hand-rolled smoke test (no pytest) that drives the handlers with
unittest.mock.AsyncMock fakes for the Telegram objects so we don't need a
live bot.
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

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_callback_cleanup.db")

from telegram.error import BadRequest  # noqa: E402

from app.bot import handlers  # noqa: E402


FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" — {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def make_update(*, callback_msg_id: int | None = 42, chat_id: int = 7) -> SimpleNamespace:
    """Build a fake Update with an attached callback_query and chat."""
    query = SimpleNamespace(
        data="",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(
            message_id=callback_msg_id,
            delete=AsyncMock(),
        ) if callback_msg_id is not None else None,
    )
    return SimpleNamespace(
        callback_query=query,
        message=None,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=99),
    )


def make_context() -> SimpleNamespace:
    bot = SimpleNamespace(
        delete_message=AsyncMock(),
        send_message=AsyncMock(),
    )
    return SimpleNamespace(
        bot=bot,
        user_data={},
        application=SimpleNamespace(bot_data={}),
    )


async def test_consume_prompt_message_deletes_and_pops() -> None:
    update = make_update()
    context = make_context()
    context.user_data[handlers._PROMPT_MSG_ID] = 123
    await handlers._consume_prompt_message(update, context)
    expect(
        "consume_prompt deletes the stored message",
        context.bot.delete_message.await_count == 1,
        f"awaited {context.bot.delete_message.await_count} times",
    )
    args, kwargs = context.bot.delete_message.call_args
    expect(
        "consume_prompt targets the right (chat, message)",
        kwargs == {"chat_id": 7, "message_id": 123},
        f"kwargs={kwargs}",
    )
    expect(
        "consume_prompt clears _PROMPT_MSG_ID",
        handlers._PROMPT_MSG_ID not in context.user_data,
    )


async def test_consume_prompt_message_noop_when_unset() -> None:
    update = make_update()
    context = make_context()
    await handlers._consume_prompt_message(update, context)
    expect(
        "consume_prompt without stored id is a no-op",
        context.bot.delete_message.await_count == 0,
    )


async def test_consume_prompt_message_swallows_badrequest() -> None:
    update = make_update()
    context = make_context()
    context.bot.delete_message = AsyncMock(side_effect=BadRequest("not found"))
    context.user_data[handlers._PROMPT_MSG_ID] = 5
    try:
        await handlers._consume_prompt_message(update, context)
        passed = True
    except Exception as exc:
        passed = False
        print(f"  swallowed?: {exc!r}")
    expect("consume_prompt swallows BadRequest", passed)


async def test_delete_callback_message_deletes() -> None:
    update = make_update(callback_msg_id=55)
    await handlers._delete_callback_message(update)
    expect(
        "delete_callback_message calls query.message.delete()",
        update.callback_query.message.delete.await_count == 1,
    )


async def test_delete_callback_message_safe_without_message() -> None:
    update = make_update(callback_msg_id=None)
    # Should be a no-op (no exception)
    await handlers._delete_callback_message(update)
    expect("delete_callback_message handles missing message", True)


async def test_safe_answer_swallows_badrequest() -> None:
    query = SimpleNamespace(answer=AsyncMock(side_effect=BadRequest("too old")))
    try:
        await handlers._safe_answer(query)
        passed = True
    except Exception:
        passed = False
    expect("safe_answer swallows BadRequest", passed)


async def test_safe_edit_text_swallows_known_badrequests() -> None:
    for msg in (
        "Message is not modified",
        "Message to edit not found",
        "Message can't be edited",
        "MESSAGE_ID_INVALID",
    ):
        query = SimpleNamespace(edit_message_text=AsyncMock(side_effect=BadRequest(msg)))
        try:
            await handlers._safe_edit_text(query, "hi")
            ok = True
        except Exception as exc:
            ok = False
            print(f"  msg {msg!r} raised: {exc!r}")
        expect(f"safe_edit_text swallows BadRequest: {msg!r}", ok)


async def test_safe_edit_text_reraises_unknown_badrequest() -> None:
    query = SimpleNamespace(edit_message_text=AsyncMock(side_effect=BadRequest("Bad request: chat not found")))
    raised = False
    try:
        await handlers._safe_edit_text(query, "hi")
    except BadRequest:
        raised = True
    expect("safe_edit_text re-raises unknown BadRequest", raised)


async def test_on_callback_deletes_stale_prompt_on_other_message() -> None:
    update = make_update(callback_msg_id=999)  # Different from stale prompt
    context = make_context()
    context.user_data[handlers._PROMPT_MSG_ID] = 111  # Stale
    context.user_data[handlers._AWAITING_USERNAME] = True
    update.callback_query.data = "menu:status"

    # Stub out the menu dispatcher so we just exercise on_callback's cleanup.
    async def fake_handle_menu(*args, **kwargs):
        return None
    handlers._handle_menu = fake_handle_menu  # type: ignore[assignment]

    await handlers.on_callback(update, context)
    expect(
        "stale prompt on a different message is deleted",
        context.bot.delete_message.await_count == 1,
        f"awaited {context.bot.delete_message.await_count}x with {context.bot.delete_message.call_args}",
    )
    expect(
        "_PROMPT_MSG_ID is cleared after stale cleanup",
        handlers._PROMPT_MSG_ID not in context.user_data,
    )
    expect(
        "_AWAITING_USERNAME is cleared when callback isn't menu:add",
        handlers._AWAITING_USERNAME not in context.user_data,
    )


async def test_on_callback_keeps_current_message_when_pressing_cancel() -> None:
    """Cancel on a prompt — the message is THIS callback, so we should not delete it
    (it'll be edited in place)."""
    update = make_update(callback_msg_id=42)
    context = make_context()
    context.user_data[handlers._PROMPT_MSG_ID] = 42  # Same as callback msg
    context.user_data[handlers._AWAITING_USERNAME] = True
    update.callback_query.data = "menu:main"

    async def fake_handle_menu(*args, **kwargs):
        return None
    handlers._handle_menu = fake_handle_menu  # type: ignore[assignment]

    await handlers.on_callback(update, context)
    expect(
        "callback on the prompt itself does NOT delete the message",
        context.bot.delete_message.await_count == 0,
    )
    expect(
        "_PROMPT_MSG_ID cleared after Cancel/non-prompt callback",
        handlers._PROMPT_MSG_ID not in context.user_data,
    )


async def test_on_callback_keeps_prompt_state_for_menu_add() -> None:
    """menu:add must not clear awaiting/prompt msg id during the cleanup phase."""
    update = make_update(callback_msg_id=10)
    context = make_context()
    context.user_data[handlers._PROMPT_MSG_ID] = 10  # Same as callback (rare but legal)
    context.user_data[handlers._AWAITING_USERNAME] = True  # Already prompting
    update.callback_query.data = "menu:add"

    seen_call = {"called": False}

    async def fake_handle_menu(*args, **kwargs):
        seen_call["called"] = True
        # _handle_menu would normally set _PROMPT_MSG_ID itself; we just track that
        # the cleanup didn't strip the awaiting state before we got here.
        return None
    handlers._handle_menu = fake_handle_menu  # type: ignore[assignment]

    await handlers.on_callback(update, context)
    expect("menu:add dispatches to _handle_menu", seen_call["called"])
    expect(
        "menu:add preserves _AWAITING_USERNAME during cleanup",
        context.user_data.get(handlers._AWAITING_USERNAME) is True,
    )


async def main() -> int:
    await test_consume_prompt_message_deletes_and_pops()
    await test_consume_prompt_message_noop_when_unset()
    await test_consume_prompt_message_swallows_badrequest()
    await test_delete_callback_message_deletes()
    await test_delete_callback_message_safe_without_message()
    await test_safe_answer_swallows_badrequest()
    await test_safe_edit_text_swallows_known_badrequests()
    await test_safe_edit_text_reraises_unknown_badrequest()
    await test_on_callback_deletes_stale_prompt_on_other_message()
    await test_on_callback_keeps_current_message_when_pressing_cancel()
    await test_on_callback_keeps_prompt_state_for_menu_add()

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {FAILURES}")
        return 1
    print("\nall good")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
