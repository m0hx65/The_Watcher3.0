"""Tests for the private→public auto-backlog grab.

When a monitored account flips from private to public, the bot must deliver its
backlog (posts, reels, highlights, story) instead of silently baselining it —
and ONLY what the chat hasn't already received. An account that flips
private/public repeatedly must never get its whole media re-sent (that flood is
what this suite exists to prevent). A pending-retry ledger makes a rate-limited
transition recover on a later sweep, bounded so a genuinely empty account can't
retry forever; a grab that listed the account but found nothing new is a
success, not a retry.

A flip also costs ONE message, not two: the profile change card already says
the account went public, so a grab with nothing new to send stays quiet.

Covers: the transition detector, the dedup-aware grab, the one-line collapse,
the retry ledger, the concurrent-grab guard, and the check_username wiring
(including a full private/public flip-flop end to end). Runs offline on sqlite
with fakes.
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
from app.monitor.instagram import IdProbe, ProfileFetchResult  # noqa: E402
from app.monitor.service import (  # noqa: E402
    _PUBLIC_GRAB_MAX_ATTEMPTS,
    MonitorService,
)
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


def _cs(*changes: Change) -> ChangeSet:
    cs = ChangeSet(username="t")
    cs.changes.extend(changes)
    return cs


def _priv(old, new) -> Change:
    return Change(field="is_private", old=old, new=new, label="privacy")


# ---------- Fakes ----------

def _item(
    pk: str,
    source: str = "post",
    media_type: str = "image",
    hid: str | None = None,
    title: str | None = None,
) -> StoryItem:
    return StoryItem(
        pk=pk, taken_at=0, media_type=media_type,
        url=f"https://cdn.test/{pk}", source=source,
        highlight_id=hid, highlight_title=title,
    )


class FakeStories:
    """Stand-in for StoriesClient: scripted listings, downloads always succeed.

    `highlights` is {highlight_id: (title, [StoryItem, ...])}. `downloaded`
    records every pk handed to download(), so a test can prove each item was
    fetched exactly once across however many grabs."""

    def __init__(self, posts=(), highlights=None, stories=()) -> None:
        self.posts: list[StoryItem] = list(posts)
        self.highlights: dict[str, tuple[str, list[StoryItem]]] = dict(highlights or {})
        self.stories: list[StoryItem] = list(stories)
        self.downloaded: list[str] = []
        self.on_fetch_posts = None  # optional hook, e.g. to fire /kill mid-listing

    async def fetch_posts(self, username: str, limit: int = 12) -> list[StoryItem]:
        if self.on_fetch_posts is not None:
            self.on_fetch_posts()
        return list(self.posts)

    async def fetch_stories(self, username: str) -> list[StoryItem]:
        return list(self.stories)

    async def fetch_highlight_items(self, username: str, hid: str, title: str) -> list[StoryItem]:
        return list(self.highlights.get(hid, (title, []))[1])

    async def download(self, item: StoryItem, username: str) -> Path:
        self.downloaded.append(item.pk)
        return Path(f"fake/{item.pk}.bin")

    def listing(self) -> dict:
        """What list_highlights would answer for this catalog."""
        items = [(hid, title) for hid, (title, _) in self.highlights.items()]
        return {"ok": True, "items": items, "untracked": set(),
                "monitored": True, "error": None}


def _make_service(stories=None) -> MonitorService:
    notifier = AsyncMock()
    notifier.send_text = AsyncMock(return_value=True)
    notifier.send_photo = AsyncMock(return_value=True)
    notifier.send_video = AsyncMock(return_value=True)
    notifier.create_forum_topic = AsyncMock(return_value=None)
    service = MonitorService(
        instagram=AsyncMock(), hasher=AsyncMock(),
        notifier=notifier, stories=stories if stories is not None else AsyncMock(),
    )
    if isinstance(stories, FakeStories):
        # The catalog normally comes from Instagram's reel query; answer it
        # from the fake so no network shape is needed.
        service.list_highlights = AsyncMock(return_value=stories.listing())  # type: ignore[method-assign]
    return service


def _media_sent(service: MonitorService) -> int:
    return service.notifier.send_photo.await_count + service.notifier.send_video.await_count


def _texts(service: MonitorService) -> list[str]:
    return [c.args[0] for c in service.notifier.send_text.call_args_list]


def _reset_notifier(service: MonitorService) -> None:
    service.notifier.send_text.reset_mock()
    service.notifier.send_photo.reset_mock()
    service.notifier.send_video.reset_mock()


async def _add_account(username: str) -> int:
    async with get_session() as session:
        account = MonitoredAccount(username=username, active=True)
        session.add(account)
        await session.flush()
        return account.id


async def _seen(account_id: int) -> set[str]:
    async with get_session() as session:
        return await crud.get_seen_story_pks(session, account_id)


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


# ---------- 2. The grab: first time everything, afterwards only what's new ----------

async def test_grab_flap_never_resends() -> None:
    """The flood scenario: grab, grab again, grab again. Media goes out once."""
    aid = await _add_account("flapper_grab")
    fake = FakeStories(
        posts=[_item("p1"), _item("p2", media_type="video")],
        highlights={"h1": ("Trips", [_item("hl1", source="highlight", hid="h1", title="Trips")])},
        stories=[_item("s1", source="story")],
    )
    service = _make_service(fake)

    # First grab: nothing has ever been delivered → the whole account.
    result = await service.grab_public_backlog(aid, "flapper_grab", instagram_id="99")
    expect("first grab sends everything", result["total"] == 4, repr(result))
    expect("first grab per-source counts",
           (result["posts"], result["highlights"], result["stories"]) == (2, 1, 1), repr(result))
    expect("first grab listed 4, skipped 0",
           result["fetched"] == 4 and result["skipped"] == 0, repr(result))
    expect("4 media messages went out", _media_sent(service) == 4, str(_media_sent(service)))
    texts = _texts(service)
    expect("first grab announces the whole account",
           any("whole account" in t for t in texts), str(texts))
    expect("first grab summarizes", any("backlog grabbed" in t for t in texts), str(texts))
    expect("everything is marked seen", await _seen(aid) == {"p1", "p2", "hl1", "s1"},
           str(await _seen(aid)))
    async with get_session() as session:
        clock = await crud.get_setting(session, service._highlight_scan_key(aid))
    expect("highlight re-scan clock stamped (reels were all just listed)",
           clock is not None)

    # Second grab (the account flipped private and public again): nothing new.
    _reset_notifier(service)
    result = await service.grab_public_backlog(aid, "flapper_grab", instagram_id="99")
    expect("re-grab sends NOTHING", result["total"] == 0, repr(result))
    expect("re-grab still listed the account (not a failure)",
           result["fetched"] == 4 and result["skipped"] == 4, repr(result))
    expect("re-grab: zero media messages", _media_sent(service) == 0, str(_media_sent(service)))
    texts = _texts(service)
    expect("re-grab: exactly one text line", len(texts) == 1, str(texts))
    expect("re-grab says nothing new", texts and "nothing new" in texts[0], str(texts))
    expect("re-grab did not re-download", fake.downloaded == ["p1", "p2", "hl1", "s1"],
           str(fake.downloaded))

    # Third grab, after two items appeared while it was private: only those.
    fake.posts.append(_item("p3"))
    fake.stories.append(_item("s2", source="story", media_type="video"))
    _reset_notifier(service)
    result = await service.grab_public_backlog(aid, "flapper_grab", instagram_id="99")
    expect("third grab sends only the 2 new items", result["total"] == 2, repr(result))
    expect("third grab counts: 1 post, 0 highlight, 1 story",
           (result["posts"], result["highlights"], result["stories"]) == (1, 0, 1), repr(result))
    expect("third grab skipped the 4 old ones", result["skipped"] == 4, repr(result))
    expect("2 media messages for the new items", _media_sent(service) == 2, str(_media_sent(service)))
    texts = _texts(service)
    expect("third grab announces what's new, not the whole account",
           any("what's new" in t and "4 already delivered" in t for t in texts), str(texts))
    expect("new items now seen too", await _seen(aid) == {"p1", "p2", "hl1", "s1", "p3", "s2"},
           str(await _seen(aid)))

    # Fourth grab: back to nothing new — the seen set keeps growing, never resets.
    _reset_notifier(service)
    result = await service.grab_public_backlog(aid, "flapper_grab", instagram_id="99")
    expect("fourth grab sends nothing again", result["total"] == 0 and result["skipped"] == 6,
           repr(result))
    expect("each item was downloaded exactly once over four grabs",
           sorted(fake.downloaded) == ["hl1", "p1", "p2", "p3", "s1", "s2"], str(fake.downloaded))


async def test_grab_respects_baseline() -> None:
    """Items baselined by the normal phase (added while public) count as seen."""
    aid = await _add_account("baselined")
    fake = FakeStories(posts=[_item("b1"), _item("b2")])
    async with get_session() as session:
        await crud.mark_story_items_seen(session, aid, [_item("b1")])
    service = _make_service(fake)
    result = await service.grab_public_backlog(aid, "baselined")
    expect("baselined item is not re-sent; the unseen one is",
           result["total"] == 1 and result["skipped"] == 1, repr(result))
    expect("only b2 downloaded", fake.downloaded == ["b2"], str(fake.downloaded))


async def test_grab_empty_reports_retry() -> None:
    aid = await _add_account("empty_grab")
    service = _make_service(FakeStories())

    result = await service.grab_public_backlog(aid, "empty_grab")
    expect("empty grab totals zero", result["total"] == 0 and result["fetched"] == 0, repr(result))
    texts = _texts(service)
    expect("empty grab says it'll retry",
           any("retry" in t.lower() for t in texts), str(texts))

    _reset_notifier(service)
    result = await service.grab_public_backlog(aid, "empty_grab", final_attempt=True)
    texts = _texts(service)
    expect("final empty attempt says it's giving up, not retrying",
           any("giving up" in t for t in texts) and not any("it'll retry" in t for t in texts),
           str(texts))


async def test_grab_quiet_when_card_announced() -> None:
    """A flip costs ONE line, not two.

    The profile change card already tells the chat an account went public, so a
    grab with nothing new to send must add nothing. It must still speak when it
    delivers, and when the sources didn't answer (a retry is coming)."""
    aid = await _add_account("quiet_flap")
    fake = FakeStories(posts=[_item("q1")])
    service = _make_service(fake)

    # First flip: something new, so the grab speaks regardless.
    result = await service.grab_public_backlog(
        aid, "quiet_flap", transition_announced=True
    )
    expect("announced flip still delivers new media", result["total"] == 1, repr(result))
    expect("delivering grab still speaks", len(_texts(service)) > 0)

    # Second flip: nothing new AND the card already said it went public.
    _reset_notifier(service)
    result = await service.grab_public_backlog(
        aid, "quiet_flap", transition_announced=True
    )
    expect("announced flip with nothing new sends no media",
           result["total"] == 0 and _media_sent(service) == 0, repr(result))
    expect("announced flip with nothing new says NOTHING",
           _texts(service) == [], str(_texts(service)))
    expect("still reports what it listed (so no retry is scheduled)",
           result["fetched"] == 1 and result["skipped"] == 1, repr(result))

    # Same state, but nobody announced it (a pending retry): the grab speaks.
    _reset_notifier(service)
    await service.grab_public_backlog(aid, "quiet_flap", transition_announced=False)
    expect("unannounced flip with nothing new does speak",
           any("nothing new" in t for t in _texts(service)), str(_texts(service)))

    # Sources silent: worth a line even when the card announced the flip,
    # because it tells the user a retry is coming.
    _reset_notifier(service)
    silent = _make_service(FakeStories())
    aid2 = await _add_account("quiet_empty")
    await silent.grab_public_backlog(aid2, "quiet_empty", transition_announced=True)
    expect("announced flip STILL reports a failed grab",
           any("retry" in t.lower() for t in _texts(silent)), str(_texts(silent)))


