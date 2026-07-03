"""Exercise the "🔎 Any user" flow: story-link, username, profile-URL, and
highlight-link routing through on_plain_text.

Hand-rolled smoke test (no pytest), in the style of test_download_all.py:
unittest.mock.AsyncMock fakes stand in for Telegram and the service so the
whole prompt -> text -> service pipeline runs without a live bot.
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
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.bot import handlers, keyboards  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def flatten(markup) -> list:
    return [btn for row in markup.inline_keyboard for btn in row]


def make_message_update(text: str) -> SimpleNamespace:
    reply_msg = SimpleNamespace(message_id=500, edit_text=AsyncMock())
    message = SimpleNamespace(
        text=text,
        reply_text=AsyncMock(return_value=reply_msg),
    )
    return SimpleNamespace(
        callback_query=None,
        message=message,
        effective_chat=SimpleNamespace(id=7),
        effective_user=SimpleNamespace(id=99),
    )


def make_service_mock() -> SimpleNamespace:
    return SimpleNamespace(
        fetch_and_send_story_url=AsyncMock(
            return_value={"ok": True, "count": 1, "error": None}
        ),
        fetch_and_send_stories=AsyncMock(
            return_value={"ok": True, "count": 2, "error": None}
        ),
        instagram=SimpleNamespace(
            fetch_username_by_id=AsyncMock(return_value="someuser")
        ),
    )


def make_context(service) -> SimpleNamespace:
    bot = SimpleNamespace(delete_message=AsyncMock(), send_message=AsyncMock())
    return SimpleNamespace(
        bot=bot,
        user_data={handlers._AWAITING_FETCH_USERNAME: True},
        args=[],
        application=SimpleNamespace(bot_data={"monitor": service}),
    )


def _markups(update) -> list:
    """Every reply_markup passed to reply_text on this update."""
    return [
        c.kwargs.get("reply_markup")
        for c in update.message.reply_text.call_args_list
        if c.kwargs.get("reply_markup") is not None
    ]


def _callbacks(markup) -> list[str]:
    return [b.callback_data for b in flatten(markup)]


# ---------- keyboard ----------

def test_fetch_actions_buttons() -> None:
    data = _callbacks(keyboards.fetch_actions("shemaa.khalill"))
    expect("fetch_actions offers profile pic", "acc:photo:shemaa.khalill" in data, str(data))
    expect("fetch_actions offers story", "acc:story:shemaa.khalill" in data, str(data))
    expect(
        "fetch_actions offers highlights",
        "acc:highlights:shemaa.khalill" in data,
        str(data),
    )
    expect("fetch_actions offers home", "menu:main" in data, str(data))
    worst = max(len(c.encode("utf-8")) for c in data)
    expect("fetch_actions callbacks within 64-byte cap", worst <= 64, f"worst={worst}")


# ---------- routing ----------

async def test_story_url_downloads_immediately() -> None:
    service = make_service_mock()
    context = make_context(service)
    url = (
        "https://www.instagram.com/stories/shemaa.khalill/3933177616550897519"
        "?utm_source=ig_story_item_share&igsh=MWUxeWszbW5wMmQ2NA=="
    )
    update = make_message_update(url)
    await handlers.on_plain_text(update, context)

    expect(
        "story link calls fetch_and_send_story_url once",
        service.fetch_and_send_story_url.await_count == 1,
        str(service.fetch_and_send_story_url.await_count),
    )
    expect(
        "story link does NOT go through the account story path",
        service.fetch_and_send_stories.await_count == 0,
    )
    if service.fetch_and_send_story_url.await_count == 1:
        call = service.fetch_and_send_story_url.call_args
        expect("story call passes the username", call.args[0] == "shemaa.khalill", str(call.args))
        expect("story call passes the full url", call.args[1] == url, str(call.args))
        expect(
            "story call passes the story pk",
            call.kwargs.get("pk") == "3933177616550897519",
            str(call.kwargs),
        )
    expect(
        "awaiting-fetch flag cleared after a story link",
        handlers._AWAITING_FETCH_USERNAME not in context.user_data,
    )


async def test_username_shows_action_menu() -> None:
    service = make_service_mock()
    context = make_context(service)
    update = make_message_update("shemaa.khalill")
    await handlers.on_plain_text(update, context)

    expect(
        "bare username does not download anything yet",
        service.fetch_and_send_story_url.await_count == 0
        and service.fetch_and_send_stories.await_count == 0,
    )
    offered = [
        m for m in _markups(update) if "acc:photo:shemaa.khalill" in _callbacks(m)
    ]
    expect("username offers the profile-pic/story/highlights menu", bool(offered))


async def test_profile_url_shows_action_menu() -> None:
    service = make_service_mock()
    context = make_context(service)
    update = make_message_update(
        "https://www.instagram.com/shemaa.khalill?igsh=ejV6M3Zhcmdtem9h"
    )
    await handlers.on_plain_text(update, context)

    expect(
        "profile URL does not download anything yet",
        service.fetch_and_send_story_url.await_count == 0,
    )
    offered = [
        m for m in _markups(update) if "acc:story:shemaa.khalill" in _callbacks(m)
    ]
    expect("profile URL offers the action menu for the right user", bool(offered))


async def test_stories_page_without_pk_shows_menu() -> None:
    # A /stories/<username>/ page with no specific item id must be read as the
    # account (not a user literally named "stories") and offer the action menu.
    service = make_service_mock()
    context = make_context(service)
    update = make_message_update("https://www.instagram.com/stories/shemaa.khalill/")
    await handlers.on_plain_text(update, context)

    expect(
        "pk-less stories page downloads nothing yet",
        service.fetch_and_send_story_url.await_count == 0,
    )
    offered = [
        m for m in _markups(update) if "acc:photo:shemaa.khalill" in _callbacks(m)
    ]
    expect("pk-less stories page offers the menu for the real account", bool(offered))


async def test_highlights_link_is_redirected() -> None:
    service = make_service_mock()
    context = make_context(service)
    update = make_message_update(
        "https://www.instagram.com/stories/highlights/17912345678901234/"
    )
    await handlers.on_plain_text(update, context)

    expect(
        "highlights link never hits the story downloader",
        service.fetch_and_send_story_url.await_count == 0,
    )
    text = update.message.reply_text.call_args.args[0]
    expect("highlights link gets a helpful redirect", "highlights" in text.lower(), text)


async def test_garbage_input_is_rejected() -> None:
    service = make_service_mock()
    context = make_context(service)
    update = make_message_update("this is not a username!!!")
    await handlers.on_plain_text(update, context)

    expect(
        "garbage input downloads nothing",
        service.fetch_and_send_story_url.await_count == 0
        and service.fetch_and_send_stories.await_count == 0,
    )
    # No action menu should be offered for invalid input.
    offered = [m for m in _markups(update) if any("acc:" in c for c in _callbacks(m))]
    expect("garbage input offers no action menu", not offered)


async def main() -> None:
    test_fetch_actions_buttons()
    await test_story_url_downloads_immediately()
    await test_username_shows_action_menu()
    await test_profile_url_shows_action_menu()
    await test_stories_page_without_pk_shows_menu()
    await test_highlights_link_is_redirected()
    await test_garbage_input_is_rejected()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        sys.exit(1)
    print("all good")


if __name__ == "__main__":
    asyncio.run(main())
