"""Tests for the private→public auto-backlog grab.

When a monitored account flips from private to public, the bot must deliver its
whole backlog (posts, reels, highlights, story) instead of silently baselining
it. A pending-retry ledger makes a rate-limited transition recover on a later
sweep, bounded so a genuinely empty account can't retry forever.

Covers: the transition detector, the grab orchestration, the retry ledger, and
the check_username wiring. Runs offline on sqlite with fakes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_went_public.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.database import crud  # noqa: E402
from app.database.models import Base, MonitoredAccount  # noqa: E402
from app.database.session import engine, get_session  # noqa: E402
from app.monitor.change_detector import Change, ChangeSet  # noqa: E402
from app.monitor.instagram import ProfileFetchResult  # noqa: E402
from app.monitor.service import (  # noqa: E402
    _PUBLIC_GRAB_MAX_ATTEMPTS,
    MonitorService,
)

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def _cs(*changes: Change) -> ChangeSet:
    cs = ChangeSet(username="t")
    cs.changes.extend(changes)
    return cs


def _priv(old, new) -> Change:
    return Change(field="is_private", old=old, new=new, label="privacy")


def _make_service(stories=None) -> MonitorService:
    notifier = AsyncMock()
    notifier.send_text = AsyncMock(return_value=True)
    notifier.create_forum_topic = AsyncMock(return_value=None)
    return MonitorService(
        instagram=AsyncMock(), hasher=AsyncMock(),
        notifier=notifier, stories=stories if stories is not None else AsyncMock(),
    )


# ---------- 1. The transition detector ----------

def test_went_public_helper() -> None:
    expect("private→public is a transition",
           MonitorService._went_public(_cs(_priv(True, False))) is True)
    expect("public→private is NOT",
           MonitorService._went_public(_cs(_priv(False, True))) is False)
    expect("no privacy change is NOT",
           MonitorService._went_public(_cs()) is False)
    # 1/0 instead of bools (some drivers) still classifies right.
    expect("1→0 (int) counts as private→public",
           MonitorService._went_public(_cs(_priv(1, 0))) is True)
    expect("None→public is NOT (no prior private state)",
           MonitorService._went_public(_cs(_priv(None, False))) is False)


# ---------- 2. The grab orchestration ----------

async def test_grab_calls_all_sources() -> None:
    service = _make_service()
    service.download_posts = AsyncMock(return_value={"count": 4})
    service.download_all_highlights = AsyncMock(return_value={"count": 6})
    service.fetch_and_send_stories = AsyncMock(return_value={"count": 2})

    result = await service.grab_public_backlog(1, "target", instagram_id="99")

    expect("grab totals all three sources", result["total"] == 12, repr(result))
    expect("posts downloaded", service.download_posts.await_count == 1)
    expect("highlights downloaded", service.download_all_highlights.await_count == 1)
    expect("stories downloaded", service.fetch_and_send_stories.await_count == 1)
    # Announcement + summary both sent.
    texts = [c.args[0] for c in service.notifier.send_text.call_args_list]
    expect("announces going public", any("PUBLIC" in t for t in texts), str(texts))
    expect("summarizes the grab", any("backlog grabbed" in t for t in texts), str(texts))


async def test_grab_empty_reports_retry() -> None:
    service = _make_service()
    service.download_posts = AsyncMock(return_value={"count": 0})
    service.download_all_highlights = AsyncMock(return_value={"count": 0})
    service.fetch_and_send_stories = AsyncMock(return_value={"count": 0})

    result = await service.grab_public_backlog(1, "empty")
    expect("empty grab totals zero", result["total"] == 0)
    texts = [c.args[0] for c in service.notifier.send_text.call_args_list]
    expect("empty grab says it'll retry",
           any("retry" in t.lower() for t in texts), str(texts))


# ---------- 3. The retry ledger ----------

async def test_ledger_clears_on_success() -> None:
    service = _make_service()
    service.grab_public_backlog = AsyncMock(return_value={"total": 5})
    handled = await service._handle_public_backlog(10, "u", "99", went_public=True)
    expect("transition is handled", handled is True)
    async with get_session() as session:
        flag = await crud.get_setting(session, service._public_grab_key(10))
    expect("flag cleared after a delivering grab", flag is None, repr(flag))


async def test_ledger_retries_then_gives_up() -> None:
    service = _make_service()
    service.grab_public_backlog = AsyncMock(return_value={"total": 0})  # always empty

    # First sweep: the transition. Grab delivers nothing → flag persists at 2.
    handled = await service._handle_public_backlog(11, "u", None, went_public=True)
    expect("first empty attempt is handled", handled is True)
    async with get_session() as session:
        flag = await crud.get_setting(session, service._public_grab_key(11))
    expect("flag advanced after an empty grab", flag == "2", repr(flag))

    # Subsequent sweeps retry off the flag alone (went_public=False now).
    for _ in range(_PUBLIC_GRAB_MAX_ATTEMPTS + 2):
        await service._handle_public_backlog(11, "u", None, went_public=False)
    async with get_session() as session:
        flag = await crud.get_setting(session, service._public_grab_key(11))
    expect("flag cleared after max attempts (no infinite retry)", flag is None, repr(flag))


async def test_ledger_noop_when_nothing_pending() -> None:
    service = _make_service()
    service.grab_public_backlog = AsyncMock(return_value={"total": 9})
    handled = await service._handle_public_backlog(12, "u", None, went_public=False)
    expect("no transition + no flag → not handled", handled is False)
    expect("grab not run when nothing pending", service.grab_public_backlog.await_count == 0)


async def test_ledger_disabled_by_flag() -> None:
    from app.config import settings
    service = _make_service()
    service.grab_public_backlog = AsyncMock(return_value={"total": 9})
    original = settings.auto_grab_on_public
    try:
        settings.auto_grab_on_public = False
        handled = await service._handle_public_backlog(13, "u", None, went_public=True)
        expect("feature-off → not handled (normal phase runs)", handled is False)
        expect("feature-off → no grab", service.grab_public_backlog.await_count == 0)
    finally:
        settings.auto_grab_on_public = original


# ---------- 4. check_username wiring (integration) ----------

class TogglingInstagram:
    """Returns is_private=True on the first fetch, then False — a private
    account going public between two checks."""

    def __init__(self) -> None:
        self.calls = 0

    async def fetch_profile(self, username: str) -> ProfileFetchResult:
        self.calls += 1
        is_private = self.calls == 1
        return ProfileFetchResult(
            username=username, http_status=200,
            parsed={
                "username": username, "full_name": "T", "biography": "",
                "followers_count": 10, "following_count": 5, "posts_count": 3,
                "reels_count": 0, "story_count": 0, "is_private": is_private,
                "is_verified": False, "is_business": False,
                "profile_pic_url": None, "external_url": None,
                "instagram_id": "555",
            },
            raw_response={"data": {"user": {"id": "555"}}},
        )

    async def fetch_reel_user(self, user_id):
        return None


async def test_check_username_triggers_grab_on_flip() -> None:
    async with get_session() as session:
        session.add(MonitoredAccount(username="flipper", active=True))

    service = _make_service()
    service.instagram = TogglingInstagram()
    # Spy on the backlog handler and the normal story phase.
    calls: dict[str, list] = {"backlog": [], "story": []}

    async def spy_backlog(account_id, username, instagram_id, *, went_public):
        calls["backlog"].append(went_public)
        return True  # claim the account so the normal phase is skipped

    async def spy_story(account_id, username, *, instagram_id=None):
        calls["story"].append(username)

    service._handle_public_backlog = spy_backlog  # type: ignore[assignment]
    service._check_stories_and_highlights = spy_story  # type: ignore[assignment]

    # Sweep 1: account is private → neither the grab nor the story phase runs.
    await service.check_username("flipper")
    expect("private account: no backlog grab", calls["backlog"] == [], repr(calls))
    expect("private account: no story phase", calls["story"] == [], repr(calls))

    # Sweep 2: it's public now, having flipped → the backlog grab fires
    # (went_public=True) and the normal story phase is skipped.
    await service.check_username("flipper")
    expect("flip triggers the backlog grab", calls["backlog"] == [True], repr(calls))
    expect("normal story phase skipped on the flip", calls["story"] == [], repr(calls))


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    test_went_public_helper()
    await test_grab_calls_all_sources()
    await test_grab_empty_reports_retry()
    await test_ledger_clears_on_success()
    await test_ledger_retries_then_gives_up()
    await test_ledger_noop_when_nothing_pending()
    await test_ledger_disabled_by_flag()
    await test_check_username_triggers_grab_on_flip()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All went-public tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