async def test_grab_kill_during_listing() -> None:
    """/kill while the sources are still being listed: nothing sent, no chatter."""
    aid = await _add_account("killed")
    fake = FakeStories(posts=[_item("k1"), _item("k2")])
    service = _make_service(fake)
    fake.on_fetch_posts = service.request_kill

    result = await service.grab_public_backlog(aid, "killed")
    expect("killed grab sends nothing", result["total"] == 0, repr(result))
    expect("killed grab still reports what it listed (no retry scheduled)",
           result["fetched"] == 2, repr(result))
    expect("killed grab is silent (the /kill handler already spoke)",
           _texts(service) == [] and _media_sent(service) == 0, str(_texts(service)))
    expect("nothing marked seen by a killed grab", await _seen(aid) == set(),
           str(await _seen(aid)))


# ---------- 3. The retry ledger ----------

async def test_ledger_clears_on_success() -> None:
    service = _make_service()
    service.grab_public_backlog = AsyncMock(return_value={"total": 5, "fetched": 5, "skipped": 0})
    handled = await service._handle_public_backlog(10, "u", "99", went_public=True)
    expect("transition is handled", handled is True)
    async with get_session() as session:
        flag = await crud.get_setting(session, service._public_grab_key(10))
    expect("flag cleared after a delivering grab", flag is None, repr(flag))


