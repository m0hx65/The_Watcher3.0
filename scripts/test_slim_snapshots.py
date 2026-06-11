"""Service-level smoke test: snapshots persist the SLIM raw_response form.

The full web_profile_info payload is 50-200 KB per row and was filling the
0.5 GB Neon tier; _handle_success must store only {data.user.id, reel_data}.
Also guards the regression where an unchanged sweep refreshed the latest row
with the full payload and silently dropped reel_data, and asserts that a
manual check_username() runs the SAME story/highlight phase as a scheduled
sweep (catalog stored + items baselined).

Runs on sqlite with fakes — no Telegram, no network.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_slim_snapshots.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from sqlalchemy import select  # noqa: E402

from app.database.models import (  # noqa: E402
    AccountSnapshot,
    Base,
    MonitoredAccount,
    SeenStory,
    StoredHighlight,
)
from app.database.session import dispose_engine, engine, get_session  # noqa: E402
from app.monitor.instagram import ProfileFetchResult  # noqa: E402
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


def make_fetch_result() -> ProfileFetchResult:
    parsed = {
        "username": "opscn1",
        "full_name": "JJ",
        "biography": "",
        "followers_count": 28,
        "following_count": 262,
        "posts_count": 0,
        "reels_count": 0,
        "story_count": 1,
        "is_private": False,
        "is_verified": False,
        "is_business": False,
        "profile_pic_url": None,  # skip the media hashing path entirely
        "external_url": None,
        "instagram_id": "40427049386",
    }
    # Simulate the heavy payload Instagram actually returns.
    raw = {
        "data": {
            "user": {
                "id": "40427049386",
                "username": "opscn1",
                "edge_owner_to_timeline_media": {
                    "edges": [{"node": {"display_url": "x" * 2000}} for _ in range(12)]
                },
                "edge_related_profiles": {"edges": [{"node": {"b": "y" * 500}}] * 20},
            }
        },
        "status": "ok",
    }
    return ProfileFetchResult(
        username="opscn1", http_status=200, parsed=parsed, raw_response=raw
    )


class FakeInstagram:
    def __init__(self) -> None:
        self.reel_calls = 0

    async def fetch_profile(self, username: str) -> ProfileFetchResult:
        return make_fetch_result()

    async def fetch_reel_user(self, user_id: str):
        self.reel_calls += 1
        return {
            "instagram_id": str(user_id),
            "username": "opscn1",
            "highlights": {"17843931795296435": "حسوني moshi"},
            "has_public_story": True,
            "is_live": False,
        }

    async def fetch_hd_pic_url(self, user_id: str):
        raise AssertionError(
            "fetch_hd_pic_url must not be called without a session cookie"
        )


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with get_session() as session:
        session.add(MonitoredAccount(username="opscn1", active=True))

    notifier = AsyncMock()
    notifier.send_text = AsyncMock(return_value=True)
    notifier.send_document = AsyncMock(return_value=True)

    service = MonitorService(
        instagram=FakeInstagram(),
        hasher=AsyncMock(hash_url=AsyncMock(return_value=None)),
        notifier=notifier,
        stories=None,
    )

    # --- First check: baseline snapshot must be SLIM ---
    result = await service.check_username("opscn1")
    expect("first check ok", result.get("ok") is True, repr(result))

    async with get_session() as session:
        rows = (await session.execute(select(AccountSnapshot))).scalars().all()
    expect("one snapshot stored", len(rows) == 1, f"got {len(rows)}")
    raw = rows[0].raw_response or {}
    raw_size = len(json.dumps(raw))
    expect("raw_response is slim (<2KB)", raw_size < 2048, f"{raw_size} bytes")
    expect(
        "slim form keeps the numeric id",
        ((raw.get("data") or {}).get("user") or {}).get("id") == "40427049386",
        repr(raw),
    )
    expect(
        "slim form keeps reel_data highlights",
        (raw.get("reel_data") or {}).get("highlights")
        == {"17843931795296435": "حسوني moshi"},
        repr(raw.get("reel_data")),
    )
    expect(
        "heavy timeline media is NOT stored",
        "edge_owner_to_timeline_media" not in json.dumps(raw),
    )

    # --- Second check, nothing changed: no new row, reel_data preserved ---
    result = await service.check_username("opscn1")
    expect("second check ok", result.get("ok") is True, repr(result))
    expect("second check unchanged", result.get("changed") is False, repr(result))

    async with get_session() as session:
        rows = (await session.execute(select(AccountSnapshot))).scalars().all()
    expect("still one snapshot row", len(rows) == 1, f"got {len(rows)}")
    raw = rows[0].raw_response or {}
    expect(
        "unchanged sweep keeps reel_data (regression)",
        (raw.get("reel_data") or {}).get("has_public_story") is True,
        repr(raw),
    )
    raw_size = len(json.dumps(raw))
    expect("refreshed row is still slim", raw_size < 2048, f"{raw_size} bytes")

    # --- Manual recheck runs the SAME story/highlight phase as a sweep ---
    class FakeStories:
        async def fetch_stories(self, username: str):
            return [
                StoryItem(pk="story1", taken_at=0, media_type="image",
                          url="http://x/1.jpg", source="story")
            ]

        async def fetch_highlight_items(self, username: str, hid: str, title: str):
            return [
                StoryItem(pk="hl1", taken_at=0, media_type="image",
                          url="http://x/2.jpg", source="highlight",
                          highlight_id=hid, highlight_title=title)
            ]

    service_with_stories = MonitorService(
        instagram=FakeInstagram(),
        hasher=AsyncMock(hash_url=AsyncMock(return_value=None)),
        notifier=notifier,
        stories=FakeStories(),
    )
    result = await service_with_stories.check_username("opscn1")
    expect("recheck with stories ok", result.get("ok") is True, repr(result))

    async with get_session() as session:
        highlights = (await session.execute(select(StoredHighlight))).scalars().all()
        seen = (await session.execute(select(SeenStory))).scalars().all()
    expect(
        "manual check stored the highlight catalog",
        [h.highlight_id for h in highlights] == ["17843931795296435"],
        repr([(h.highlight_id, h.title) for h in highlights]),
    )
    seen_pks = {s.story_pk for s in seen}
    expect(
        "manual check baselined story + highlight items",
        {"story1", "hl1"} <= seen_pks,
        repr(seen_pks),
    )

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
