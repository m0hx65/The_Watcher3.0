"""Regression tests for the /kill download-cancellation path.

A huge on-demand download (e.g. an account with a mountain of highlight stories)
must stop the moment the user sends /kill: already-sent media stays, the rest is
skipped, and a scheduled sweep's auto-download is never affected.

Verifies:
  1. request_kill mid-delivery stops the loop early (only the items already in
     flight are sent) and the call still returns cleanly.
  2. request_kill is a no-op when nothing is downloading.
  3. The cancel flag is cleared once the download scope exits, so the next
     download starts clean.
  4. A non-cancellable (sweep) delivery ignores the flag entirely.

Runs offline on sqlite with fakes — no Telegram, no network.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_kill_download.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.database.models import Base  # noqa: E402
from app.database.session import engine  # noqa: E402
from app.monitor.service import MonitorService  # noqa: E402
from app.monitor.stories import StoryItem  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def _items(n: int, *, source: str = "highlight") -> list[StoryItem]:
    return [
        StoryItem(
            pk=f"{1000 + i}",
            taken_at=0,
            media_type="image",
            url=f"https://dl.snapcdn.app/x{i}",
            source=source,
            highlight_id="h:1",
            highlight_title="Trip",
        )
        for i in range(n)
    ]


class FakeStories:
    """Stands in for StoriesClient. download() returns a real temp file so the
    delivery loop behaves exactly like production."""

    def __init__(self, tmp: Path, catalog_items: list[StoryItem]) -> None:
        self._tmp = tmp
        self._catalog_items = catalog_items
        self.download_calls = 0

    async def fetch_highlight_items(self, username, highlight_id, title):
        return list(self._catalog_items)

    async def download(self, item: StoryItem, username: str) -> Path:
        self.download_calls += 1
        p = self._tmp / f"{item.pk}.jpg"
        p.write_bytes(b"\xff\xd8\xffmedia")
        return p


class KillOnNthNotifier:
    """Sends nothing real; after the Nth media send it fires /kill, simulating
    the user pressing it mid-download."""

    def __init__(self, service_box: dict, kill_after: int) -> None:
        self._box = service_box
        self._kill_after = kill_after
        self.photos = 0

    async def send_photo(self, path, caption=None, *, message_thread_id=None):
        self.photos += 1
        if self.photos == self._kill_after:
            self._box["service"].request_kill()
        return True

    async def send_video(self, path, caption=None, *, message_thread_id=None):
        return await self.send_photo(path, caption)

    async def send_text(self, *a, **k):
        return True

    async def create_forum_topic(self, *a, **k):
        return None


async def test_kill_mid_delivery() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        box: dict = {}
        items = _items(12)
        stories = FakeStories(Path(tmp), items)
        notifier = KillOnNthNotifier(box, kill_after=3)
        service = MonitorService(
            instagram=None, hasher=None, notifier=notifier, stories=stories
        )
        box["service"] = service

        # A 1-reel catalog whose reel holds 12 story items.
        result = await service.download_highlights_from_catalog(
            "victim", {"h:1": "Trip"}
        )

        expect("kill mid-download still returns ok", result.get("ok") is True, repr(result))
        expect(
            "stopped after the in-flight item (3 sent, not 12)",
            result.get("count") == 3,
            f"count={result.get('count')}, photos={notifier.photos}",
        )
        expect(
            "remaining items were skipped, not downloaded",
            stories.download_calls == 3,
            f"download_calls={stories.download_calls}",
        )
        expect(
            "cancel flag cleared after the scope exits",
            service.is_cancelling() is False,
        )
        expect(
            "no download is active after it returns",
            service.download_active is False,
        )


async def test_kill_during_gather() -> None:
    """A /kill pressed while the reel-fetch gather is still running aborts it
    before the (much longer) delivery phase even begins."""
    box: dict = {}

    class SlowStories:
        def __init__(self) -> None:
            self.download_calls = 0

        async def fetch_highlight_items(self, username, highlight_id, title):
            await asyncio.sleep(1.0)  # simulate a slow per-reel fetch
            return _items(5)

        async def download(self, item, username):
            self.download_calls += 1
            return None

    class PlainNotifier:
        async def send_photo(self, *a, **k):
            return True

        async def send_video(self, *a, **k):
            return True

    stories = SlowStories()
    service = MonitorService(
        instagram=None, hasher=None, notifier=PlainNotifier(), stories=stories
    )
    box["service"] = service

    catalog = {f"h:{i}": f"Reel {i}" for i in range(6)}
    task = asyncio.create_task(
        service.download_highlights_from_catalog("victim", catalog)
    )
    await asyncio.sleep(0.05)  # let the gather get going
    killed = service.request_kill()
    result = await asyncio.wait_for(task, timeout=2.0)

    expect("kill during gather was acknowledged", killed is True)
    expect("gather aborted -> nothing delivered", result.get("count") == 0, repr(result))
    expect("gather aborted -> no media downloaded", stories.download_calls == 0)
    expect("cancel flag cleared after the scope exits", service.is_cancelling() is False)


async def test_request_kill_noop_when_idle() -> None:
    service = MonitorService(instagram=None, hasher=None, notifier=None, stories=None)
    expect("request_kill returns False when nothing is downloading",
           service.request_kill() is False)
    expect("idle service reports no active download", service.download_active is False)


async def test_sweep_delivery_ignores_kill() -> None:
    """A non-cancellable (sweep) delivery must run to completion even if the
    cancel flag is set — /kill targets on-demand downloads only."""
    with tempfile.TemporaryDirectory() as tmp:
        items = _items(4, source="story")
        stories = FakeStories(Path(tmp), items)

        class PlainNotifier:
            def __init__(self):
                self.photos = 0

            async def send_photo(self, path, caption=None, *, message_thread_id=None):
                self.photos += 1
                return True

            async def send_video(self, path, caption=None, *, message_thread_id=None):
                return True

        notifier = PlainNotifier()
        service = MonitorService(
            instagram=None, hasher=None, notifier=notifier, stories=stories
        )
        # Force the cancel flag on as if a kill were pending.
        service._download_cancel.set()
        sent = await service._deliver_story_items(
            None, "victim", items, set(), cancellable=False
        )
        expect(
            "sweep delivery sends all items despite the flag",
            sent == 4 and notifier.photos == 4,
            f"sent={sent}, photos={notifier.photos}",
        )


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await test_kill_mid_delivery()
    await test_kill_during_gather()
    await test_request_kill_noop_when_idle()
    await test_sweep_delivery_ignores_kill()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All /kill cancellation tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
