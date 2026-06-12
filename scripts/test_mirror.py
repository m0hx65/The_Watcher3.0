"""Verify the dispatcher mirrors every message to extra chats.

Primary chat keeps its forum-topic thread; mirror chats get a flat copy
(thread None). Covers text + media, and that the primary's result is returned.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.bot.notifications import NotificationDispatcher  # noqa: E402

FAILURES: list[str] = []

_TMP = tempfile.NamedTemporaryFile(prefix="watcher-mirror-", suffix=".bin", delete=False)
_TMP.write(b"fake media")
_TMP.close()
MEDIA = Path(_TMP.name)


def expect(name: str, cond: bool, detail: str = "") -> None:
    print(("ok" if cond else "FAIL") + f": {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def calls(mock):
    """Return [(chat_id, message_thread_id)] for each call."""
    out = []
    for c in mock.await_args_list:
        out.append((c.kwargs.get("chat_id"), c.kwargs.get("message_thread_id")))
    return out


async def main() -> int:
    PRIMARY = -100123          # forum group
    MIRROR = 930123749         # DM

    # --- No mirrors: behaves exactly as before (single send) ---
    d0 = NotificationDispatcher(AsyncMock(), chat_id=PRIMARY)
    d0.bot.send_message = AsyncMock()
    await d0.send_text("hi", message_thread_id=77)
    expect("no-mirror: one send", len(calls(d0.bot.send_message)) == 1)
    expect("no-mirror: keeps thread", calls(d0.bot.send_message)[0] == (PRIMARY, 77))

    # --- With a mirror: primary keeps thread, mirror gets None ---
    d = NotificationDispatcher(AsyncMock(), chat_id=PRIMARY, mirror_chat_ids=[MIRROR])
    d.bot.send_message = AsyncMock()
    ok = await d.send_text("hello", message_thread_id=42)
    c = calls(d.bot.send_message)
    expect("text goes to both chats", len(c) == 2, str(c))
    expect("primary keeps its topic thread", (PRIMARY, 42) in c, str(c))
    expect("mirror is flat (no thread)", (MIRROR, None) in c, str(c))
    expect("returns primary delivery", ok is True)

    # --- Media mirrors too ---
    d.bot.send_photo = AsyncMock()
    await d.send_photo(MEDIA, caption="x", message_thread_id=42)
    cp = calls(d.bot.send_photo)
    expect("photo goes to both chats", len(cp) == 2, str(cp))
    expect("photo primary keeps thread", (PRIMARY, 42) in cp, str(cp))
    expect("photo mirror flat", (MIRROR, None) in cp, str(cp))

    # --- Multiple mirrors ---
    d2 = NotificationDispatcher(AsyncMock(), chat_id=PRIMARY, mirror_chat_ids=[MIRROR, 555])
    d2.bot.send_message = AsyncMock()
    await d2.send_text("multi")
    c2 = calls(d2.bot.send_message)
    expect("three destinations", len(c2) == 3, str(c2))
    expect("all mirrors flat", all(t is None for (cid, t) in c2 if cid != PRIMARY), str(c2))

    # --- A mirror failure doesn't sink the primary's result ---
    from telegram.error import TelegramError
    d3 = NotificationDispatcher(AsyncMock(), chat_id=PRIMARY, mirror_chat_ids=[MIRROR])

    async def flaky(*, chat_id, **kwargs):
        if chat_id == MIRROR:
            raise TelegramError("mirror down")
        return None

    d3.bot.send_message = AsyncMock(side_effect=flaky)
    ok3 = await d3.send_text("resilient")
    expect("primary ok despite mirror failure", ok3 is True)

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {FAILURES}")
        return 1
    print("\nall good")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
