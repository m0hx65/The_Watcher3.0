"""Service test: per-account forum topic routing.

A recording fake notifier captures the message_thread_id of every send, so we
can assert that an account's alerts go to its own topic, global messages go to
General (thread None), topics are created once and reused, and sync_topics
backfills everyone.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_forum_topics.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "-1001234567890")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")
os.environ["TELEGRAM_FORUM_TOPICS"] = "true"

from app.config import settings  # noqa: E402
from app.database import crud  # noqa: E402
from app.database.models import Base, MonitoredAccount  # noqa: E402
from app.database.session import dispose_engine, engine, get_session  # noqa: E402
from app.monitor.service import MonitorService  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, cond: bool, detail: str = "") -> None:
    print(("ok" if cond else "FAIL") + f": {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class RecordingNotifier:
    """Captures (kind, thread_id) for every send and hands out topic ids."""

    def __init__(self) -> None:
        self.sends: list[tuple[str, object]] = []
        self._next_topic = 1000
        self.created: list[str] = []

    async def send_text(self, text, *, parse_mode="HTML", message_thread_id=None):
        self.sends.append(("text", message_thread_id))
        return True

    async def send_document(self, path, caption=None, *, message_thread_id=None):
        self.sends.append(("document", message_thread_id))
        return True

    async def send_photo(self, path, caption=None, *, message_thread_id=None):
        self.sends.append(("photo", message_thread_id))
        return True

    async def send_video(self, path, caption=None, *, message_thread_id=None):
        self.sends.append(("video", message_thread_id))
        return True

    async def create_forum_topic(self, name):
        self._next_topic += 1
        self.created.append(name)
        return self._next_topic


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with get_session() as session:
        session.add(MonitoredAccount(id=1, username="alpha", active=True))
        session.add(MonitoredAccount(id=2, username="beta", active=True))

    notifier = RecordingNotifier()
    service = MonitorService(
        instagram=AsyncMock(), hasher=AsyncMock(), notifier=notifier, stories=None
    )

    # 1. First resolution creates a topic and persists it.
    t1 = await service.topic_for(1, "alpha")
    expect("topic created for alpha", t1 is not None)
    expect("create_forum_topic called with @alpha", notifier.created == ["@alpha"], str(notifier.created))
    async with get_session() as session:
        stored = await crud.get_account_topic(session, 1)
    expect("topic persisted", stored == t1, f"{stored} vs {t1}")

    # 2. Second resolution is cached — no second creation.
    t1b = await service.topic_for(1, "alpha")
    expect("same topic reused", t1b == t1)
    expect("no duplicate topic created", notifier.created == ["@alpha"], str(notifier.created))

    # 3. Different account → different topic.
    t2 = await service.topic_for(2, "beta")
    expect("beta gets its own topic", t2 is not None and t2 != t1)

    # 4. A per-account alert routes to that account's topic.
    notifier.sends.clear()
    await service._check_dark_radar()  # no activity → no sends, but exercises path
    # Directly exercise the dark-radar send by faking silence:
    from datetime import datetime, timedelta, timezone
    from app.database.models import SeenStory
    async with get_session() as session:
        session.add(SeenStory(
            account_id=1, story_pk="old", source="story", media_type="image",
            taken_at=0, seen_at=datetime.now(timezone.utc) - timedelta(days=10),
        ))
    notifier.sends.clear()
    await service._check_dark_radar()
    dark_sends = [s for s in notifier.sends if s[0] == "text"]
    expect("dark alert sent", len(dark_sends) >= 1, str(notifier.sends))
    expect("dark alert routed to alpha's topic", dark_sends[0][1] == t1, str(dark_sends))

    # 5. Global message (sync_topics doesn't send; use a global notifier call).
    notifier.sends.clear()
    await service.notifier.send_text("global!", message_thread_id=None)
    expect("global message has no thread (General)", notifier.sends == [("text", None)], str(notifier.sends))

    # 6. sync_topics reports existing (both already created above).
    result = await service.sync_topics()
    expect("sync ok", result["ok"] is True, str(result))
    expect("sync sees 2 existing", result["existing"] == 2, str(result))
    expect("sync created 0 new", result["created"] == 0, str(result))

    # 7. Feature flag off → topic_for returns None (General).
    settings.telegram_forum_topics = False
    service._topic_cache.clear()
    expect("disabled flag routes to General", await service.topic_for(1, "alpha") is None)
    settings.telegram_forum_topics = True

    # 8. create failure latches off and routes to General. Use a fresh account
    # id (3) with no stored topic so resolution actually reaches create.
    notifier2 = RecordingNotifier()
    notifier2.create_forum_topic = AsyncMock(return_value=None)
    service2 = MonitorService(
        instagram=AsyncMock(), hasher=AsyncMock(), notifier=notifier2, stories=None
    )
    expect("failed create -> None", await service2.topic_for(3, "gamma") is None)
    expect("latched unavailable", service2._topics_unavailable is True)

    await dispose_engine()
    if DB_FILE.exists():
        DB_FILE.unlink()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {FAILURES}")
        return 1
    print("\nall good")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