async def test_ledger_clears_when_nothing_new() -> None:
    """A grab that listed the account but had nothing new is settled — it must
    NOT retry on later sweeps (that was the second half of the flood)."""
    service = _make_service()
    service.grab_public_backlog = AsyncMock(return_value={"total": 0, "fetched": 6, "skipped": 6})
    handled = await service._handle_public_backlog(14, "u", None, went_public=True)
    expect("nothing-new grab is handled", handled is True)
    async with get_session() as session:
        flag = await crud.get_setting(session, service._public_grab_key(14))
    expect("flag cleared after a nothing-new grab", flag is None, repr(flag))
    # Next sweep, no transition: nothing pending → normal phase, no re-grab.
    handled = await service._handle_public_backlog(14, "u", None, went_public=False)
    expect("no retry after a nothing-new grab", handled is False)
    expect("grab ran exactly once", service.grab_public_backlog.await_count == 1)


async def test_ledger_retries_then_gives_up() -> None:
    service = _make_service()
    service.grab_public_backlog = AsyncMock(
        return_value={"total": 0, "fetched": 0, "skipped": 0}  # source never answers
    )

    # First sweep: the transition. Grab lists nothing → flag persists at 2.
    handled = await service._handle_public_backlog(11, "u", None, went_public=True)
    expect("first empty attempt is handled", handled is True)
    async with get_session() as session:
        flag = await crud.get_setting(session, service._public_grab_key(11))
    expect("flag advanced after an empty grab", flag == "2", repr(flag))
    expect("first attempt is not flagged final",
           service.grab_public_backlog.await_args.kwargs.get("final_attempt") is False)
    expect("a real transition is flagged as already announced by the card",
           service.grab_public_backlog.await_args.kwargs.get("transition_announced") is True)

    # Subsequent sweeps retry off the flag alone (went_public=False now).
    for _ in range(_PUBLIC_GRAB_MAX_ATTEMPTS + 2):
        await service._handle_public_backlog(11, "u", None, went_public=False)
    async with get_session() as session:
        flag = await crud.get_setting(session, service._public_grab_key(11))
    expect("flag cleared after max attempts (no infinite retry)", flag is None, repr(flag))
    expect("grab attempted exactly max times",
           service.grab_public_backlog.await_count == _PUBLIC_GRAB_MAX_ATTEMPTS,
           str(service.grab_public_backlog.await_count))
    expect("a pending retry is NOT flagged announced (no card fired that sweep)",
           service.grab_public_backlog.await_args_list[-1].kwargs.get(
               "transition_announced") is False)
    expect("last attempt is flagged final (wording: giving up)",
           service.grab_public_backlog.await_args_list[-1].kwargs.get("final_attempt") is True)


