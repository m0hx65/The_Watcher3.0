"""What a sweep is allowed to spend (2026-09-07).

Every Instagram request a sweep makes is either useful or it is the reason
the next one gets refused. These are the places the sweep was spending
requests and seconds it did not need to:

- a page the phone had ALREADY delivered was used only after this host's own
  page request had been refused first — up to _DIRECT_PAGE_TIMEOUT seconds
  per account, and one more refused request from an IP already out of favour;
- a reel query went out for EVERY account every sweep, on the same home line
  the page door depends on, while the page answered the story question and
  nothing ever read the reel's answer;
- a PRIVATE account bought one of those every sweep forever: the story phase
  skips it, so its highlight-scan stamp never advanced and it read as
  permanently due — for a reel with no story, no live flag and no visible
  highlights in it;
- what the phone did deliver was then thrown away, so the live flag and the
  highlight catalog went missing for the whole page-served era;
- each check re-read the account's numeric id from the database, and a failed
  check re-read its privacy — both of which the sweep had already read to
  build its own list.

And one guard: with more than one lane, the pacing gap must space the
launches. Reading the next slot without claiming it let every waiting lane
wake to the same instant — a burst, which is the shape that trips the gate.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
DB_FILE = ROOT / "test_sweep_efficiency.db"
if DB_FILE.exists():
    DB_FILE.unlink()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.config import settings  # noqa: E402
from app.database import crud  # noqa: E402
from app.database.models import (  # noqa: E402
    AccountSnapshot, Base, MonitoredAccount,
)
from app.database.session import engine, get_session  # noqa: E402
from app.monitor import home_fetch  # noqa: E402
from app.monitor.instagram import (  # noqa: E402
    IdProbe, InstagramClient, ProfileFetchResult,
)
from app.monitor.service import (  # noqa: E402
    MonitorService, _SWEEP_STAGGER_SECONDS, _SweepThrottle,
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


# The page as Instagram serves it, payload and all — the same shape the home
# fetcher extracts and posts back.
PAGE = (
    "<!DOCTYPE html><html><head></head><body>"
    "<script type=\"application/json\" data-sjs>"
    '{"require":[["RelayPrefetchedStreamCache","next",[],[{"__bbox":'
    '{"result":{"data":{"xig_user_by_username":'
    '{"pk":"42","username":"pageuser",'
    '"profile_pic_url":"https:\\/\\/scontent.cdninstagram.com\\/v\\/t51.2885-19\\/1_2_3_n.jpg",'
    '"is_private":false,"biography":"bio text","full_name":"Page User",'
    '"is_verified":false,"bio_links":[],"follower_count":1234,'
    '"following_count":567,"latest_reel_media":0,"all_media_count":null,'
    '"id":"17841407816045006"}'
    "}}}}]]]}</script></body></html>"
)


class _MockResponse:
    def __init__(self, status_code: int, body: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text

    def json(self) -> Any:
        return self._body


class _MockSession:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.requests: list[dict] = []

    async def get(self, url: str, *, params: Any = None, headers: Any = None):
        self.requests.append({"url": url, "params": dict(params or {})})
        return self.handler(url, dict(params or {}))

    async def close(self) -> None:
        pass

    def page_asks(self) -> list[str]:
        """The requests that went to instagram.com/<username>/ — this host's
        own page door, the one a datacenter IP is refused on."""
        return [
            r["url"] for r in self.requests
            if r["url"].startswith("https://www.instagram.com/")
            and "/graphql/" not in r["url"] and "/api/" not in r["url"]
        ]


class FakeBroker:
    """The home fetcher's broker: pages and reels the phone has delivered,
    plus a record of everything the sweep asked it for."""

    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.cache: dict[str, home_fetch.PageResult] = {}
        self.reel_cache: dict[str, home_fetch.PageResult] = {}
        self.asked: list[str] = []
        self.prefetched: list[str] = []
        self.prefetched_reels: list[tuple[str, str]] = []
        self.last_seen_seconds = 5.0
        self.battery = None
        self.charging = None
        self.worker = "xiaomi"
        self.delivered = 0

    def describe(self) -> str:
        return "connected (fake)" if self.connected else "not connected (fake)"

    def note_sweep(self, jobs: int) -> None:
        self.last_sweep_jobs = jobs

    def cached(self, username: str):
        return self.cache.get(username)

    def cached_reel(self, user_id: str):
        return self.reel_cache.get(str(user_id))

    def prefetch(self, usernames) -> int:
        if not self.connected:
            return 0
        names = list(usernames)
        self.prefetched.extend(names)
        return len(names)

    def prefetch_reels(self, users) -> int:
        if not self.connected:
            return 0
        pairs = list(users)
        self.prefetched_reels.extend(pairs)
        return len(pairs)

    async def request_page(self, username: str, *, timeout: float = 30.0,
                           fresh: bool = False):
        self.asked.append(username)
        return self.cache.get(username) if not fresh else None

    async def request_reel(self, user_id: str, username: str = "", *,
                           timeout: float = 15.0, fresh: bool = False):
        return None


class ScriptedInstagram:
    """A client whose two doors answer whatever a test scripts."""

    def __init__(self) -> None:
        self.profile = lambda u: ProfileFetchResult(
            username=u, http_status=401, error="HTTP 401", api_status=401,
        )
        self.probe = lambda i: IdProbe(user_id=i, status=401)
        self.probe_calls: list[str] = []
        # This host's page door, as the client reports it: refusing by
        # default, which is the regime these sweeps model.
        self.direct_page_door_failing = True
        self.in_hand: Optional[dict] = None
        self.in_hand_calls: list[str] = []

    async def fetch_profile(self, username: str, **kw) -> ProfileFetchResult:
        return self.profile(username)

    async def probe_by_id(self, user_id: str, **kw) -> IdProbe:
        self.probe_calls.append(str(user_id))
        return self.probe(str(user_id))

    async def fetch_reel_user(self, user_id: str):
        raise AssertionError(
            "the reel route must not be asked again once the page answered"
        )

    def reel_in_hand(self, user_id: str):
        self.in_hand_calls.append(str(user_id))
        return self.in_hand

    async def fetch_hd_pic_url(self, user_id: str):
        return None


class QuietStories:
    async def fetch_stories(self, username):
        return []

    async def fetch_highlight_items(self, username, highlight_id, title):
        return []

    async def fetch_profile_pic_url(self, username):
        return None


def _service(instagram, *, stories=None) -> MonitorService:
    notifier = AsyncMock()
    notifier.send_text = AsyncMock(return_value=True)
    notifier.send_document = AsyncMock(return_value=True)
    notifier.create_forum_topic = AsyncMock(return_value=None)
    return MonitorService(
        instagram=instagram,
        hasher=AsyncMock(hash_url=AsyncMock(return_value=None)),
        notifier=notifier, stories=stories,
    )


def _sent(service) -> list[str]:
    return [c.args[0] for c in service.notifier.send_text.await_args_list]


async def _new_account(username: str, instagram_id: Optional[str] = "42") -> int:
    async with get_session() as session:
        account = MonitoredAccount(
            username=username, active=True, instagram_id=instagram_id
        )
        session.add(account)
        await session.flush()
        return account.id


async def _seed_snapshot(account_id: int, username: str,
                         is_private: Optional[bool], *,
                         http_status: int = 200) -> None:
    """One stored reading, as a check would have written it."""
    async with get_session() as session:
        session.add(AccountSnapshot(
            account_id=account_id, username=username, http_status=http_status,
            followers_count=10, following_count=5, is_private=is_private,
        ))


async def _pause_everything() -> None:
    async with get_session() as session:
        for a in await crud.list_accounts(session, only_active=True):
            await crud.set_account_active(session, a.username, False)


async def _set_door(closed: bool) -> None:
    from datetime import datetime, timezone
    async with get_session() as session:
        if closed:
            await crud.set_setting(
                session, "username_api_closed_at",
                datetime.now(timezone.utc).isoformat(),
            )
        else:
            await crud.delete_setting(session, "username_api_closed_at")


# ---------- 1. a page already in hand is not paid for twice ----------------

async def test_a_prefetched_page_skips_this_hosts_refused_door() -> None:
    """The phone delivered this page seconds ago. Asking Instagram for it
    again from here costs up to 12 s and earns a 429 — and that refusal is
    what keeps the door shut. Take the one already paid for."""
    old_broker, old_token, old_proxy = (
        home_fetch.broker, settings.home_fetch_token, settings.ig_proxy_url,
    )
    settings.home_fetch_token = "sekrit"
    settings.ig_proxy_url = "https://ig-proxy.example.workers.dev"
    try:
        broker = FakeBroker(connected=True)
        broker.cache["pageuser"] = home_fetch.PageResult(
            200, PAGE, "https://www.instagram.com/pageuser/"
        )
        home_fetch.broker = broker

        # This host's page door would answer — slowly, and with a 429.
        session = _MockSession(lambda url, p: _MockResponse(429, {}, text=""))
        async with InstagramClient(max_retries=5, session=session) as client:
            swept = await client.fetch_profile(
                "pageuser", auth_attempts=1, api=False, cached_page_ok=True
            )
        expect("the sweep's check reads the page the phone delivered",
               swept.success and swept.source == "public_page"
               and (swept.parsed or {}).get("following_count") == 567, repr(swept))
        expect("without spending a request on this host's refused door",
               session.page_asks() == [], repr(session.page_asks()))
        expect("and without waiting on the phone",
               broker.asked == [], repr(broker.asked))

        # A manual check wants a fresh reading, so this host asks first — which
        # is also what keeps the door under test rather than written off.
        session2 = _MockSession(lambda url, p: _MockResponse(429, {}, text=""))
        async with InstagramClient(max_retries=5, session=session2) as client:
            fresh = await client.fetch_profile(
                "pageuser", auth_attempts=1, api=False, cached_page_ok=False
            )
        expect("a manual check still asks this host first",
               len(session2.page_asks()) == 1, repr(session2.page_asks()))
        expect("and reports the refusal rather than a stale page",
               not fresh.success, repr(fresh))

        # A delivered page that carried nothing usable is not an answer: the
        # doors below still get their turn.
        broker.cache["walled"] = home_fetch.PageResult(429, "", "")
        session3 = _MockSession(lambda url, p: _MockResponse(429, {}, text=""))
        async with InstagramClient(max_retries=5, session=session3) as client:
            walled = await client.fetch_profile(
                "walled", auth_attempts=1, api=False, cached_page_ok=True
            )
        expect("a refused page in hand falls through to the other doors",
               len(session3.page_asks()) == 1, repr(session3.page_asks()))
        expect("and the check reports the failure honestly", not walled.success,
               repr(walled))
    finally:
        home_fetch.broker = old_broker
        settings.home_fetch_token = old_token
        settings.ig_proxy_url = old_proxy


# ---------- 2. reels only where a reel is still needed ---------------------

async def _sweep(usernames: list[str], *, connected: bool = True
                 ) -> tuple[dict, FakeBroker, ScriptedInstagram, MonitorService]:
    await _pause_everything()
    await _set_door(True)
    for i, u in enumerate(usernames):
        await _new_account(u, instagram_id=str(1000 + i))
    broker = FakeBroker(connected=connected)
    home_fetch.broker = broker
    ig = ScriptedInstagram()
    ig.profile = lambda u: ProfileFetchResult(
        username=u, http_status=200, source="public_page", api_status=401,
        parsed={"username": u, "followers_count": 10, "following_count": 5,
                "is_private": False, "instagram_id": "42",
                "has_public_story": False},
    )
    service = _service(ig)
    result = await service.check_all()
    return result, broker, ig, service


async def test_the_sweep_asks_only_for_the_reels_it_will_read() -> None:
    """With the username API shut and the phone serving pages, the page
    answers the story question — so a reel is worth asking for only where the
    highlight catalog is actually due. Asking for one per account was a second
    Instagram request per account, on the home line, that nothing read."""
    old_broker, old_token = home_fetch.broker, settings.home_fetch_token
    settings.home_fetch_token = "sekrit"
    try:
        names = [f"reel{i}" for i in range(4)]
        await _pause_everything()
        ids = {}
        for i, u in enumerate(names):
            ids[u] = await _new_account(u, instagram_id=str(1000 + i))
        # Three were scanned just now; one is overdue.
        async with get_session() as session:
            for u in names[:3]:
                await crud.set_setting(
                    session, f"highlight_scan:{ids[u]}", str(time.time())
                )
            await crud.set_setting(
                session, f"highlight_scan:{ids[names[3]]}",
                str(time.time() - settings.highlight_scan_interval - 60),
            )
        await _set_door(True)
        broker = FakeBroker(connected=True)
        home_fetch.broker = broker
        ig = ScriptedInstagram()
        ig.profile = lambda u: ProfileFetchResult(
            username=u, http_status=200, source="public_page", api_status=401,
            parsed={"username": u, "followers_count": 10, "following_count": 5,
                    "is_private": False, "instagram_id": "42",
                    "has_public_story": False},
        )
        service = _service(ig)
        await service.check_all()
        asked = sorted(name for _, name in broker.prefetched_reels)
        expect("only the account whose catalog is due gets a reel query",
               asked == [names[3]], repr(broker.prefetched_reels))
        expect("every account still gets its page",
               sorted(broker.prefetched) == sorted(names), repr(broker.prefetched))

        # The filter only applies while the page is answering the story
        # question. With the username API believed open, the reel is the
        # primary route again and every account still gets one — including
        # the three whose catalog was scanned a moment ago.
        await _set_door(False)
        broker2 = FakeBroker(connected=True)
        home_fetch.broker = broker2
        service2 = _service(ig)
        await service2.check_all()
        expect("with the API door believed open, every account gets a reel",
               sorted(n for _, n in broker2.prefetched_reels) == sorted(names),
               repr(broker2.prefetched_reels))
    finally:
        home_fetch.broker, settings.home_fetch_token = old_broker, old_token
        await _set_door(False)


# ---------- 3. what the phone delivered is read, not discarded -------------

async def test_the_story_phase_reads_the_reel_the_phone_delivered() -> None:
    """The page says whether a story is up; it has never known a live
    broadcast or the highlight catalog. The phone's reel answer does, it has
    already been fetched, and reading it costs nothing."""
    account_id = await _new_account("livedup", instagram_id="42")
    ig = ScriptedInstagram()
    ig.in_hand = {
        "has_public_story": False,
        "is_live": True,
        "highlights": {"h1": "Trips", "h2": "Food"},
    }
    service = _service(ig, stories=QuietStories())
    page_reel = {
        "has_public_story": False, "is_live": False,
        "highlights": None, "from_page": True,
    }
    await service._check_stories_and_highlights(
        account_id, "livedup", instagram_id="42",
        reel_data=dict(page_reel), always_report=True,
    )
    texts = _sent(service)
    expect("the live flag comes back",
           any("LIVE" in t for t in texts), repr(texts))
    expect("and it never asked the refused reel route again",
           ig.in_hand_calls == ["42"], repr(ig.in_hand_calls))
    async with get_session() as session:
        stored = await crud.get_highlight_catalog(session, account_id)
    expect("the highlight catalog is stored from the same free answer",
           stored == {"h1": "Trips", "h2": "Food"}, repr(stored))

    # Nothing in hand: the page's own answer stands, and no stored catalog is
    # overwritten with an empty one.
    quiet = await _new_account("nothingyet", instagram_id="77")
    ig2 = ScriptedInstagram()
    ig2.in_hand = None
    service2 = _service(ig2, stories=QuietStories())
    await service2._check_stories_and_highlights(
        quiet, "nothingyet", instagram_id="77",
        reel_data=dict(page_reel), always_report=True,
    )
    texts2 = _sent(service2)
    expect("with nothing in hand the page's answer still stands",
           any("NO STORY" in t for t in texts2), repr(texts2))


async def test_the_shut_door_verdict_survives_a_restart() -> None:
    """Two sweeps in a row opened with "believed open" and paid five blocked
    Worker calls — 45 s and thirty refused upstream attempts — to rediscover
    a door the sweep before had already found shut. The verdict has to reach
    the database and be read back by a FRESH service, and when it is not
    trusted the log has to say which of the two reasons it is."""
    await _pause_everything()
    names = [f"verdict{i}" for i in range(3)]
    for i, u in enumerate(names):
        await _new_account(u, instagram_id=str(7000 + i))
    await _set_door(False)

    ig = ScriptedInstagram()          # every username lookup refused
    ig.profile = lambda u: ProfileFetchResult(
        username=u, http_status=200, source="public_page", api_status=401,
        parsed={"username": u, "followers_count": 10, "following_count": 5,
                "is_private": False, "instagram_id": "42",
                "has_public_story": False},
    )
    first = _service(ig)
    expect("a fresh service starts out believing the door is open",
           not await first.username_api_known_closed())
    await first.check_all()
    async with get_session() as session:
        stored = await crud.get_setting(session, "username_api_closed_at")
    expect("the sweep wrote the verdict to the database",
           stored is not None, repr(stored))

    # A new MonitorService is what a redeploy produces: nothing in memory.
    restarted = _service(ig)
    expect("and a restarted service reads it back",
           await restarted.username_api_known_closed(), repr(stored))
    expect("so the next sweep knocks once, not USERNAME_API_KNOCKS times",
           settings.username_api_knocks > 1)

    # The two ways it can legitimately not be trusted, each named in the log
    # line rather than both showing as a bare "believed open".
    fresh = _service(ig)
    await fresh.username_api_known_closed()
    reason = fresh._door_open_reason()
    expect("a trusted verdict is not reported as a reason to knock",
           "nothing recorded" not in reason, reason)

    old_window = settings.username_api_recheck_seconds
    try:
        settings.username_api_recheck_seconds = 0
        zeroed = _service(ig)
        expect("with the window at 0 the verdict is never trusted",
               not await zeroed.username_api_known_closed())
        expect("and the log says so, by name",
               "USERNAME_API_RECHECK_SECONDS is 0" in zeroed._door_open_reason(),
               zeroed._door_open_reason())

        # The real misconfiguration, measured 2026-09-07: a 90-second window
        # against a 30-minute sweep interval. The verdict was written every
        # sweep and expired long before the next one could read it, so every
        # sweep re-paid five blocked Worker calls to rediscover the same shut
        # door. Nothing looked broken — which is why the log has to say it.
        settings.username_api_recheck_seconds = 90
        stale = datetime.now(timezone.utc) - timedelta(minutes=30)
        async with get_session() as session:
            await crud.set_setting(
                session, "username_api_closed_at", stale.isoformat()
            )
        short = _service(ig)
        expect("a verdict one sweep old is already stale in a 90s window",
               not await short.username_api_known_closed())
        why = short._door_open_reason()
        expect("and the log names it as the cause, not as a rounding error",
               "SHORTER than the sweep interval" in why and "90s" in why, why)
    finally:
        settings.username_api_recheck_seconds = old_window

    async with get_session() as session:
        await crud.delete_setting(session, "username_api_closed_at")
    blank = _service(ig)
    await blank.username_api_known_closed()
    expect("with nothing stored the log says that instead",
           "nothing recorded" in blank._door_open_reason(),
           blank._door_open_reason())


async def test_the_phone_stands_by_while_this_host_can_fetch_pages() -> None:
    """Instagram started serving Render's own page requests again (measured
    2026-09-07): 17 accounts, 17 pages, half a second each. The phone was
    still handed all 17 as well — 17 fetches nobody read, spent against the
    home line's own standing — and the sweep still ran at the 0.2 s pace that
    only makes sense when the bot is making no requests of its own. Both are
    wrong when this host is the one asking Instagram."""
    old_broker, old_token = home_fetch.broker, settings.home_fetch_token
    settings.home_fetch_token = "sekrit"
    try:
        await _pause_everything()
        names = [f"standby{i}" for i in range(2)]
        for i, u in enumerate(names):
            await _new_account(u, instagram_id=str(6000 + i))
        await _set_door(True)
        broker = FakeBroker(connected=True)
        home_fetch.broker = broker
        ig = ScriptedInstagram()
        ig.direct_page_door_failing = False   # this host's page door answers
        ig.profile = lambda u: ProfileFetchResult(
            username=u, http_status=200, source="public_page", api_status=401,
            parsed={"username": u, "followers_count": 10, "following_count": 5,
                    "is_private": False, "instagram_id": "42",
                    "has_public_story": False},
        )
        service = _service(ig)
        started = time.monotonic()
        result = await service.check_all()
        own_pace = time.monotonic() - started
        expect("every account is still checked", result["checked"] == 2, repr(result))
        expect("the phone is handed no pages at all",
               broker.prefetched == [], repr(broker.prefetched))
        expect("and the sweep paces its own requests, not the phone's",
               own_pace >= _SWEEP_STAGGER_SECONDS - 0.3, f"{own_pace:.2f}s")

        # The same sweep with this host's door refusing: the phone takes over
        # and the pace goes back up, because now the bot is asking nobody.
        await _pause_everything()
        handed = [f"handover{i}" for i in range(2)]
        for i, u in enumerate(handed):
            await _new_account(u, instagram_id=str(6100 + i))
        broker2 = FakeBroker(connected=True)
        home_fetch.broker = broker2
        ig.direct_page_door_failing = True
        service2 = _service(ig)
        started = time.monotonic()
        await service2.check_all()
        phone_pace = time.monotonic() - started
        expect("with this host refused, the phone gets the whole list",
               sorted(broker2.prefetched) == sorted(handed), repr(broker2.prefetched))
        expect("and the sweep runs at the phone's pace instead",
               phone_pace < own_pace, f"{phone_pace:.2f}s vs {own_pace:.2f}s")
    finally:
        home_fetch.broker, settings.home_fetch_token = old_broker, old_token
        await _set_door(False)


async def test_one_odd_page_does_not_hand_the_phone_the_sweep() -> None:
    """Measured 2026-09-07: one account's page came back a login wall while
    the other sixteen answered in half a second each. On a first-refusal rule
    that one page handed the phone the fifteen accounts still to check — it
    fetched all fifteen and delivered them 80 seconds AFTER the sweep had
    already read every one of them from here. One odd page is not a shut
    door, so the handover takes two refusals in a row."""
    client = InstagramClient(max_retries=1, session=_MockSession(
        lambda url, p: _MockResponse(401, {})
    ))
    expect("a healthy door asks nothing of the phone",
           not client.direct_page_door_failing)
    client._note_direct_page({"status": 200, "parsed": None,
                              "error": "no profile payload in the page"})
    expect("one login-walled page is still not a shut door",
           not client.direct_page_door_failing)
    client._note_direct_page({"status": 200, "parsed": None,
                              "error": "no profile payload in the page"})
    expect("two in a row is", client.direct_page_door_failing)
    client._note_direct_page({"status": 200, "parsed": {"username": "u"}})
    expect("and an answer clears it again", not client.direct_page_door_failing)

    # A 404 is an answer about the username, not about this host's door.
    client._note_direct_page({"status": 404, "parsed": None})
    client._note_direct_page({"status": 404, "parsed": None})
    expect("a 404 never counts against the door",
           not client.direct_page_door_failing)
    await client.close()


async def test_a_refused_reel_stops_costing_the_phone_its_page_door() -> None:
    """Instagram refuses the reel query from the home line (429). The worker
    reads that as "wait a few minutes" and stops fetching ANYTHING for a
    minute — so a reel nobody can have costs the phone the page door it
    exists for, which is how a live page request timed out at 30 s."""
    broker = home_fetch.HomeFetchBroker()
    await broker.next_job(wait=0.01, worker="xiaomi", kinds=["page", "reel"])
    expect("reels are asked for while the route answers",
           broker.prefetch_reels([("42", "a")]) == 1)
    job = (await broker.next_job(wait=0.2, worker="xiaomi",
                                 kinds=["page", "reel"]))[0]
    broker.deliver(job.id, home_fetch.PageResult(429, ""))
    expect("one refusal is tolerated", not broker.reel_route_paused)

    expect("a second reel still goes out",
           broker.prefetch_reels([("43", "b")]) == 1)
    job = (await broker.next_job(wait=0.2, worker="xiaomi",
                                 kinds=["page", "reel"]))[0]
    broker.deliver(job.id, home_fetch.PageResult(429, ""))
    expect("two refusals in a row pause the reel route",
           broker.reel_route_paused)
    expect("so no more reel jobs are queued",
           broker.prefetch_reels([("44", "c")]) == 0)
    expect("nor asked for live",
           await broker.request_reel("44", "c", timeout=0.1) is None)
    expect("but PAGES are never held back — that is the whole point",
           broker.prefetch(["c"]) == 1)

    # An answer clears it, so the route recovers on its own.
    broker._reel_paused_until = 0.0
    broker.prefetch_reels([("45", "d")])
    job = next(j for j in await broker.next_job(
        wait=0.2, worker="xiaomi", kinds=["page", "reel"], max_jobs=8,
    ) if j.kind == "reel")
    broker.deliver(job.id, home_fetch.PageResult(200, '{"data":{}}'))
    expect("a 200 clears the record", broker._reel_refusals == 0)


async def test_a_private_account_never_buys_a_reel_query() -> None:
    """A private account has no story, no live broadcast and no visible
    highlights, so the story phase skips it — and its scan stamp never
    advances, which made it read as permanently 'due' and buy a reel query
    every sweep, forever, for an answer nothing could ever read."""
    old_broker, old_token = home_fetch.broker, settings.home_fetch_token
    settings.home_fetch_token = "sekrit"
    try:
        await _pause_everything()
        # private (no stamp), public and due, public and freshly scanned,
        # and one nobody has ever read.
        privacy = {"shy": True, "loud": False, "scanned": False,
                   "brandnew": None}
        ids = {}
        for i, (name, private) in enumerate(privacy.items()):
            ids[name] = await _new_account(name, instagram_id=str(4000 + i))
            if private is not None:
                await _seed_snapshot(ids[name], name, private)
        async with get_session() as session:
            await crud.set_setting(
                session, f"highlight_scan:{ids['scanned']}", str(time.time())
            )
            await crud.set_setting(
                session, f"highlight_scan:{ids['loud']}",
                str(time.time() - settings.highlight_scan_interval - 60),
            )
        await _set_door(True)
        broker = FakeBroker(connected=True)
        home_fetch.broker = broker
        ig = ScriptedInstagram()
        ig.profile = lambda u: ProfileFetchResult(
            username=u, http_status=200, source="public_page", api_status=401,
            parsed={"username": u, "followers_count": 10, "following_count": 5,
                    "is_private": bool(privacy.get(u)), "instagram_id": "42"},
        )
        service = _service(ig)
        await service.check_all()
        asked = sorted(name for _, name in broker.prefetched_reels)
        expect("the private account is left out of the reel queries",
               "shy" not in asked, repr(asked))
        expect("the public account whose catalog is due still gets one",
               "loud" in asked, repr(asked))
        expect("a freshly scanned public account does not",
               "scanned" not in asked, repr(asked))
        expect("and an account nobody has read yet is never silently skipped",
               "brandnew" in asked, repr(asked))
        expect("every account still gets its page",
               sorted(broker.prefetched) == sorted(privacy), repr(broker.prefetched))
    finally:
        home_fetch.broker, settings.home_fetch_token = old_broker, old_token
        await _set_door(False)


async def test_privacy_is_read_from_the_newest_successful_reading() -> None:
    """One query, and the same answer the per-account lookup gives: the
    newest SUCCESSFUL snapshot, with a missing flag reported as unknown
    rather than guessed either way."""
    await _pause_everything()
    flipped = await _new_account("flipped", instagram_id="5000")
    await _seed_snapshot(flipped, "flipped", True)
    await _seed_snapshot(flipped, "flipped", False)   # went public since

    blank = await _new_account("blank", instagram_id="5001")
    await _seed_snapshot(blank, "blank", None)        # a reading without the flag

    blocked = await _new_account("blocked", instagram_id="5002")
    await _seed_snapshot(blocked, "blocked", True)
    await _seed_snapshot(blocked, "blocked", False, http_status=401)  # not a reading

    never = await _new_account("never", instagram_id="5003")

    async with get_session() as session:
        got = await crud.latest_privacy_by_account(
            session, [flipped, blank, blocked, never]
        )
    expect("the newest reading wins", got.get(flipped) is False, repr(got))
    expect("a reading without the flag is unknown, not a guess",
           got.get(blank) is None, repr(got))
    expect("a blocked check is not a reading", got.get(blocked) is True, repr(got))
    expect("an account never read is absent, which is also unknown",
           never not in got, repr(got))

    async with get_session() as session:
        empty = await crud.latest_privacy_by_account(session, [])
    expect("no accounts, no query", empty == {}, repr(empty))


async def test_a_previous_sweeps_reel_is_not_read_as_this_ones() -> None:
    """The broker keeps an answer for 15 minutes so one sweep can share it.
    A status the bot announces as current must not come from the sweep
    before — too old is the same as nothing in hand."""
    old_broker, old_token = home_fetch.broker, settings.home_fetch_token
    settings.home_fetch_token = "sekrit"
    try:
        broker = FakeBroker(connected=True)
        home_fetch.broker = broker
        client = InstagramClient(max_retries=1, session=_MockSession(
            lambda url, p: _MockResponse(401, {})
        ))
        live = '{"data":{"user":{"has_public_story":false,"is_live":true,' \
               '"reel":{"id":"55","user":{"id":"55","username":"n"}},' \
               '"edge_highlight_reels":{"edges":[]}}}}'
        fresh = home_fetch.PageResult(200, live)
        broker.reel_cache["55"] = fresh
        expect("a reel from this sweep is read",
               (client.reel_in_hand("55") or {}).get("is_live") is True,
               repr(client.reel_in_hand("55")))

        stale_client = InstagramClient(max_retries=1, session=_MockSession(
            lambda url, p: _MockResponse(401, {})
        ))
        stale = home_fetch.PageResult(200, live)
        stale.fetched_at -= InstagramClient._REEL_IN_HAND_MAX_AGE + 60
        broker.reel_cache["56"] = stale
        expect("a reel from the sweep before is not",
               stale_client.reel_in_hand("56") is None,
               repr(stale_client.reel_in_hand("56")))
        await client.close()
        await stale_client.close()
    finally:
        home_fetch.broker, settings.home_fetch_token = old_broker, old_token


# ---------- 4. the id the sweep already read is not read again -------------

async def test_a_sweep_does_not_re_read_every_accounts_id() -> None:
    """check_all reads every account row to build its list. Reading each id
    again inside the check cost a session checkout, a pool ping and a round
    trip per account for something already in hand."""
    old_broker, old_token = home_fetch.broker, settings.home_fetch_token
    settings.home_fetch_token = "sekrit"
    try:
        names = [f"noreread{i}" for i in range(3)]
        result, broker, ig, service = await _sweep(names)

        async def must_not_be_called(account_id):
            raise AssertionError("the sweep re-read an id it already had")

        service._stored_instagram_id = must_not_be_called  # type: ignore[assignment]
        await _pause_everything()
        again = [f"again{i}" for i in range(3)]
        for i, u in enumerate(again):
            await _new_account(u, instagram_id=str(3000 + i))
        home_fetch.broker = FakeBroker(connected=True)
        second = await service.check_all()
        expect("the sweep ran without a single id re-read",
               second["checked"] == 3, repr(second))
        expect("and every check asked by the id the sweep already had",
               sorted(ig.probe_calls[-3:]) == ["3000", "3001", "3002"],
               repr(ig.probe_calls))
    finally:
        home_fetch.broker, settings.home_fetch_token = old_broker, old_token
        await _set_door(False)


# ---------- 5. extra lanes space out, they do not burst --------------------

async def test_extra_lanes_are_spaced_not_bursted() -> None:
    """Reading the next slot without claiming it let every waiting lane wake
    to the same instant. Three lanes then left as one burst — which is the
    exact shape that trips Instagram's anonymous limiter."""
    t = _SweepThrottle(
        base_stagger=0.15, max_stagger=0.15, breaker_threshold=0, concurrency=3
    )
    starts: list[float] = []
    began = time.monotonic()

    async def one() -> None:
        async with t.slot():
            starts.append(time.monotonic() - began)
            await asyncio.sleep(0.25)

    await asyncio.gather(*(one() for _ in range(3)))
    starts.sort()
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    expect("no two lanes leave at the same instant",
           all(g >= 0.12 for g in gaps), repr(gaps))

    # And one lane is still paced from the END of the previous check, so a
    # slow check does not get a second request fired on top of it.
    t2 = _SweepThrottle(base_stagger=0.2, max_stagger=0.2, breaker_threshold=0)
    began = time.monotonic()
    async with t2.slot():
        await asyncio.sleep(0.2)
    async with t2.slot():
        second = time.monotonic() - began
    expect("a single lane still measures the gap from the last request",
           second >= 0.4 - 0.05, f"{second:.3f}s")


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await test_a_prefetched_page_skips_this_hosts_refused_door()
    await test_the_sweep_asks_only_for_the_reels_it_will_read()
    await test_the_story_phase_reads_the_reel_the_phone_delivered()
    await test_the_shut_door_verdict_survives_a_restart()
    await test_the_phone_stands_by_while_this_host_can_fetch_pages()
    await test_one_odd_page_does_not_hand_the_phone_the_sweep()
    await test_a_refused_reel_stops_costing_the_phone_its_page_door()
    await test_a_private_account_never_buys_a_reel_query()
    await test_privacy_is_read_from_the_newest_successful_reading()
    await test_a_previous_sweeps_reel_is_not_read_as_this_ones()
    await test_a_sweep_does_not_re_read_every_accounts_id()
    await test_extra_lanes_are_spaced_not_bursted()

    await engine.dispose()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("All sweep-efficiency checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
