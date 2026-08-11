"""Verify when the main-menu panel bump is suppressed.

Hand-rolled smoke test (no pytest). A fake bot + a toggleable download_active
flag drive PanelBumper directly, asserting it re-anchors the panel for sweep
notifications but never:

- while a manual download is in flight (the duplicate-menu bug),

and that when it DOES re-anchor it re-posts whatever the panel is currently
showing. Re-anchoring DELETES the panel, and the panel is where "Sweep
running", Status and Dark radar are rendered — so posting a hardcoded menu
back threw those views away a second after a button opened them.
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

from app.bot.handlers import (  # noqa: E402
    PANEL_CHAT_ID,
    PANEL_MSG_ID,
    WELCOME_TEXT,
    _LAST_RENDER,
    _LAST_RENDER_MAX,
    current_render,
    forget_render,
    remember_render,
)
from app.bot.panel_bump import PanelBumper  # noqa: E402

FAILURES: list[str] = []
DEBOUNCE = 0.01


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def make_bot() -> SimpleNamespace:
    return SimpleNamespace(
        delete_message=AsyncMock(),
        send_message=AsyncMock(
            return_value=SimpleNamespace(message_id=999)
        ),
    )


def make_bumper(bot, *, download_active, max_deferral=DEBOUNCE * 4) -> tuple[PanelBumper, dict, list]:
    bot_data = {PANEL_MSG_ID: 100, PANEL_CHAT_ID: 7}
    persisted: list = []

    async def _persist(mid, cid):
        persisted.append((mid, cid))

    bumper = PanelBumper(
        bot,
        bot_data,
        download_active=download_active,
        persist=_persist,
        debounce=DEBOUNCE,
        max_deferral=max_deferral,
    )
    return bumper, bot_data, persisted


async def _settle() -> None:
    # Give the debounced bump task time to run (or not).
    await asyncio.sleep(DEBOUNCE * 6)


async def test_bump_when_idle() -> None:
    bot = make_bot()
    bumper, bot_data, persisted = make_bumper(bot, download_active=lambda: False)
    await bumper.schedule()
    await _settle()
    expect("idle: old panel deleted", bot.delete_message.await_count == 1)
    expect("idle: fresh panel posted", bot.send_message.await_count == 1)
    expect("idle: new panel id recorded", bot_data.get(PANEL_MSG_ID) == 999)
    expect("idle: new position persisted", persisted == [(999, 7)])


async def test_no_bump_between_download_items() -> None:
    """A batch download delivers each item through the notifier. Bumping per
    item would wedge a menu between every photo — but the re-anchor must not be
    lost either, or the panel is left stranded above the media."""
    bot = make_bot()
    active = {"v": True}
    bumper, bot_data, persisted = make_bumper(
        bot, download_active=lambda: active["v"], max_deferral=DEBOUNCE * 40
    )
    await bumper.schedule()
    await _settle()
    expect(
        "download: nothing posted between items",
        bot.send_message.await_count == 0,
        str(bot.send_message.await_count),
    )
    expect("download: panel id untouched mid-batch", bot_data.get(PANEL_MSG_ID) == 100)
    expect("download: nothing persisted mid-batch", persisted == [])

    active["v"] = False  # batch finishes
    await _settle()
    expect(
        "download: the panel re-anchors once the batch ends",
        bot.send_message.await_count == 1,
        str(bot.send_message.await_count),
    )
    expect("download: new panel id recorded", bot_data.get(PANEL_MSG_ID) == 999)


async def test_download_starting_mid_debounce_defers_the_bump() -> None:
    # Scheduled while idle, but a download begins before the debounce fires —
    # the bump must wait it out rather than land between the media items.
    bot = make_bot()
    active = {"v": False}
    bumper, bot_data, _persisted = make_bumper(
        bot, download_active=lambda: active["v"], max_deferral=DEBOUNCE * 40
    )
    await bumper.schedule()  # idle at schedule time -> task queued
    active["v"] = True  # download starts during the debounce window
    await _settle()
    expect(
        "mid-debounce download: no panel posted",
        bot.send_message.await_count == 0,
        str(bot.send_message.await_count),
    )
    expect("mid-debounce download: panel id untouched", bot_data.get(PANEL_MSG_ID) == 100)

    active["v"] = False
    await _settle()
    expect(
        "mid-debounce download: bump runs after it finishes",
        bot.send_message.await_count == 1,
        str(bot.send_message.await_count),
    )


async def test_wedged_download_gives_up() -> None:
    """A download that never clears must not leave a task waiting forever."""
    bot = make_bot()
    bumper, _bot_data, _persisted = make_bumper(
        bot, download_active=lambda: True, max_deferral=DEBOUNCE * 2
    )
    await bumper.schedule()
    await _settle()
    await _settle()
    expect(
        "a wedged download abandons the bump",
        bot.send_message.await_count == 0,
        str(bot.send_message.await_count),
    )
    expect("and the task is not left pending", bumper._pending.done())


async def test_burst_collapses_to_one_bump() -> None:
    bot = make_bot()
    bumper, _bot_data, _persisted = make_bumper(bot, download_active=lambda: False)
    # Several notifications in quick succession should collapse into one bump.
    await asyncio.gather(*(bumper.schedule() for _ in range(5)))
    await _settle()
    expect(
        "burst of sends triggers exactly one re-anchor",
        bot.send_message.await_count == 1,
        str(bot.send_message.await_count),
    )


async def test_bump_preserves_the_open_view() -> None:
    """Pressing 🔄 Sweep All renders "Sweep running" INTO the panel. The sweep's
    own first notification re-anchors it a second later, and that must move the
    view to the bottom — not replace it with a bare menu."""
    bot = make_bot()
    bumper, bot_data, persisted = make_bumper(bot, download_active=lambda: False)
    view_text = "🔄 Sweep running — results will appear in the chat."
    view_markup = "STATUS_ACTIONS"
    remember_render(100, view_text, view_markup)
    try:
        await bumper.schedule()
        await _settle()
        expect("the stale copy is deleted", bot.delete_message.await_count == 1)
        expect("the panel is re-posted", bot.send_message.await_count == 1)
        kwargs = bot.send_message.await_args.kwargs
        expect("the open view is re-posted, not the menu",
               kwargs.get("text") == view_text, repr(kwargs.get("text")))
        expect("its keyboard comes with it",
               kwargs.get("reply_markup") == view_markup,
               repr(kwargs.get("reply_markup")))
        expect("the new panel id is recorded", bot_data.get(PANEL_MSG_ID) == 999)
        expect("the new position is persisted", persisted == [(999, 7)])
        # The view moved to a new message id — a second notification must
        # preserve it again rather than fall back to the menu.
        carried = current_render(999)
        expect("the view record follows the panel to its new id",
               carried == (view_text, view_markup), repr(carried))
        expect("the old id's record is dropped", current_render(100) is None)
    finally:
        forget_render(100)
        forget_render(999)


async def test_bump_falls_back_to_the_menu_when_unknown() -> None:
    """After a restart the panel's content isn't known — the menu is the only
    honest thing to post, and it still lands at the bottom."""
    bot = make_bot()
    bumper, _bot_data, _persisted = make_bumper(bot, download_active=lambda: False)
    forget_render(100)
    try:
        await bumper.schedule()
        await _settle()
        kwargs = bot.send_message.await_args.kwargs
        expect("an unknown panel falls back to the menu",
               kwargs.get("text") == WELCOME_TEXT, repr(kwargs.get("text")))
    finally:
        forget_render(999)


def test_render_record_is_bounded() -> None:
    """The record exists to look up one message; it must not grow forever as
    account cards and search results are edited."""
    for i in range(_LAST_RENDER_MAX + 25):
        remember_render(10_000 + i, f"text {i}", None)
    expect("the render record stays bounded",
           len(_LAST_RENDER) <= _LAST_RENDER_MAX, str(len(_LAST_RENDER)))
    expect("the newest render survives eviction",
           current_render(10_000 + _LAST_RENDER_MAX + 24) is not None)
    expect("the oldest render is evicted", current_render(10_000) is None)
    _LAST_RENDER.clear()


async def main() -> None:
    await test_bump_when_idle()
    await test_no_bump_between_download_items()
    await test_download_starting_mid_debounce_defers_the_bump()
    await test_wedged_download_gives_up()
    await test_burst_collapses_to_one_bump()
    await test_bump_preserves_the_open_view()
    await test_bump_falls_back_to_the_menu_when_unknown()
    test_render_record_is_bounded()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        sys.exit(1)
    print("all good")


if __name__ == "__main__":
    asyncio.run(main())