async def test_ledger_noop_when_nothing_pending() -> None:
    service = _make_service()
    service.grab_public_backlog = AsyncMock(return_value={"total": 9, "fetched": 9, "skipped": 0})
    handled = await service._handle_public_backlog(12, "u", None, went_public=False)
    expect("no transition + no flag → not handled", handled is False)
    expect("grab not run when nothing pending", service.grab_public_backlog.await_count == 0)


async def test_ledger_disabled_by_flag() -> None:
    from app.config import settings
    service = _make_service()
    service.grab_public_backlog = AsyncMock(return_value={"total": 9, "fetched": 9, "skipped": 0})
    original = settings.auto_grab_on_public
    try:
        settings.auto_grab_on_public = False
        handled = await service._handle_public_backlog(13, "u", None, went_public=True)
        expect("feature-off → not handled (normal phase runs)", handled is False)
        expect("feature-off → no grab", service.grab_public_backlog.await_count == 0)
    finally:
        settings.auto_grab_on_public = original


async def test_concurrent_grab_guard() -> None:
    """Two checks of the same account overlapping (a manual Recheck landing
    mid-sweep) must run ONE grab — two would each load the seen-set before the
    other marked anything and send everything twice."""
    service = _make_service()
    gate = asyncio.Event()
    calls = 0

    async def slow_grab(*args, **kwargs):
        nonlocal calls
        calls += 1
        await gate.wait()
        return {"total": 3, "fetched": 3, "skipped": 0}

    service.grab_public_backlog = slow_grab  # type: ignore[method-assign]
    first = asyncio.create_task(service._handle_public_backlog(20, "u", None, went_public=True))
    # Let it reach the (blocked) grab. Polled rather than a fixed sleep:
    # on a loaded machine (the runner executes suites back to back) 0.1s
    # is not always enough for the task to get past its first DB read.
    for _ in range(300):
        if 20 in service._public_grabs_in_flight:
            break
        await asyncio.sleep(0.01)
    expect("first grab is in flight", 20 in service._public_grabs_in_flight)
    second = await service._handle_public_backlog(20, "u", None, went_public=True)
    expect("overlapping call claims the account without a second grab", second is True)
    gate.set()
    await first
    expect("grab ran exactly once", calls == 1, str(calls))
    expect("in-flight marker released", 20 not in service._public_grabs_in_flight)

    # The marker is released even when the grab blows up.
    service.grab_public_backlog = AsyncMock(side_effect=RuntimeError("boom"))
    try:
        await service._handle_public_backlog(21, "u", None, went_public=True)
    except RuntimeError:
        pass
    expect("in-flight marker released after a crash", 21 not in service._public_grabs_in_flight)


