"""Verify when the main-menu panel bump is suppressed.

Hand-rolled smoke test (no pytest). A fake bot + a toggleable download_active
flag drive PanelBumper directly, asserting it re-anchors the panel for sweep
notifications but never:

- while a manual download is in flight (the duplicate-menu bug), nor
- while the panel is showing a view the user just opened. Re-anchoring DELETES
  the panel, and the panel is where "Sweep running", Status and Dark radar are
  rendered — so 🔄 Sweep All used to open a view that the sweep's own first
  notification wiped a second later, before it could be read.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
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
    PANEL_VIEW_AT,
    PANEL_VIEW_GRACE_SECONDS,
    _note_panel_navigation,
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


def make_bumper(bot, *, download_active) -> tuple[PanelBumper, dict, list]:
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


async def test_no_bump_during_download() -> None:
    bot = make_bot()
    bumper, bot_data, persisted = make_bumper(bot, download_active=lambda: True)
    await bumper.schedule()
    await _settle()
    expect(
        "download: nothing deleted",
        bot.delete_message.await_count == 0,
        str(bot.delete_message.await_count),
    )
    expect(
        "download: no fresh panel posted",
        bot.send_message.await_count == 0,
        str(bot.send_message.await_count),
    )
    expect("download: panel id untouched", bot_data.get(PANEL_MSG_ID) == 100)
    expect("download: nothing persisted", persisted == [])


async def test_download_starting_mid_debounce_cancels_bump() -> None:
    # Scheduled while idle, but a download begins before the debounce fires — the
    # post-sleep re-check must still bail so the menu isn't dropped under media.
    bot = make_bot()
    active = {"v": False}
    bumper, bot_data, persisted = make_bumper(bot, download_active=lambda: active["v"])
    await bumper.schedule()  # idle at schedule time -> task queued
    active["v"] = True  # download starts during the debounce window
    await _settle()
    expect(
        "mid-debounce download: no panel posted",
        bot.send_message.await_count == 0,
        str(bot.send_message.await_count),
    )
    expect("mid-debounce download: panel id untouched", bot_data.get(PANEL_MSG_ID) == 100)


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


async def test_no_bump_while_a_view_is_being_read() -> None:
    """Pressing 🔄 Sweep All renders "Sweep running" INTO the panel, and the
    sweep's own first notification used to delete it a second later — the view
    vanished into a bare menu before it could be read."""
    bot = make_bot()
    bumper, bot_data, persisted = make_bumper(bot, download_active=lambda: False)
    bot_data[PANEL_VIEW_AT] = time.monotonic()  # a view was just opened
    await bumper.schedule()
    await _settle()
    expect("reading: the open view is not deleted",
           bot.delete_message.await_count == 0,
           str(bot.delete_message.await_count))
    expect("reading: no menu posted over it",
           bot.send_message.await_count == 0,
           str(bot.send_message.await_count))
    expect("reading: panel id untouched", bot_data.get(PANEL_MSG_ID) == 100)
    expect("reading: nothing persisted", persisted == [])


async def test_bump_resumes_after_the_grace_period() -> None:
    bot = make_bot()
    bumper, bot_data, _persisted = make_bumper(bot, download_active=lambda: False)
    # Opened longer ago than the grace period — the user has read it by now, so
    # the menu is allowed to return to the bottom of the chat.
    bot_data[PANEL_VIEW_AT] = time.monotonic() - PANEL_VIEW_GRACE_SECONDS - 1
    await bumper.schedule()
    await _settle()
    expect("expired grace: the panel is re-anchored",
           bot.send_message.await_count == 1,
           str(bot.send_message.await_count))
    expect("expired grace: the stale mark is cleared",
           PANEL_VIEW_AT not in bot_data, repr(bot_data.get(PANEL_VIEW_AT)))


async def test_bump_clears_the_view_mark() -> None:
    """After a re-anchor the panel IS the menu again, so a mark left behind
    would freeze the bumper for a whole grace period for no reason."""
    bot = make_bot()
    bumper, bot_data, _persisted = make_bumper(bot, download_active=lambda: False)
    await bumper.schedule()
    await _settle()
    expect("re-anchor leaves no view mark", PANEL_VIEW_AT not in bot_data,
           repr(bot_data.get(PANEL_VIEW_AT)))


def test_panel_navigation_marks_and_clears() -> None:
    """The mark is only set by presses on the panel itself, and 🏠 Home clears
    it so the menu can be re-anchored immediately."""
    bot_data = {PANEL_MSG_ID: 100, PANEL_CHAT_ID: 7}
    context = SimpleNamespace(application=SimpleNamespace(bot_data=bot_data))

    _note_panel_navigation(context, 100, "menu:sweep")
    expect("a panel button marks the panel as a view", PANEL_VIEW_AT in bot_data)

    _note_panel_navigation(context, 100, "menu:main")
    expect("Home clears the mark", PANEL_VIEW_AT not in bot_data)

    # A button on some OTHER message (an account card, a search result) must
    # not make the menu look like an open view.
    _note_panel_navigation(context, 555, "acc:open:1")
    expect("a press on another message leaves the panel alone",
           PANEL_VIEW_AT not in bot_data)


async def main() -> None:
    await test_bump_when_idle()
    await test_no_bump_during_download()
    await test_download_starting_mid_debounce_cancels_bump()
    await test_burst_collapses_to_one_bump()
    await test_no_bump_while_a_view_is_being_read()
    await test_bump_resumes_after_the_grace_period()
    await test_bump_clears_the_view_mark()
    test_panel_navigation_marks_and_clears()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        sys.exit(1)
    print("all good")


if __name__ == "__main__":
    asyncio.run(main())
