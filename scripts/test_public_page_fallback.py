"""Regression tests for the public-page fallback WIRING.

When web_profile_info answers 401 there is a second door: the plain profile
page, whose embedded Relay payload carries the same fields the API returns.
The parser itself is covered by test_public_page_payload.py — this file covers
what happens around it.

What must hold:
- the fallback runs only after the API is actually blocked, never before;
- a blocked page leaves the API's original error intact, never a bare success;
- a partial observation never blanks a field it couldn't see, never invents a
  change, and never turns a private account public;
- the two sources are diffed against their own history, because they can
  disagree about the same account at the same moment;
- the one-time purge removes the withdrawn og:-era rows and leaves the
  payload-era ones alone.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
DB_FILE = ROOT / "test_public_page.db"
if DB_FILE.exists():
    DB_FILE.unlink()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.config import settings  # noqa: E402
from app.database import crud  # noqa: E402
from app.database.models import MonitoredAccount  # noqa: E402
from app.monitor.change_detector import _is_meaningful_change  # noqa: E402
from app.monitor.instagram import InstagramClient  # noqa: E402
from app.monitor.public_page import parse_public_profile, strip_bidi  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def page_for(
    username: str = "rein__saad",
    full_name: str = "Reina Saad",
    *,
    followers: int = 1234,
    following: int = 567,
    pk: str = "7880052534",
) -> str:
    """A profile page shaped like the real capture: the stale og: block that
    shipped first, and the embedded payload the parser actually reads."""
    return (
        "<!DOCTYPE html><html><head>"
        f'<meta property="og:title" content="{full_name} (@{username})" />'
        # Deliberately WRONG, and deliberately present: a fixture where both
        # numbers agree could not catch the parser drifting back to the tags.
        '<meta property="og:description" content="9,999 Followers, 8,888 '
        'Following, 7,777 Posts" />'
        "</head><body><script type=\"application/json\" data-sjs>"
        '{"require":[["RelayPrefetchedStreamCache","next",[],[{"__bbox":'
        '{"result":{"data":{"xig_user_by_username":'
        f'{{"pk":"{pk}","username":"{username}",'
        '"profile_pic_url":"https:\\/\\/scontent.cdninstagram.com\\/v\\/'
        't51.2885-19\\/123_n.jpg","is_private":false,'
        f'"biography":"bio text","full_name":"{full_name}",'
        '"is_verified":false,"bio_links":[],'
        f'"follower_count":{followers},"following_count":{following},'
        '"all_media_count":89,"id":"17841407816045006"}'
        "}}}}]]]}</script></body></html>"
    )


PAGE = page_for()

# What a login wall looks like: no payload anywhere.
PAGE_LOGIN_WALL = """
<html><head>
<meta property="og:title" content="Instagram" />
<meta property="og:description" content="Create an account or log in to Instagram - Share what you're into with the people who get you." />
</head><body></body></html>
"""


def test_the_fixture_is_a_valid_page() -> None:
    """Guards the fixture itself: every wiring test below is meaningless if
    this page stops parsing, and it would fail in confusing ways instead."""
    parsed = parse_public_profile(PAGE, "rein__saad")
    expect("the fixture parses", parsed is not None)
    assert parsed
    expect("counts come from the payload, not the og: tags",
           (parsed.get("followers_count"), parsed.get("following_count"))
           == (1234, 567), repr(parsed))
    expect("the numeric id is pk", parsed.get("instagram_id") == "7880052534",
           repr(parsed.get("instagram_id")))


def test_unknown_text_field_is_not_a_change() -> None:
    """The heart of the safety rule: 'I could not see the bio' must never be
    reported as 'the bio was cleared'. A real clearing arrives as "", which
    still reports."""
    expect("unknown bio is not a change",
           not _is_meaningful_change("biography", "some bio", None))
    expect("unknown bio is not a change either way",
           not _is_meaningful_change("biography", None, "some bio"))
    expect("a genuinely cleared bio IS a change",
           _is_meaningful_change("biography", "some bio", ""))
    expect("a real edit is still a change",
           _is_meaningful_change("biography", "old", "new"))


def test_a_carried_forward_field_never_alerts() -> None:
    """The page cannot see the post count (all_media_count is null for public
    and private accounts alike), so a page snapshot stores the last known one.
    That value came from the API, which already alerted on it — diffing it on
    the page's own timeline reports the same change a second time."""
    from app.database.models import AccountSnapshot
    from app.monitor.change_detector import detect_changes

    # Previous page reading: inherited posts=101 from the API at the time.
    previous = AccountSnapshot(
        username="dup", http_status=200, followers_count=10, posts_count=101,
    )
    # This page reading saw followers only; posts=102 was inherited from a
    # later API check that ALREADY announced 101 -> 102.
    current = AccountSnapshot(
        username="dup", http_status=200, followers_count=10, posts_count=102,
    )
    observed = {"username", "followers_count", "following_count", "is_private"}

    naive = detect_changes(previous, current)
    expect("without the guard the carried value looks like a change",
           naive.find("posts_count") is not None)

    guarded = detect_changes(previous, current, observed_fields=observed)
    expect("an unobserved field never alerts",
           guarded.find("posts_count") is None,
           repr([c.field for c in guarded.changes]))
    expect("and nothing else is invented", not guarded.has_changes,
           repr([c.field for c in guarded.changes]))

    # What the page DID see still reports normally.
    moved = AccountSnapshot(
        username="dup", http_status=200, followers_count=12, posts_count=102,
    )
    real = detect_changes(previous, moved, observed_fields=observed)
    expect("an observed field still alerts",
           real.find("followers_count") is not None,
           repr([c.field for c in real.changes]))