# ---------- 4. check_username wiring (integration) ----------

class ScriptedInstagram:
    """is_private follows a scripted sequence, one value per profile fetch
    (the last value repeats)."""

    def __init__(self, sequence: list[bool]) -> None:
        self.sequence = list(sequence)
        self.calls = 0

    async def fetch_profile(self, username: str, **kw) -> ProfileFetchResult:
        is_private = self.sequence[min(self.calls, len(self.sequence) - 1)]
        self.calls += 1
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

    async def probe_by_id(self, user_id, **kw):
        # The client's numeric-id probe; this fake has no reel data, so the
        # id route reads as blocked and the check proceeds by username.
        return IdProbe(user_id=str(user_id), status=401)


async def test_check_username_triggers_grab_on_flip() -> None:
    async with get_session() as session:
        session.add(MonitoredAccount(username="flipper", active=True))

    service = _make_service()
    service.instagram = ScriptedInstagram([True, False])
    # Spy on the backlog handler and the normal story phase.
    calls: dict[str, list] = {"backlog": [], "story": []}

    async def spy_backlog(account_id, username, instagram_id, *, went_public):
        calls["backlog"].append(went_public)
        return True  # claim the account so the normal phase is skipped

    async def spy_story(account_id, username, *, instagram_id=None, reel_data=None, **kw):
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


