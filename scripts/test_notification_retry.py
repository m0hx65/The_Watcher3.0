"""Verify the notification send/retry logic, focused on the duplicate-upload bug.

Hand-rolled smoke test (no pytest). The production failure: a media upload
reaches Telegram but the response is slow, the request raises TimedOut, and the
old retry loop resent the same file 2â€“4Ã— (users got duplicate photos/videos).
The fix: uploads are not retried on TimedOut (assumed delivered), while text
sends and other Telegram errors keep retrying.
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

from telegram.error import RetryAfter, TimedOut  # noqa: E402

from app.bot.notifications import _UPLOAD_TIMEOUTS, NotificationDispatcher  # noqa: E402

FAILURES: list[str] = []

# A real on-disk file so the send methods' `open(path, "rb")` succeeds and the
# (mocked) bot.send_* call is actually reached.
_TMP = tempfile.NamedTemporaryFile(prefix="watcher-test-", suffix=".bin", delete=False)
_TMP.write(b"fake media bytes")
_TMP.close()
MEDIA = Path(_TMP.name)


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def make_dispatcher() -> NotificationDispatcher:
    bot = AsyncMock()
    return NotificationDispatcher(bot, chat_id=1)


async def test_upload_timeout_sends_once_no_retry() -> None:
    d = make_dispatcher()
    d.bot.send_photo = AsyncMock(side_effect=TimedOut("Timed out"))
    ok = await d.send_photo(MEDIA, caption="c")
    expect(
        "photo upload that times out is reported delivered (no false failure)",
        ok is True,
    )
    expect(
        "photo upload is attempted exactly once on timeout (no duplicate resend)",
        d.bot.send_photo.await_count == 1,
        f"await_count={d.bot.send_photo.await_count}",
    )


async def test_video_timeout_sends_once() -> None:
    d = make_dispatcher()
    d.bot.send_video = AsyncMock(side_effect=TimedOut("Timed out"))
    ok = await d.send_video(MEDIA, caption="c")
    expect("video timeout -> delivered, once", ok and d.bot.send_video.await_count == 1)


async def test_document_timeout_sends_once() -> None:
    d = make_dispatcher()
    d.bot.send_document = AsyncMock(side_effect=TimedOut("Timed out"))
    ok = await d.send_document(MEDIA, caption="c")
    expect(
        "document timeout -> delivered, once",
        ok and d.bot.send_document.await_count == 1,
    )


async def test_upload_passes_generous_timeouts() -> None:
    d = make_dispatcher()
    d.bot.send_photo = AsyncMock(return_value=None)
    await d.send_photo(MEDIA, caption="c")
    kwargs = d.bot.send_photo.call_args.kwargs
    expect(
        "upload passes the large write/read timeout kwargs",
        all(kwargs.get(k) == v for k, v in _UPLOAD_TIMEOUTS.items()),
        f"kwargs={kwargs}",
    )
    expect("write timeout is well above the 5s default", _UPLOAD_TIMEOUTS["write_timeout"] >= 60)


async def test_upload_success_path() -> None:
    d = make_dispatcher()
    d.bot.send_photo = AsyncMock(return_value=None)
    hook = AsyncMock()
    d.post_send_hook = hook
    ok = await d.send_photo(MEDIA)
    expect("successful photo returns True and fires the panel hook", ok and hook.await_count == 1)


async def test_text_timeout_still_retries() -> None:
    # Text sends are small and a timeout usually means it didn't go through, so
    # retrying is correct there â€” only uploads must avoid the resend.
    d = make_dispatcher()
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimedOut("Timed out")
        return None

    d.bot.send_message = AsyncMock(side_effect=flaky)
    ok = await d.send_text("hello")
    expect(
        "text send retries past a single timeout and ultimately succeeds",
        ok and calls["n"] == 2,
        f"calls={calls['n']}",
    )


async def test_retry_after_still_respected_for_uploads() -> None:
    # RetryAfter (rate limit) must still retry â€” only TimedOut is special-cased.
    d = make_dispatcher()
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RetryAfter(0)  # 0s so the test doesn't actually sleep long
        return None

    d.bot.send_photo = AsyncMock(side_effect=flaky)
    ok = await d.send_photo(MEDIA)
    expect(
        "upload retries after a RetryAfter and then succeeds",
        ok and calls["n"] == 2,
        f"calls={calls['n']}",
    )


async def main() -> int:
    await test_upload_timeout_sends_once_no_retry()
    await test_video_timeout_sends_once()
    await test_document_timeout_sends_once()
    await test_upload_passes_generous_timeouts()
    await test_upload_success_path()
    await test_text_timeout_still_retries()
    await test_retry_after_still_respected_for_uploads()

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