class _MockResponse:
    def __init__(self, status_code: int, body: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text

    def json(self) -> Any:
        return self._body


class _MockSession:
    def __init__(self, handler: Callable[[str, dict, dict], _MockResponse]) -> None:
        self.handler = handler
        self.urls: list[str] = []

    async def get(self, url: str, *, params: Any = None, headers: Any = None):
        self.urls.append(url)
        return self.handler(url, dict(params or {}), dict(headers or {}))

    async def close(self) -> None:
        pass


async def test_purge_removes_only_the_og_era_rows() -> None:
    """The withdrawn og: parser's rows have to go — the card reads the newest
    snapshot, so leaving one means it keeps stating a wrong number as current.
    The payload parser's rows must NOT: they matched ground truth, and a purge
    that ran on every boot would delete good data forever."""
    from datetime import timedelta

    from app.database.models import AccountSnapshot, Base
    from app.database.session import engine, get_session

    before_cutoff = crud.OG_ERA_CUTOFF.replace(tzinfo=None) - timedelta(days=1)
    after_cutoff = crud.OG_ERA_CUTOFF.replace(tzinfo=None) + timedelta(days=8)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with get_session() as session:
        account = MonitoredAccount(username="purgeme", active=True)
        session.add(account)
        await session.flush()
        account_id = account.id
        session.add(AccountSnapshot(
            account_id=account_id, username="purgeme", http_status=200,
            following_count=577, raw_response={"data": {"user": {"id": "9"}}},
            created_at=before_cutoff,
        ))
        await session.flush()
        # The wrong number the og: block reported for a real account.
        session.add(AccountSnapshot(
            account_id=account_id, username="purgeme", http_status=200,
            following_count=677, raw_response={"source": "public_page"},
            created_at=before_cutoff,
        ))
        await session.flush()
        # A payload-era reading of the same account: same source marker, but
        # written after the parser was fixed.
        session.add(AccountSnapshot(
            account_id=account_id, username="purgeme", http_status=200,
            following_count=577, raw_response={"source": "public_page"},
            created_at=after_cutoff,
        ))

    async with get_session() as session:
        removed = await crud.purge_partial_snapshots(session)
    async with get_session() as session:
        latest = await crud.get_latest_snapshot(session, account_id)
        survivors = await crud.recent_snapshots(session, account_id, limit=10)

    expect("og:-era partial rows are removed", removed >= 1, repr(removed))
    expect("the wrong number is gone",
           all(row.following_count != 677 for row in survivors),
           repr([row.following_count for row in survivors]))
    expect("the payload-era partial row survives",
           any(crud.snapshot_source(row) == "public_page" for row in survivors),
           repr([crud.snapshot_source(row) for row in survivors]))
    expect("the authoritative row survives",
           any(crud.snapshot_source(row) is None for row in survivors))
    expect("and the card reads a correct number",
           latest is not None and latest.following_count == 577,
           repr(latest and latest.following_count))

    async with get_session() as session:
        again = await crud.purge_partial_snapshots(session)
    expect("a second run is a no-op", again == 0, repr(again))
    await engine.dispose()


async def test_a_blocked_api_falls_through_to_the_page() -> None:
    """The second door, and the reason it exists: the API 401s from a
    datacenter IP while the page still answers. The reading is marked partial
    so nothing downstream mistakes it for a full one."""
    def handler(url: str, params: dict, headers: dict) -> _MockResponse:
        if "instagram.com/rein__saad/" in url:
            return _MockResponse(200, text=PAGE)
        return _MockResponse(401, {})

    session = _MockSession(handler)
    old = settings.ig_proxy_url
    settings.ig_proxy_url = "https://ig-proxy.example.workers.dev"
    try:
        async with InstagramClient(max_retries=5, session=session) as client:
            result = await client.fetch_profile("rein__saad", auth_attempts=1)
    finally:
        settings.ig_proxy_url = old

    expect("the page answers when the API is blocked", result.success,
           repr(result.error))
    expect("and is labelled partial", result.partial and result.source == "public_page",
           result.source)
    expect("with the payload's counts",
           (result.parsed or {}).get("following_count") == 567,
           repr(result.parsed))
    expect("the page is fetched directly, not through the worker",
           any(u.startswith("https://www.instagram.com/rein__saad/")
               for u in session.urls),
           repr(session.urls))


async def test_probe_measures_each_door_alone() -> None:
    """/probe reports the API and the page separately. If the API call fell
    through internally, a blocked API would still print as reachable."""
    def handler(url: str, params: dict, headers: dict) -> _MockResponse:
        if "instagram.com/rein__saad/" in url:
            return _MockResponse(200, text=PAGE)
        return _MockResponse(401, {})

    session = _MockSession(handler)
    old = settings.ig_proxy_url
    settings.ig_proxy_url = "https://ig-proxy.example.workers.dev"
    try:
        async with InstagramClient(max_retries=5, session=session) as client:
            result = await client.fetch_profile(
                "rein__saad", auth_attempts=1, allow_fallback=False
            )
    finally:
        settings.ig_proxy_url = old

    expect("allow_fallback=False reports the block", not result.success)
    expect("the 401 is preserved", result.http_status == 401, repr(result.http_status))
    expect("and the page is not even fetched",
           not any("instagram.com/rein__saad/" in u for u in session.urls),
           repr(session.urls))


async def test_no_fallback_when_the_api_answers() -> None:
    payload = {
        "data": {"user": {
            "username": "rein__saad", "full_name": "R", "biography": "bio",
            "edge_followed_by": {"count": 10}, "edge_follow": {"count": 20},
            "edge_owner_to_timeline_media": {"count": 3},
            "is_private": False, "is_verified": False,
            "is_business_account": False, "id": "1",
        }}
    }
    session = _MockSession(lambda url, p, h: _MockResponse(200, payload))
    async with InstagramClient(max_retries=2, session=session) as client:
        result = await client.fetch_profile("rein__saad")

    expect("a healthy API needs no fallback", result.success and not result.partial,
           result.source)
    expect("exactly one request", len(session.urls) == 1, repr(session.urls))


async def test_blocked_page_keeps_the_original_error() -> None:
    """Both doors shut must report the block — never a success with no data."""
    session = _MockSession(lambda url, p, h: _MockResponse(401, {}, text=PAGE_LOGIN_WALL))
    old = settings.ig_proxy_url
    settings.ig_proxy_url = "https://ig-proxy.example.workers.dev"
    try:
        async with InstagramClient(max_retries=5, session=session) as client:
            result = await client.fetch_profile("nobody", auth_attempts=1)
    finally:
        settings.ig_proxy_url = old

    expect("both doors shut = failure", not result.success)
    expect("the 401 is preserved", result.http_status == 401, repr(result.http_status))
    expect("no invented data", result.parsed is None, repr(result.parsed))


def test_bidi_marks_never_alert() -> None:
    """Instagram's og:title wraps RTL names in invisible direction marks; the
    API doesn't. Alternating between the sources reported "Full name changed
    لِ → ‎لِ‎" — two visibly identical strings."""
    plain = "لِ"
    wrapped = "‎لِ‎"
    expect("the marks are stripped at parse time", strip_bidi(wrapped) == plain,
           repr(strip_bidi(wrapped)))
    expect("and a marks-only difference is not a change",
           not _is_meaningful_change("full_name", plain, wrapped))
    expect("a real rename still reports",
           _is_meaningful_change("full_name", plain, "رالا"))

    parsed = parse_public_profile(page_for(full_name=wrapped), "rein__saad")
    assert parsed
    expect("the parsed name carries no marks", parsed.get("full_name") == plain,
           repr(parsed.get("full_name")))


async def test_sources_are_diffed_separately() -> None:
    """The API and the public page disagree about the same account at the same
    moment. Diffing one against the other reported a change on every flip — in
    both directions, forever."""
    from app.database.models import AccountSnapshot, Base
    from app.database.session import engine, get_session
    from app.database import crud

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with get_session() as session:
        account = MonitoredAccount(username="flipflop", active=True)
        session.add(account)
        await session.flush()
        account_id = account.id
        # An API reading, then a page reading that disagrees, then API again.
        session.add(AccountSnapshot(
            account_id=account_id, username="flipflop", http_status=200,
            following_count=103, raw_response={"data": {"user": {"id": "1"}}},
        ))
        await session.flush()
        session.add(AccountSnapshot(
            account_id=account_id, username="flipflop", http_status=200,
            following_count=124, raw_response={"source": "public_page"},
        ))
        await session.flush()

    async with get_session() as session:
        api_prev = await crud.get_latest_snapshot_by_source(
            session, account_id, source=None
        )
        page_prev = await crud.get_latest_snapshot_by_source(
            session, account_id, source="public_page"
        )
        newest = await crud.get_latest_snapshot(session, account_id)

    expect("the API baseline is the API row",
           api_prev is not None and api_prev.following_count == 103,
           repr(api_prev and api_prev.following_count))
    expect("the page baseline is the page row",
           page_prev is not None and page_prev.following_count == 124,
           repr(page_prev and page_prev.following_count))
    expect("last-known is still the newest of either",
           newest is not None and newest.following_count == 124)
    expect("a page row is identified as partial",
           crud.snapshot_source(page_prev) == "public_page")
    expect("an API row is identified as authoritative",
           crud.snapshot_source(api_prev) is None)

    await engine.dispose()


async def test_partial_reading_keeps_a_private_account_private() -> None:
    """`bool(None)` called a private account public, so private targets entered
    the story phase and got a "⭕ NO STORY" line — for an account whose stories
    the bot cannot and must not see."""
    from unittest.mock import AsyncMock

    from app.database.models import AccountSnapshot, Base
    from app.database.session import engine, get_session
    from app.monitor.instagram import ProfileFetchResult
    from app.monitor.service import MonitorService

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with get_session() as session:
        account = MonitoredAccount(
            username="privateuser", active=True, instagram_id="555"
        )
        session.add(account)
        await session.flush()
        account_id = account.id
        session.add(AccountSnapshot(
            account_id=account_id, username="privateuser", http_status=200,
            is_private=True, followers_count=10, biography="hi",
            raw_response={"data": {"user": {"id": "555"}}},
        ))

    service = MonitorService(
        instagram=AsyncMock(), hasher=AsyncMock(),
        notifier=AsyncMock(), stories=AsyncMock(),
    )
    service.notifier.send_text = AsyncMock(return_value=True)
    service.hasher.hash_url = AsyncMock(return_value=None)

    # The payload normally carries is_private, and that is the real fix for
    # this bug. This fixture omits it anyway: the rule under test is that an
    # ABSENT field falls back to the last known value rather than to
    # bool(None), and it has to hold for whatever the page fails to include.
    fetch = ProfileFetchResult(
        "privateuser", 200,
        parsed={"username": "privateuser", "followers_count": 11,
                "following_count": 5, "posts_count": 2},
        source="public_page",
    )
    result = await service._handle_success(account_id, "privateuser", fetch, False)

    expect("a partial reading does not call a private account public",
           result["is_private"] is True, repr(result["is_private"]))
    # The page cannot see the bio; the previous one must survive the write and
    # must not be reported as a change.
    async with get_session() as session:
        latest = await crud.get_latest_snapshot(session, account_id)
    expect("the unseen bio is carried forward, not blanked",
           latest is not None and latest.biography == "hi",
           repr(latest and latest.biography))
    expect("the fresh count from the page IS stored",
           latest is not None and latest.followers_count == 11,
           repr(latest and latest.followers_count))
    await engine.dispose()


async def main() -> int:
    test_the_fixture_is_a_valid_page()
    test_unknown_text_field_is_not_a_change()
    test_a_carried_forward_field_never_alerts()
    test_bidi_marks_never_alert()
    await test_sources_are_diffed_separately()
    await test_partial_reading_keeps_a_private_account_private()
    await test_a_blocked_api_falls_through_to_the_page()
    await test_probe_measures_each_door_alone()
    await test_purge_removes_only_the_og_era_rows()
    await test_no_fallback_when_the_api_answers()
    await test_blocked_page_keeps_the_original_error()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All public-page fallback tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