async def test_flip_flop_end_to_end() -> None:
    """The reported bug, end to end: private→public→private→public→…

    The real check path, the real ledger and the real grab run against a fake
    Instagram that flips privacy on every check and a fake media source. Media
    must reach the chat exactly once per item, however many times it flips."""
    async with get_session() as session:
        session.add(MonitoredAccount(username="flapper", active=True))

    fake = FakeStories(posts=[_item("fp1"), _item("fp2")], stories=[_item("fs1", source="story")])
    service = _make_service(fake)
    service.instagram = ScriptedInstagram([True, False, True, False, True, False, False])
    story_phase: list[str] = []

    async def spy_story(account_id, username, **kw):
        story_phase.append(username)

    service._check_stories_and_highlights = spy_story  # type: ignore[assignment]

    await service.check_username("flapper")  # 1: private
    expect("e2e: private — nothing sent", _media_sent(service) == 0)

    await service.check_username("flapper")  # 2: public (flip #1) → whole account
    expect("e2e: first flip delivers the whole account (3 items)",
           _media_sent(service) == 3, str(_media_sent(service)))

    await service.check_username("flapper")  # 3: private again
    _reset_notifier(service)
    await service.check_username("flapper")  # 4: public (flip #2) → nothing new
    expect("e2e: second flip re-sends NOTHING", _media_sent(service) == 0, str(_media_sent(service)))
    texts = _texts(service)
    # The whole point of the collapse: one flip, one message.
    expect("e2e: second flip costs exactly ONE message", len(texts) == 1, str(texts))
    expect("e2e: that message is the profile card announcing the flip",
           texts and "PRIVATE" in texts[0] and "PUBLIC" in texts[0], str(texts))
    expect("e2e: the grab adds no second line",
           not any("nothing new" in t for t in texts), str(texts))
    expect("e2e: second flip never claims to grab the whole account",
           not any("whole account" in t for t in texts), str(texts))

    fake.posts.append(_item("fp3"))              # posted while private
    await service.check_username("flapper")  # 5: private
    _reset_notifier(service)
    await service.check_username("flapper")  # 6: public (flip #3) → only fp3
    expect("e2e: third flip delivers only the new post", _media_sent(service) == 1,
           str(_media_sent(service)))

    _reset_notifier(service)
    await service.check_username("flapper")  # 7: still public → normal phase, no grab
    expect("e2e: staying public runs the normal story phase", story_phase == ["flapper"],
           str(story_phase))
    expect("e2e: staying public sends no backlog", _media_sent(service) == 0)

    expect("e2e: every item downloaded exactly once across all flips",
           sorted(fake.downloaded) == ["fp1", "fp2", "fp3", "fs1"], str(fake.downloaded))
    async with get_session() as session:
        account = await crud.get_account(session, "flapper")
        flag = await crud.get_setting(session, service._public_grab_key(account.id))
    expect("e2e: no pending grab left behind", flag is None, repr(flag))


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    test_went_public_helper()
    await test_grab_flap_never_resends()
    await test_grab_respects_baseline()
    await test_grab_empty_reports_retry()
    await test_grab_quiet_when_card_announced()
    await test_grab_kill_during_listing()
    await test_ledger_clears_on_success()
    await test_ledger_clears_when_nothing_new()
    await test_ledger_retries_then_gives_up()
    await test_ledger_noop_when_nothing_pending()
    await test_ledger_disabled_by_flag()
    await test_concurrent_grab_guard()
    await test_check_username_triggers_grab_on_flip()
    await test_flip_flop_end_to_end()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All went-public tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
