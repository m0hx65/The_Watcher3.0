"""Verify the main-menu panel bump is suppressed during on-demand downloads.

Hand-rolled smoke test (no pytest). A fake bot + a toggleable download_active
flag drive PanelBumper directly, asserting it re-anchors the panel for sweep
notifications but never while a manual download is in flight (the duplicate-menu
bug).
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

from app.bot.handlers import PANEL_CHAT_ID, PANEL_MSG_ID  # noqa: E402
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


async def main() -> None:
    await test_bump_when_idle()
    await test_no_bump_during_download()
    await test_download_starting_mid_debounce_cancels_bump()
    await test_burst_collapses_to_one_bump()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        sys.exit(1)
    print("all good")


if __name__ == "__main__":
    asyncio.run(main())
