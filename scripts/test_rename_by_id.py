"""A renamed account is found by its Instagram ID, or reported as not found.

2026-11-11: a target that changed its username sat at nine failed checks,
"HTTP 401", its card still saying "🟢 active". The bot keeps the numeric id
for exactly this — it survives a rename — and it had stopped finding
anything through it:

- the ONE route it turned an id into a username with, the graphql reel query,
  was login-walled by Instagram on 2026-11-04 (401 "require_login",
  "igweb_rollout", for every id — @instagram's included, measured again on
  2026-11-11 directly and through the Worker);
- the other id route, users/<id>/info, was only ever asked to fill in a reel
  answer that had already arrived, so it was unreachable exactly when it was
  the one left;
- the old username's page reads as a block or as "not found", neither of
  which names the account's new handle;
- and with every id route refusing anonymous callers, adding the new username
  by hand made a SECOND entry with the same Instagram ID, while the first
  went on failing with all the history.

What must hold:
- the second id route is asked on its own when the reel query gives no
  answer, and an answer from it renames the account: same row, same id, same
  history, one message;
- a refusal (401/403) is never read as "deleted" or "renamed", and a route
  that refuses is never taken to say an id is gone;
- an account whose username is gone and whose id cannot be resolved is
  reported as exactly that — never shown as "🟢 active", never renamed on a
  guess;
- identity is the id: a username that answers as ANOTHER id is not this
  account, and nothing from it is stored or announced as a change;
- a new username added by hand is matched to the entry that holds its id —
  one entry, with its history — and a rename is announced once.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_rename_by_id.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from sqlalchemy import select  # noqa: E402

from app.bot import handlers, keyboards  # noqa: E402
from app.bot.notifications import render_not_found_message  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import crud  # noqa: E402
from app.database.models import AccountSnapshot, Base, MonitoredAccount  # noqa: E402
from app.database.session import dispose_engine, engine, get_session  # noqa: E402
from app.monitor import home_fetch, page_tunnel  # noqa: E402
from app.monitor import service as service_mod  # noqa: E402
from app.monitor.instagram import (  # noqa: E402
    PROFILE_REEL_QUERY_URL,
    PROFILE_URL,
    ROUTE_REEL,
    ROUTE_USER_INFO,
    IdProbe,
    InstagramClient,
    ProfileFetchResult,
)
from app.monitor.page_tunnel import PageTunnel  # noqa: E402
from app.monitor.service import MonitorService  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


# ---------- Instagram, as the bot reaches it ---------------------------------------

WORKER = "https://ig-proxy.example.workers.dev"
AVATAR = "https://scontent.cdninstagram.com/v/t51.2885-19/1_2_3_n.jpg?stp=x"

# The three refusals, as measured 2026-11-11 for the target's id and for
# @instagram's own: the reel query asked directly, either id route asked
# through the Worker, and the mobile user-info route asked directly.
LOGIN_WALL = {
    "message": "Please wait a few minutes before you try again.",
    "require_login": True, "igweb_rollout": True, "status": "fail",
}
WORKER_REFUSED = {"error": "upstream blocked after retries"}
MOBILE_REFUSED = {
    "message": "We're sorry, but something went wrong. Please try again.",
    "status_code": "403", "status": "fail",
}


class Resp:
    def __init__(self, status: int, body: Any = None, *, text: str = "", url: str = "") -> None:
        self.status_code = status
        self._body = body
        self.text = text or (json.dumps(body) if body is not None else "")
        self.url = url

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


def user_info(user_id: str, username: str, **extra: Any) -> dict:
    """users/<id>/info as an anonymous caller got it (2026-09-05): the id,
    the handle and the 150px avatar."""
    return {"user": {"pk": int(user_id), "username": username,
                     "profile_pic_url": AVATAR, **extra}, "status": "ok"}


def reel(user_id: str, username: str, *, story: bool = False) -> dict:
    return {"data": {"user": {
        "has_public_story": story, "is_live": False,
        "reel": {"id": user_id, "user": {
            "id": user_id, "username": username, "profile_pic_url": AVATAR}},
        "edge_highlight_reels": {"edges": []},
    }}}


def profile_page(username: str, pk: str, *, followers: int = 100, private: bool = True) -> str:
    return (
        "<!DOCTYPE html><html><head></head><body>"
        '<script type="application/json" data-sjs>'
        '{"require":[["RelayPrefetchedStreamCache","next",[],[{"__bbox":'
        '{"result":{"data":{"xig_user_by_username":'
        f'{{"pk":"{pk}","username":"{username}",'
        f'"is_private":{"true" if private else "false"},"biography":"bio",'
        '"full_name":"Somebody","is_verified":false,"bio_links":[],'
        f'"follower_count":{followers},"following_count":50,'
        '"all_media_count":null,"id":"17841400000000000"}'
        "}}}}]]]}</script></body></html>"
    )


def unavailable_page(username: str) -> str:
    """Instagram's "Profile isn't available" page, served with HTTP 200 for a
    username that has no profile (see test_page_unavailable)."""
    return (
        "<!DOCTYPE html><html><head><title>Instagram</title></head><body>"
        '<script type="application/json" data-sjs>{"initialRouteInfo":{"route":{'
        '"canonicalRouteName":"comet.igweb.PolarisErrorRoute",'
        '"polarisRouteConfig":{"pageID":"httpErrorPage"},'
        f'"url":"\\/{username}\\/"}}}}}}</script></body></html>'
    )


class Wire:
    """Every route scripted on its own. The defaults are Instagram as it is
    now: both id routes refuse, the username API answers 404 for everyone,
    and a username's page is the "isn't available" page until a test says
    whose profile it is."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.cookies: list[tuple[str, Optional[str]]] = []
        self.reel_worker: Callable[[str], Resp] = lambda uid: Resp(401, WORKER_REFUSED)
        self.reel_direct: Callable[[str], Resp] = lambda uid: Resp(401, LOGIN_WALL)
        self.info_worker: Callable[[str], Resp] = lambda uid: Resp(401, WORKER_REFUSED)
        self.info_direct: Callable[[str], Resp] = lambda uid: Resp(403, MOBILE_REFUSED)
        self.pages: dict[str, Callable[[str], Resp]] = {}
        self.username_api: Callable[[str], Resp] = lambda name: Resp(
            404, text="<html>Page Not Found</html>"
        )

    def page_is(self, username: str, pk: str, **kw: Any) -> None:
        self.pages[username] = lambda name: Resp(
            200, text=profile_page(name, pk, **kw),
            url=f"https://www.instagram.com/{name}/",
        )

    async def get(self, url: str, *, params: Any = None, headers: Any = None, **kw: Any) -> Resp:
        params, headers = dict(params or {}), dict(headers or {})
        cookie = headers.get("cookie") or headers.get("Cookie")
        if url == WORKER:
            if "user_id" in params:
                route, key, answer = "worker:reel", params["user_id"], self.reel_worker
            elif "hd_user_id" in params:
                route, key, answer = "worker:user_info", params["hd_user_id"], self.info_worker
            else:
                route, key, answer = "worker:username", params.get("username", ""), self.username_api
        elif url == PROFILE_REEL_QUERY_URL:
            route, key, answer = "direct:reel", params.get("user_id", ""), self.reel_direct
        elif url.startswith("https://i.instagram.com/api/v1/users/") and url.endswith("/info/"):
            route, key = "direct:user_info", url.split("/users/")[1].split("/")[0]
            answer = self.info_direct
        elif url == PROFILE_URL:
            route, key, answer = "direct:username", params.get("username", ""), self.username_api
        elif url.startswith("https://www.instagram.com/"):
            key = url[len("https://www.instagram.com/"):].strip("/")
            route = "page"
            answer = self.pages.get(key) or (lambda name: Resp(
                200, text=unavailable_page(name), url=f"https://www.instagram.com/{name}/"
            ))
        else:
            raise AssertionError(f"unexpected request: {url}")
        self.calls.append((route, str(key)))
        self.cookies.append((route, cookie))
        return answer(str(key))

    async def close(self) -> None:
        pass

    def asked(self, route: str) -> int:
        return sum(1 for name, _ in self.calls if name == route)

    def routes(self) -> list[str]:
        return [name for name, _ in self.calls]


# ---------- the service and its database -------------------------------------------

def make_service(instagram: Any, *, stories: Any = None) -> MonitorService:
    notifier = AsyncMock()
    notifier.send_text = AsyncMock(return_value=True)
    notifier.send_document = AsyncMock(return_value=True)
    notifier.create_forum_topic = AsyncMock(return_value=None)
    return MonitorService(
        instagram=instagram,
        hasher=AsyncMock(hash_url=AsyncMock(return_value=None)),
        notifier=notifier, stories=stories,
    )


def sent(service: MonitorService) -> list[str]:
    return [c.args[0] for c in service.notifier.send_text.await_args_list]


def renames(service: MonitorService) -> list[str]:
    return [t for t in sent(service) if "changed username" in t]


async def new_account(
    username: str, instagram_id: Optional[str], *, history: int = 2,
    private: bool = True, failures: int = 0, status: Optional[int] = None,
) -> int:
    """An entry with `history` successful readings on record."""
    async with get_session() as session:
        account = MonitoredAccount(
            username=username, active=True, instagram_id=instagram_id,
            consecutive_failures=failures, last_status_code=status,
        )
        session.add(account)
        await session.flush()
        for n in range(history):
            session.add(AccountSnapshot(
                account_id=account.id, username=username, http_status=200,
                full_name="Somebody", biography="bio", followers_count=100 + n,
                following_count=50, posts_count=3, is_private=private,
                raw_response=(
                    {"data": {"user": {"id": instagram_id}}} if instagram_id else None
                ),
            ))
        return account.id


async def row(account_id: int) -> Optional[MonitoredAccount]:
    async with get_session() as session:
        return await session.get(MonitoredAccount, account_id)


async def entries_with_id(instagram_id: str) -> list[MonitoredAccount]:
    async with get_session() as session:
        result = await session.execute(
            select(MonitoredAccount).where(MonitoredAccount.instagram_id == instagram_id)
        )
        return list(result.scalars().all())


async def readings(account_id: int) -> list[AccountSnapshot]:
    async with get_session() as session:
        return await crud.recent_snapshots(session, account_id, limit=50)


async def lost_note(service: MonitorService, account_id: int) -> Optional[dict]:
    async with get_session() as session:
        raw = await crud.get_setting(session, service._identity_key(account_id))
    return json.loads(raw) if raw else None


async def card(username: str) -> str:
    return await handlers._render_account_card(username, None, story_state="—") or ""


def pages_through_the_tunnel() -> PageTunnel:
    """The production state: this host's page requests leave through the
    page tunnel and are answered — so a sweep's first id probe asks nothing
    live, and the id routes are only asked about an account whose page fails."""
    up = PageTunnel()
    up.up, up._port = True, 1080
    return up


async def sweep_check(service: MonitorService, account_id: int, username: str) -> dict:
    """One account's check as a sweep runs it, the username API known shut."""
    return await service._run_check(
        account_id, username, thorough=False, skip_username_api=True, paced=True,
    )


# ---------- 1. the client: two id routes -------------------------------------------

async def test_the_second_id_route_answers_when_the_reel_query_is_walled() -> None:
    wire = Wire()
    wire.info_worker = lambda uid: Resp(200, user_info(uid, "New.Handle"))
    async with InstagramClient(max_retries=1, session=wire) as client:
        probe = await client.probe_by_id("7000000001", user_info=True)
    expect("the id resolves through the second route",
           probe.answered and probe.status == 200 and probe.via == "user-info", repr(probe))
    expect("to the handle Instagram gave, normalised", probe.username == "new.handle",
           repr(probe.username))
    expect("with its avatar", probe.profile_pic_url == AVATAR, repr(probe.profile_pic_url))
    expect("and no story status — that route does not know one", probe.reel_data is None,
           repr(probe.reel_data))
    expect("what each route said is on the probe",
           probe.routes == {ROUTE_REEL: 401, ROUTE_USER_INFO: 200}, repr(probe.routes))
    expect("the reel query was asked first, then the second route",
           wire.routes() == ["worker:reel", "direct:reel", "worker:user_info"], repr(wire.routes()))

    wire = Wire()
    wire.info_worker = lambda uid: Resp(200, user_info(uid, "new.handle"))
    async with InstagramClient(max_retries=1, session=wire) as client:
        probe = await client.probe_by_id("7000000001")
    expect("a caller that does not ask for the second route does not get it",
           probe.blocked and wire.asked("worker:user_info") == 0, repr((probe, wire.routes())))

    # The Worker's copy of the route is refused, Instagram's own answers.
    wire = Wire()
    wire.info_direct = lambda uid: Resp(200, user_info(uid, "new.handle"))
    async with InstagramClient(max_retries=1, session=wire) as client:
        probe = await client.probe_by_id("7000000001", user_info=True)
    expect("the direct request is tried when the Worker's is refused",
           probe.answered and probe.username == "new.handle"
           and wire.routes()[-2:] == ["worker:user_info", "direct:user_info"],
           repr((probe, wire.routes())))


async def test_a_reel_answer_needs_no_second_route() -> None:
    wire = Wire()
    wire.reel_worker = lambda uid: Resp(200, reel(uid, "Still.Here", story=True))
    async with InstagramClient(max_retries=1, session=wire) as client:
        probe = await client.probe_by_id("7000000001", user_info=True)
    expect("the reel query's answer stands on its own",
           probe.answered and probe.username == "still.here" and probe.via == "worker"
           and probe.reel_data == {"has_public_story": True, "is_live": False, "highlights": {}},
           repr(probe))
    expect("one request, as before", wire.routes() == ["worker:reel"], repr(wire.routes()))

    wire = Wire()
    wire.reel_worker = lambda uid: Resp(404, {})
    wire.info_worker = lambda uid: Resp(200, user_info(uid, "someone"))
    async with InstagramClient(max_retries=1, session=wire) as client:
        probe = await client.probe_by_id("7000000001", user_info=True)
    expect("the reel query's 404 is final: the id is gone",
           probe.gone and wire.routes() == ["worker:reel"], repr((probe, wire.routes())))


async def test_a_refusal_is_never_an_answer() -> None:
    """401 and 403 say the caller is not let in. They say nothing about the
    account: not that it was deleted, not that it was renamed."""
    wire = Wire()
    async with InstagramClient(max_retries=1, session=wire) as client:
        probe = await client.probe_by_id("7000000001", user_info=True)
    expect("both routes refused reads as blocked",
           probe.blocked and probe.status == 401 and not probe.answered, repr(probe))
    expect("never as gone", not probe.gone, repr(probe))
    expect("with no username invented", probe.username is None, repr(probe.username))
    expect("and each route's own refusal on record",
           probe.routes == {ROUTE_REEL: 401, ROUTE_USER_INFO: 403}, repr(probe.routes))

    # A 404 from the route that refuses anonymous callers is not "id gone":
    # on 2026-11-04 the username API began answering 404 for every username.
    wire = Wire()
    wire.info_worker = lambda uid: Resp(404, {"message": "not found"})
    wire.info_direct = lambda uid: Resp(404, {"message": "not found"})
    async with InstagramClient(max_retries=1, session=wire) as client:
        probe = await client.probe_by_id("7000000001", user_info=True)
    expect("the second route's 404 does not declare the id gone",
           not probe.gone and probe.blocked and probe.routes[ROUTE_USER_INFO] == 404, repr(probe))

    wire = Wire()
    async with InstagramClient(max_retries=1, session=wire) as client:
        probe = await client.probe_by_id("7000000001", reel=False, user_info=True)
    expect("asked alone and refused, the second route reads as blocked too",
           probe.blocked and probe.status == 403 and not probe.gone
           and wire.asked("worker:reel") == 0 and wire.asked("direct:reel") == 0,
           repr((probe, wire.routes())))


async def test_an_answer_must_be_about_the_id_that_was_asked() -> None:
    wire = Wire()
    wire.info_worker = lambda uid: Resp(200, user_info("999", "somebody.else"))
    wire.info_direct = lambda uid: Resp(200, user_info("999", "somebody.else"))
    async with InstagramClient(max_retries=1, session=wire) as client:
        probe = await client.probe_by_id("7000000001", user_info=True)
    expect("an answer that names another id is not used",
           not probe.answered and probe.username is None, repr(probe))
    expect("and is booked as no answer, not as a 200", probe.routes[ROUTE_USER_INFO] == 0,
           repr(probe.routes))

    for bad in ("not a handle!", "", "....", "x" * 31):
        wire = Wire()
        wire.info_worker = lambda uid, bad=bad: Resp(200, user_info(uid, bad))
        wire.info_direct = wire.info_worker
        async with InstagramClient(max_retries=1, session=wire) as client:
            probe = await client.probe_by_id("7000000001", user_info=True)
        expect(f"{bad[:14]!r} is not taken for a username", not probe.answered, repr(probe))

    wire = Wire()
    async with InstagramClient(max_retries=1, session=wire) as client:
        probe = await client.probe_by_id("7000000001/../x", reel=False, user_info=True)
    expect("an id that is not a number is not put into a URL",
           wire.calls == [] and not probe.answered, repr(wire.calls))


async def test_a_sweep_stops_knocking_on_a_refusing_second_route() -> None:
    """Refused through the Worker the route costs ~9 s (measured 2026-11-11).
    Like the reel route: three refusals in a row and a sweep takes the last
    one as the answer for the cooldown; a manual check still asks."""
    wire = Wire()
    info = lambda: wire.asked("worker:user_info")  # noqa: E731
    async with InstagramClient(max_retries=1, session=wire) as client:
        for _ in range(3):
            probe = await client.probe_by_id(
                "7000000001", reel=False, user_info=True, knock_refusing=False)
        expect("the refusals that trip the breaker are real knocks", info() == 3, repr(info()))
        probe = await client.probe_by_id(
            "7000000001", reel=False, user_info=True, knock_refusing=False)
        expect("then a sweep does not ask it", info() == 3, repr(info()))
        expect("and its last refusal stands",
               probe.blocked and probe.routes[ROUTE_USER_INFO] == 403, repr(probe))
        probe = await client.probe_by_id("7000000001", reel=False, user_info=True)
        expect("a manual check still knocks", info() == 4, repr(info()))
        client._user_info_blocked_until = 0.0  # the cooldown ran out
        await client.probe_by_id(
            "7000000001", reel=False, user_info=True, knock_refusing=False)
        expect("after the cooldown a sweep knocks once", info() == 5, repr(info()))
        await client.probe_by_id(
            "7000000001", reel=False, user_info=True, knock_refusing=False)
        expect("and that refusal re-arms the skip", info() == 5, repr(info()))

        wire.info_worker = lambda uid: Resp(200, user_info(uid, "new.handle"))
        client._user_info_blocked_until = 0.0
        probe = await client.probe_by_id(
            "7000000001", reel=False, user_info=True, knock_refusing=False)
        expect("the knock that is answered finds the username and clears the streak",
               probe.answered and client._user_info_failures == 0, repr(probe))


async def test_the_session_cookie_never_reaches_the_worker() -> None:
    """IG_SESSION_COOKIE is optional and off by default. Set, it makes the
    user-info route answer — asked of Instagram directly, with the cookie,
    and never through the Worker, which must not see it."""
    old = settings.ig_session_cookie
    settings.ig_session_cookie = "sessionid=TEST-ONLY; ds_user_id=1"
    try:
        wire = Wire()
        wire.info_direct = lambda uid: Resp(200, user_info(uid, "new.handle", full_name="N"))
        async with InstagramClient(max_retries=1, session=wire) as client:
            probe = await client.probe_by_id("7000000001", reel=False, user_info=True)
        expect("with a session the id resolves", probe.answered and probe.username == "new.handle",
               repr(probe))
        expect("the Worker is not asked at all", wire.asked("worker:user_info") == 0,
               repr(wire.routes()))
        expect("the cookie went to Instagram only",
               wire.cookies == [("direct:user_info", "sessionid=TEST-ONLY; ds_user_id=1")],
               repr(wire.cookies))
    finally:
        settings.ig_session_cookie = old

    wire = Wire()
    async with InstagramClient(max_retries=1, session=wire) as client:
        await client.probe_by_id("7000000001", user_info=True)
    expect("without one, no request carries a cookie",
           all(cookie is None for _, cookie in wire.cookies), repr(wire.cookies))


async def test_add_by_numeric_id_uses_both_routes() -> None:
    wire = Wire()
    wire.info_worker = lambda uid: Resp(200, user_info(uid, "By.Id"))
    async with InstagramClient(max_retries=1, session=wire) as client:
        name = await client.fetch_username_by_id("7000000001")
    expect("/add <numeric id> resolves through the second route", name == "by.id", repr(name))

    wire = Wire()
    async with InstagramClient(max_retries=1, session=wire) as client:
        name = await client.fetch_username_by_id("7000000001")
    expect("and says so when neither route names the account", name is None, repr(name))

    wire = Wire()
    wire.reel_worker = lambda uid: Resp(200, reel(uid, "by.reel"))
    async with InstagramClient(max_retries=1, session=wire) as client:
        name = await client.fetch_username_by_id("7000000001")
    expect("the reel query's answer is still one request",
           name == "by.reel" and wire.routes() == ["worker:reel"], repr((name, wire.routes())))


# ---------- 2. the rename: same id, new username -----------------------------------

async def test_a_sweep_finds_the_new_username_by_id() -> None:
    """The whole path as on 2026-11-11, with the second route answering: the
    old username's page is "isn't available", the reel query is walled."""
    old_tunnel = page_tunnel.tunnel
    page_tunnel.tunnel = pages_through_the_tunnel()
    try:
        await _a_sweep_finds_the_new_username_by_id()
    finally:
        page_tunnel.tunnel = old_tunnel

    # With no page door serving, a sweep asks by id FIRST: the rename is
    # known before the username side is asked, which then reads the profile
    # under the new name in the same check.
    account_id = await new_account("direct.old", "7000000019", failures=2, status=401)
    wire = Wire()
    wire.info_worker = lambda uid: Resp(200, user_info(uid, "direct.new"))
    wire.page_is("direct.new", "7000000019", followers=140)
    async with InstagramClient(max_retries=1, session=wire) as client:
        service = make_service(client)
        result = await sweep_check(service, account_id, "direct.old")
    expect("asked by id first, the profile is read under the new name at once",
           wire.calls == [("worker:reel", "7000000019"), ("direct:reel", "7000000019"),
                          ("worker:user_info", "7000000019"), ("page", "direct.new")],
           repr(wire.calls))
    expect("as a full page reading of the same entry",
           result.get("ok") is True and result.get("partial") == "public_page"
           and (await row(account_id)).username == "direct.new"
           and (await readings(account_id))[0].followers_count == 140, repr(result))
    expect("announced once", len(renames(service)) == 1 and len(sent(service)) == 1,
           repr(sent(service)))


async def _a_sweep_finds_the_new_username_by_id() -> None:
    account_id = await new_account("old.handle", "7000000001", failures=9, status=401)
    before = await readings(account_id)
    wire = Wire()
    wire.info_worker = lambda uid: Resp(200, user_info(uid, "New.Handle"))
    wire.page_is("new.handle", "7000000001", followers=140)
    async with InstagramClient(max_retries=1, session=wire) as client:
        service = make_service(client)
        result = await sweep_check(service, account_id, "old.handle")

        expect("the check succeeds, as an id-only reading",
               result.get("ok") is True and result.get("partial") == "id_probe", repr(result))
        expect("under the new username", result.get("username") == "new.handle", repr(result))
        expect("the page was asked first, the id routes only when it failed",
               wire.routes() == ["page", "worker:reel", "direct:reel", "worker:user_info"],
               repr(wire.routes()))

        account = await row(account_id)
        expect("the SAME entry carries the new username",
               account is not None and account.username == "new.handle", repr(account))
        expect("its Instagram ID is untouched", account.instagram_id == "7000000001",
               repr(account.instagram_id))
        expect("the failures are over", account.consecutive_failures == 0
               and account.last_status_code == 200,
               repr((account.consecutive_failures, account.last_status_code)))
        holders = await entries_with_id("7000000001")
        expect("one entry holds the id — no duplicate", [a.id for a in holders] == [account_id],
               repr([a.id for a in holders]))
        async with get_session() as session:
            expect("the old username leads to no entry",
                   await crud.get_account(session, "old.handle") is None)

        after = await readings(account_id)
        expect("every earlier reading is still on record",
               {s.id for s in before} <= {s.id for s in after}, repr((len(before), len(after))))
        newest = after[0]
        expect("the new reading is an id-only one under the new name",
               crud.snapshot_source(newest) == "id_probe" and newest.username == "new.handle",
               repr((crud.snapshot_source(newest), newest.username)))
        expect("carrying the last known numbers forward, not blanks",
               newest.followers_count == 101 and newest.biography == "bio",
               repr((newest.followers_count, newest.biography)))

        texts = sent(service)
        expect("the rename is announced once", len(renames(service)) == 1, repr(texts))
        expect("naming both handles and the id as its source",
               "@old.handle" in texts[0] and "@new.handle" in texts[0]
               and "stored Instagram ID" in texts[0], texts[0] if texts else "")
        expect("and nothing else is said", len(texts) == 1, repr(texts))

        # The next sweep still has the old name in its list.
        wire.calls.clear()
        again = await sweep_check(service, account_id, "old.handle")
        expect("a check queued under the old name asks for the new one",
               ("page", "new.handle") in wire.calls and ("page", "old.handle") not in wire.calls,
               repr(wire.calls))
        expect("and reads the profile in full", again.get("ok") is True
               and again.get("partial") == "public_page", repr(again))
        expect("without announcing the rename a second time", len(renames(service)) == 1,
               repr(sent(service)))
        account = await row(account_id)
        expect("still one entry, still the same id",
               account.username == "new.handle" and account.instagram_id == "7000000001"
               and len(await entries_with_id("7000000001")) == 1, repr(account.username))


async def test_a_manual_check_asks_the_second_route_only_when_it_must() -> None:
    account_id = await new_account("manual.old", "7000000002")
    wire = Wire()
    wire.info_worker = lambda uid: Resp(200, user_info(uid, "manual.new"))
    async with InstagramClient(max_retries=1, session=wire) as client:
        service = make_service(client)
        result = await service.check_username("manual.old")
    expect("a Recheck finds the rename", result.get("ok") is True
           and result.get("username") == "manual.new", repr(result))
    expect("asking the reel query once, the username side, then the second route",
           wire.routes() == ["worker:reel", "direct:reel", "worker:username", "page",
                             "worker:user_info"], repr(wire.routes()))
    expect("one rename message", len(renames(service)) == 1, repr(sent(service)))
    account = await row(account_id)
    expect("on the same entry, with the same id",
           account.username == "manual.new" and account.instagram_id == "7000000002"
           and len(await entries_with_id("7000000002")) == 1,
           repr((account.username, account.instagram_id)))

    # An account that answers under its own name costs no second-route call.
    healthy_id = await new_account("manual.fine", "7000000003")
    wire = Wire()
    wire.page_is("manual.fine", "7000000003")
    async with InstagramClient(max_retries=1, session=wire) as client:
        service = make_service(client)
        result = await service.check_username("manual.fine")
    expect("a Recheck of an account that answers is as cheap as before",
           result.get("ok") is True and wire.asked("worker:user_info") == 0
           and wire.asked("direct:user_info") == 0, repr((result, wire.routes())))
    expect("and says nothing about a rename", renames(service) == [], repr(sent(service)))
    expect("(its entry is untouched)", (await row(healthy_id)).username == "manual.fine")


async def test_the_same_username_is_not_a_rename() -> None:
    """The id route spells the handle its own way. That is not a change."""
    account_id = await new_account("same.name", "7000000004")
    wire = Wire()
    wire.info_worker = lambda uid: Resp(200, user_info(uid, " @Same.Name "))
    wire.pages["same.name"] = lambda name: Resp(429, text="")
    async with InstagramClient(max_retries=1, session=wire) as client:
        service = make_service(client)
        for _ in range(3):
            result = await sweep_check(service, account_id, "same.name")
    expect("the check is an id-only reading", result.get("ok") is True
           and result.get("partial") == "id_probe", repr(result))
    expect("no rename is announced", renames(service) == [], repr(sent(service)))
    expect("nothing at all is said", sent(service) == [], repr(sent(service)))
    expect("the username is as it was", (await row(account_id)).username == "same.name")

    service = make_service(AsyncMock())
    kept = await service._apply_rename(account_id, "same.name", "not a handle!")
    expect("a name that is no Instagram handle is never stored",
           kept == "same.name" and (await row(account_id)).username == "same.name"
           and sent(service) == [], repr((kept, sent(service))))


# ---------- 3. refused, unavailable, unresolved ------------------------------------

async def test_a_block_is_not_a_deletion_and_not_a_rename() -> None:
    """Every route refuses: the page door is sent to the login page and both
    id routes answer 401/403. Nine checks of that say the bot was not let in.
    They do not say the account is gone, or has a new name."""
    account_id = await new_account("walled.off", "7000000005")
    before = await readings(account_id)
    wire = Wire()
    wire.pages["walled.off"] = lambda name: Resp(
        200, text="<html>Log in</html>",
        url=f"https://www.instagram.com/accounts/login/?next=%2F{name}%2F",
    )
    async with InstagramClient(max_retries=1, session=wire) as client:
        service = make_service(client)
        for _ in range(9):
            result = await sweep_check(service, account_id, "walled.off")
    expect("the check fails as blocked", result.get("ok") is False
           and result.get("status") == 401 and result.get("id_status") in (401, 403),
           repr(result))
    account = await row(account_id)
    expect("every failure is booked", account.consecutive_failures == 9
           and account.last_status_code == 401,
           repr((account.consecutive_failures, account.last_status_code)))
    expect("the username is not changed", account.username == "walled.off", account.username)
    expect("nor the id", account.instagram_id == "7000000005", repr(account.instagram_id))
    texts = sent(service)
    expect("nothing says the account is missing, deleted or renamed",
           not any(w in t for t in texts
                   for w in ("doesn't exist", "deleted", "changed username", "no longer")),
           repr(texts))
    expect("no note that its username leads nowhere is kept",
           await lost_note(service, account_id) is None,
           repr(await lost_note(service, account_id)))
    expect("no reading is written for a block", len(await readings(account_id)) == len(before))
    text = await card("walled.off")
    expect("but the card no longer calls it active and well",
           "State: ⚠️ active — not answering (9 checks failed in a row)" in text
           and "🟢" not in text, text)


async def test_a_username_that_is_gone_and_an_id_nobody_answers_for() -> None:
    """The reported case, as it stands today: no profile under the old name,
    and every id route refusing. The bot cannot know the new username, and
    must say that — not go quiet, and not call the account well."""
    account_id = await new_account("gone.name", "7000000006")
    old_tunnel = page_tunnel.tunnel
    page_tunnel.tunnel = pages_through_the_tunnel()
    try:
        wire = Wire()
        async with InstagramClient(max_retries=1, session=wire) as client:
            service = make_service(client)
            first = await sweep_check(service, account_id, "gone.name")
            expect("the check fails as not found, with the id routes' refusal on record",
                   first.get("ok") is False and first.get("status") == 404
                   and first.get("id_status") == 401, repr(first))
            expect("both id routes were asked before giving up",
                   wire.asked("worker:reel") == 1 and wire.asked("worker:user_info") == 1,
                   repr(wire.routes()))
            expect("one miss is not announced (it can be a flake)", sent(service) == [],
                   repr(sent(service)))
            expect("nor shown on the card yet",
                   "username gone" not in await card("gone.name"), await card("gone.name"))

            await sweep_check(service, account_id, "gone.name")
            texts = sent(service)
            expect("the second miss is announced", len(texts) == 1, repr(texts))
            expect("as a username that no longer exists",
                   "@gone.name" in texts[0] and "doesn't exist (2 checks in a row)" in texts[0],
                   texts[0] if texts else "")
            expect("naming what each id route said",
                   "ID lookup was blocked (reel query: HTTP 401; user info: HTTP 403)" in texts[0],
                   texts[0] if texts else "")
            expect("and what the owner can do about it", "/add newname" in texts[0],
                   texts[0] if texts else "")
            expect("it does not claim a rename, or a deletion",
                   "changed username" not in texts[0] and "deleted" not in texts[0], texts[0])

            account = await row(account_id)
            expect("the entry keeps its username and id — nothing is guessed",
                   account.username == "gone.name" and account.instagram_id == "7000000006",
                   repr((account.username, account.instagram_id)))
            expect("and is not marked healthy",
                   account.consecutive_failures == 2 and account.last_status_code == 404,
                   repr((account.consecutive_failures, account.last_status_code)))
            note = await lost_note(service, account_id)
            expect("the unresolved state is on record",
                   note is not None and note["reason"] == "id_blocked" and note["misses"] == 2
                   and note["routes"] == {ROUTE_REEL: 401, ROUTE_USER_INFO: 403}, repr(note))
            text = await card("gone.name")
            expect("the card says the username is gone and the current one unknown",
                   "State: 🔴 active — username gone, current one not found" in text, text)
            expect("never '🟢 active'", "🟢" not in text, text)
            expect("says the lookup by ID was refused, and how to relink",
                   "refused the lookup" in text and "/add newname" in text, text)
            buttons = keyboards.accounts_list([account]).inline_keyboard
            expect("and the list marks it as failing, not green",
                   buttons[0][0].text == "⚠️ @gone.name", buttons[0][0].text)

            # Instagram answers by id again: the rename is found on the spot.
            wire.info_worker = lambda uid: Resp(200, user_info(uid, "found.name"))
            client._user_info_blocked_until = 0.0
            result = await sweep_check(service, account_id, "gone.name")
            expect("the first id answer resolves it", result.get("ok") is True
                   and result.get("username") == "found.name", repr(result))
            account = await row(account_id)
            expect("on the same entry", account.username == "found.name"
                   and account.instagram_id == "7000000006"
                   and account.consecutive_failures == 0, repr(account.username))
            expect("the unresolved note is cleared",
                   await lost_note(service, account_id) is None)
            expect("the rename is announced once", len(renames(service)) == 1,
                   repr(sent(service)))
            expect("and the card is green again",
                   "State: 🟢 active" in await card("found.name"), await card("found.name"))
    finally:
        page_tunnel.tunnel = old_tunnel


async def test_an_id_that_no_longer_resolves_is_gone_not_renamed() -> None:
    account_id = await new_account("deleted.one", "7000000007")
    wire = Wire()
    wire.reel_worker = lambda uid: Resp(404, {})
    async with InstagramClient(max_retries=1, session=wire) as client:
        service = make_service(client)
        result = await service.check_username("deleted.one")
    expect("the check fails with the id's own 404", result.get("ok") is False
           and result.get("status") == 404 and result.get("id_status") == 404, repr(result))
    texts = sent(service)
    expect("said at once, as deactivated or deleted — not renamed",
           len(texts) == 1 and "deactivated or deleted" in texts[0]
           and "changed username" not in texts[0], repr(texts))
    account = await row(account_id)
    expect("the entry is kept, with its history", account is not None
           and account.username == "deleted.one" and len(await readings(account_id)) == 2)
    text = await card("deleted.one")
    expect("the card says the id no longer resolves",
           "State: 🔴 active — the Instagram ID no longer resolves" in text, text)


# ---------- 4. identity is the id, not the username --------------------------------

class _Stories:
    """The media source, which works by username."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def fetch_stories(self, username: str) -> list:
        self.asked.append(username)
        return []

    async def fetch_highlight_items(self, username: str, highlight_id: str, title: str) -> list:
        return []

    async def fetch_posts(self, username: str, limit: int = 12) -> list:
        return []

    async def fetch_profile_pic_url(self, username: str) -> Optional[str]:
        return None

    def refusal_since(self, since: float) -> Optional[str]:
        return None

    def highlight_failure_since(self, since: float) -> Optional[str]:
        return None


async def test_a_username_taken_over_by_another_account_is_not_this_account() -> None:
    """The target renames, and someone else takes the old handle. The old
    username answers again — as a different Instagram ID. Read as the same
    account, a stranger's numbers land in this account's history as "changes"
    and a stranger's stories are delivered as the target's."""
    async with get_session() as session:
        for a in await crud.list_accounts(session, only_active=True):
            await crud.set_account_active(session, a.username, False)
    account_id = await new_account("taken.handle", "7000000008", private=False)
    control_id = await new_account("plain.gone", "7000000009", private=False)
    before = await readings(account_id)
    wire = Wire()
    wire.page_is("taken.handle", "5555555555", followers=3, private=False)  # the new owner
    old_tunnel = page_tunnel.tunnel
    page_tunnel.tunnel = pages_through_the_tunnel()
    try:
        async with InstagramClient(max_retries=1, session=wire) as client:
            service = make_service(client)
            first = await sweep_check(service, account_id, "taken.handle")
            expect("the reading is not accepted as this account's",
                   first.get("ok") is False and first.get("status") == 404
                   and first.get("foreign_id") == "5555555555", repr(first))
            expect("the id routes were asked where the account went",
                   wire.asked("worker:reel") == 1 and wire.asked("worker:user_info") == 1,
                   repr(wire.routes()))
            after = await readings(account_id)
            expect("nothing from the other account is stored",
                   [s.id for s in after] == [s.id for s in before]
                   and after[0].followers_count == 101, repr([s.followers_count for s in after]))
            expect("no change is announced", sent(service) == [], repr(sent(service)))

            await sweep_check(service, account_id, "taken.handle")
            texts = sent(service)
            expect("the second such answer says whose username it is now",
                   len(texts) == 1 and "now belongs to a different Instagram account" in texts[0]
                   and "5555555555" in texts[0] and "7000000008" in texts[0], repr(texts))
            account = await row(account_id)
            expect("the stored id is not replaced by the username's new owner",
                   account.instagram_id == "7000000008" and account.username == "taken.handle",
                   repr((account.instagram_id, account.username)))
            text = await card("taken.handle")
            expect("the card says so",
                   "State: 🔴 active — this username is another account's now" in text
                   and "5555555555" in text, text)

            # In a sweep the story phase works by username: not for this one.
            stories = _Stories()
            service = make_service(client, stories=stories)
            await service.check_all()
            expect("a sweep does not fetch the new owner's stories as the target's",
                   "taken.handle" not in stories.asked, repr(stories.asked))
            expect("while an account that is merely not found is handled as before",
                   "plain.gone" in stories.asked, repr(stories.asked))
            expect("(the control is the other entry)", (await row(control_id)) is not None)

            # The id route answers at last: the account moved to a new name,
            # which is its own — so its story phase runs again, under it.
            wire.info_worker = lambda uid: (
                Resp(200, user_info(uid, "taken.new")) if uid == "7000000008"
                else Resp(401, WORKER_REFUSED)
            )
            client._user_info_blocked_until, client._user_info_failures = 0.0, 0
            stories = _Stories()
            service = make_service(client, stories=stories)
            await service.check_all()
            account = await row(account_id)
            expect("once the id names the new handle, the entry follows it",
                   account.username == "taken.new" and account.instagram_id == "7000000008",
                   repr((account.username, account.instagram_id)))
            expect("and its story phase runs under that name, not the taken one",
                   "taken.new" in stories.asked and "taken.handle" not in stories.asked,
                   repr(stories.asked))
            expect("announced once", len(renames(service)) == 1, repr(sent(service)))
    finally:
        page_tunnel.tunnel = old_tunnel


def test_only_a_real_id_decides_whose_profile_it_is() -> None:
    """Two ids that differ are two accounts. Anything short of that — no id
    on the reading, none stored, a placeholder — decides nothing, and the
    reading is taken as it always was."""
    def reading(pk: Any) -> ProfileFetchResult:
        return ProfileFetchResult(
            username="x", http_status=200, parsed={"username": "x", "instagram_id": pk},
        )

    owner = MonitorService._foreign_owner
    expect("a different id is another account", owner(reading("222"), "111") == "222")
    expect("the same id, however it is written, is this account",
           owner(reading("111"), "111") is None and owner(reading(111), " 111 ") is None
           and owner(reading("0111"), "111") is None)
    expect("a reading with no id decides nothing",
           owner(reading(None), "111") is None and owner(reading(""), "111") is None)
    expect("nor does a placeholder", owner(reading("0"), "111") is None
           and owner(reading("17841400000000000x"), "111") is None)
    expect("nor an account with no id stored", owner(reading("222"), None) is None
           and owner(reading("222"), "") is None)
    expect("nor a reading that failed",
           owner(ProfileFetchResult(username="x", http_status=401), "111") is None)


async def test_adding_the_new_username_relinks_the_old_entry() -> None:
    """What the owner can do today, when no id route answers: add the new
    username. Its profile carries the id, so it is matched to the entry that
    holds it — one entry, the old one, with all it has on record."""
    old_id = await new_account("relink.old", "7000000010", failures=9, status=404)
    before = await readings(old_id)
    async with get_session() as session:
        await crud.set_setting(session, MonitorService._not_found_key(old_id),
                               "2026-11-11T07:39:02+00:00")
        fresh, created = await crud.add_account(session, "relink.new", added_by=1)
        await crud.set_media_only(session, fresh.id, True)  # /addmedia
        fresh_id = fresh.id
    expect("(the new username starts as an entry of its own)", created and fresh_id != old_id)

    wire = Wire()
    wire.page_is("relink.new", "7000000010", followers=140)
    async with InstagramClient(max_retries=1, session=wire) as client:
        service = make_service(client)
        result = await service.check_username("relink.new")

    expect("the check answers for the entry that was already there",
           result.get("ok") is True and result.get("username") == "relink.new"
           and result.get("relinked_from") == "relink.old", repr(result))
    holders = await entries_with_id("7000000010")
    expect("ONE entry holds the id, and it is the old one",
           [a.id for a in holders] == [old_id], repr([(a.id, a.username) for a in holders]))
    account = await row(old_id)
    expect("under the new username", account.username == "relink.new", account.username)
    expect("the entry added a moment ago is gone", await row(fresh_id) is None)
    async with get_session() as session:
        names = [a.username for a in await crud.list_accounts(session, only_active=False)]
        expect("so the account is listed once",
               names.count("relink.new") == 1 and "relink.old" not in names, repr(names))
        expect("what /addmedia asked for carries over",
               await crud.is_media_only(session, old_id)
               and await crud.get_setting(session, f"media_only:{fresh_id}") is None)
    after = await readings(old_id)
    expect("every reading it had is still there, and the new one joins them",
           {s.id for s in before} < {s.id for s in after}, repr((len(before), len(after))))
    expect("the new reading is the profile's own", after[0].followers_count == 140
           and after[0].username == "relink.new",
           repr((after[0].followers_count, after[0].username)))
    expect("its nine failures are over", account.consecutive_failures == 0
           and account.last_status_code == 200,
           repr((account.consecutive_failures, account.last_status_code)))
    texts = sent(service)
    expect("the rename is announced once", len(renames(service)) == 1, repr(texts))
    expect("as confirmed by the id on the new profile",
           "@relink.old" in renames(service)[0] and "@relink.new" in renames(service)[0]
           and "Confirmed by the Instagram ID" in renames(service)[0], renames(service)[0])
    expect("and the standing “doesn't exist” alert is corrected",
           any("answers again" in t for t in texts), repr(texts))
    expect("the entry is green again", "State: 🟢 active" in await card("relink.new"),
           await card("relink.new"))


async def test_a_username_with_its_own_id_is_a_new_account() -> None:
    old_id = await new_account("other.old", "7000000011")
    async with get_session() as session:
        fresh, _ = await crud.add_account(session, "other.new")
        fresh_id = fresh.id
    wire = Wire()
    wire.page_is("other.new", "7000000012")
    async with InstagramClient(max_retries=1, session=wire) as client:
        service = make_service(client)
        result = await service.check_username("other.new")
    expect("an id nobody holds makes a new entry, as ever",
           result.get("ok") is True and "relinked_from" not in result, repr(result))
    expect("which gets that id", (await row(fresh_id)).instagram_id == "7000000012")
    expect("and the other entry is left alone",
           (await row(old_id)).username == "other.old" and renames(service) == [],
           repr(sent(service)))


async def test_a_rename_absorbs_the_copy_added_before_it_was_known() -> None:
    """The owner added the new name while nothing could confirm it; later an
    id route answers for the old entry. Two entries for one account must not
    be what is left."""
    old_id = await new_account("absorb.old", "7000000013")
    async with get_session() as session:
        fresh, _ = await crud.add_account(session, "absorb.new")
        fresh_id = fresh.id
    wire = Wire()
    wire.info_worker = lambda uid: Resp(200, user_info(uid, "absorb.new"))
    wire.pages["absorb.new"] = lambda name: Resp(429, text="")
    async with InstagramClient(max_retries=1, session=wire) as client:
        service = make_service(client)
        result = await sweep_check(service, old_id, "absorb.old")
    expect("the rename is applied", result.get("ok") is True
           and (await row(old_id)).username == "absorb.new", repr(result))
    expect("the copy with nothing on record is folded in", await row(fresh_id) is None)
    text = renames(service)[0] if renames(service) else ""
    expect("and it is announced as a plain rename, once",
           len(renames(service)) == 1 and "separate entry" not in text, repr(sent(service)))


async def test_a_sweep_skips_the_check_of_an_entry_folded_in_meanwhile() -> None:
    """A sweep's list holds both the old entry and the copy added under the
    new name. The old entry is checked first (it is failing), its id route
    names the new handle, and the copy is folded in — while its own check is
    still queued. That check must not write readings for a row that is gone,
    nor count as a failure."""
    async with get_session() as session:
        for a in await crud.list_accounts(session, only_active=True):
            await crud.set_account_active(session, a.username, False)
    old_id = await new_account("fold.old", "7000000022", failures=3, status=404)
    async with get_session() as session:
        fresh, _ = await crud.add_account(session, "fold.new")
        fresh_id = fresh.id
    wire = Wire()
    wire.info_worker = lambda uid: Resp(200, user_info(uid, "fold.new"))
    wire.page_is("fold.new", "7000000022", followers=150)
    async with InstagramClient(max_retries=1, session=wire) as client:
        service = make_service(client)
        result = await service.check_all()
    account = await row(old_id)
    expect("the old entry follows the new name, with its id",
           account.username == "fold.new" and account.instagram_id == "7000000022"
           and await row(fresh_id) is None, repr(account.username))
    async with get_session() as session:
        orphans = (await session.execute(
            select(AccountSnapshot.id).where(AccountSnapshot.account_id == fresh_id)
        )).all()
    expect("the folded copy's queued check wrote nothing", orphans == [], repr(orphans))
    expect("its page was read once — by the old entry's check",
           wire.calls.count(("page", "fold.new")) == 1, repr(wire.calls))
    expect("and the sweep counts one check, no failure",
           result["checked"] == 1 and result["failed"] == 0, repr(result))
    summary = sent(service)[-1]
    expect("so the summary names nobody as failed", " failed: " not in summary, summary)
    expect("one rename message, a plain one",
           len(renames(service)) == 1 and "separate entry" not in renames(service)[0],
           repr(sent(service)))


async def test_a_username_held_by_another_account_is_said_once() -> None:
    """A real collision: the new username is an entry with an id and history
    of its own. Neither is touched — and the rename that cannot be applied
    was announced again on every check."""
    renamed_id = await new_account("clash.old", "7000000014")
    holder_id = await new_account("clash.taken", "7000000015")
    wire = Wire()
    wire.info_worker = lambda uid: Resp(200, user_info(uid, "clash.taken"))
    wire.pages["clash.taken"] = lambda name: Resp(429, text="")
    async with InstagramClient(max_retries=1, session=wire) as client:
        service = make_service(client)
        for _ in range(4):
            await sweep_check(service, renamed_id, "clash.old")
    expect("the entry keeps its old name in the list",
           (await row(renamed_id)).username == "clash.old")
    expect("the other entry is untouched",
           (await row(holder_id)).username == "clash.taken"
           and (await row(holder_id)).instagram_id == "7000000015")
    expect("four checks, one message", len(renames(service)) == 1, repr(sent(service)))
    expect("which says why the name was not taken",
           "already monitored as a separate entry" in renames(service)[0], renames(service)[0])


async def test_one_id_is_never_stored_on_two_entries() -> None:
    """An entry with history of its own is not merged behind the owner's
    back — and it does not get the id a second entry already holds."""
    holder_id = await new_account("twin.one", "7000000016")
    idless_id = await new_account("twin.two", None)
    wire = Wire()
    wire.page_is("twin.two", "7000000016")
    async with InstagramClient(max_retries=1, session=wire) as client:
        service = make_service(client)
        result = await service.check_username("twin.two")
    expect("the check still reads the profile", result.get("ok") is True, repr(result))
    expect("the id stays with the entry that holds it",
           [a.id for a in await entries_with_id("7000000016")] == [holder_id])
    expect("neither entry is removed or renamed",
           (await row(idless_id)).username == "twin.two"
           and (await row(holder_id)).username == "twin.one" and renames(service) == [])

    # ...and the entry that is failing is told its account is followed elsewhere.
    text = render_not_found_message(
        "twin.one", 2, IdProbe(user_id="7000000016", status=401), twin="twin.two",
    )
    expect("a “not found” alert names the entry that holds the same id",
           "already monitored as @twin.two" in text and "/add newname" not in text, text)


async def test_a_sweep_relinks_and_recovers_in_one_pass() -> None:
    """The entry added for the new name and the old one failing as not found
    meet in the same sweep."""
    async with get_session() as session:
        for a in await crud.list_accounts(session, only_active=True):
            await crud.set_account_active(session, a.username, False)
    old_id = await new_account("pass.old", "7000000017", failures=4, status=404)
    async with get_session() as session:
        fresh, _ = await crud.add_account(session, "pass.new")
        fresh_id = fresh.id
    wire = Wire()
    wire.page_is("pass.new", "7000000017")
    old = (page_tunnel.tunnel, settings.sweep_retry_rounds,
           service_mod._SWEEP_RETRY_COOLDOWN_SECONDS, service_mod._SWEEP_RETRY_GAP_SECONDS)
    page_tunnel.tunnel = pages_through_the_tunnel()
    settings.sweep_retry_rounds = 1
    service_mod._SWEEP_RETRY_COOLDOWN_SECONDS = 0.0
    service_mod._SWEEP_RETRY_GAP_SECONDS = (0.0, 0.0)
    try:
        async with InstagramClient(max_retries=1, session=wire) as client:
            service = make_service(client)
            async with get_session() as session:
                await crud.set_setting(session, "username_api_closed_at",
                                       "2999-01-01T00:00:00+00:00")
            result = await service.check_all()
    finally:
        (page_tunnel.tunnel, settings.sweep_retry_rounds,
         service_mod._SWEEP_RETRY_COOLDOWN_SECONDS, service_mod._SWEEP_RETRY_GAP_SECONDS) = old
        async with get_session() as session:
            await crud.delete_setting(session, "username_api_closed_at")
    account = await row(old_id)
    expect("after the sweep there is one entry, under the new name",
           account.username == "pass.new" and await row(fresh_id) is None
           and len(await entries_with_id("7000000017")) == 1, repr(account.username))
    expect("answering", account.consecutive_failures == 0 and account.last_status_code == 200,
           repr((account.consecutive_failures, account.last_status_code)))
    expect("nothing is left failing", result["failed"] == 0, repr(result))
    expect("one rename message", len(renames(service)) == 1, repr(sent(service)))
    expect("and the page of the old name was asked at most once",
           wire.calls.count(("page", "pass.old")) <= 1, repr(wire.calls))


# ---------- 5. what the owner is shown ---------------------------------------------

def test_the_state_line_is_green_only_for_an_account_that_answers() -> None:
    def account(**kw: Any) -> SimpleNamespace:
        base = {"active": True, "consecutive_failures": 0, "username": "x", "instagram_id": "1"}
        return SimpleNamespace(**{**base, **kw})

    label = handlers._state_label
    expect("answering: green", label(account(), None) == "🟢 active")
    expect("paused: paused, whatever else", label(account(active=False, consecutive_failures=9),
                                                   {"reason": "id_blocked"}) == "⏸ paused")
    expect("failing: said, with the count",
           label(account(consecutive_failures=1), None)
           == "⚠️ active — not answering (1 check failed in a row)")
    expect("one flaky “not found” is not yet a lost username",
           handlers._identity_lost(json.dumps({"reason": "id_blocked", "misses": 1})) is None)
    expect("two in a row are",
           handlers._identity_lost(json.dumps({"reason": "id_blocked", "misses": 2})) is not None)
    expect("a gone id is at once",
           handlers._identity_lost(json.dumps({"reason": "id_gone", "misses": 1})) is not None)
    expect("an unreadable note is no note",
           handlers._identity_lost("{not json") is None and handlers._identity_lost("[]") is None
           and handlers._identity_lost(None) is None)
    no_id = handlers._identity_lost_lines(
        account(instagram_id=None), {"reason": "no_id", "misses": 2})
    expect("with no id stored there is nothing to relink by, and it says so",
           any("No Instagram ID is stored" in line for line in no_id)
           and not any("/add" in line for line in no_id), repr(no_id))

    marks = [b[0].text for b in keyboards.accounts_list([
        account(username="fine"), account(username="failing", consecutive_failures=3),
        account(username="paused", active=False, consecutive_failures=3),
    ]).inline_keyboard[:3]]
    expect("the list marks failing accounts apart from healthy and paused ones",
           marks == ["🟢 @fine", "⚠️ @failing", "⏸ @paused"], repr(marks))


async def test_status_says_which_failing_account_lost_its_username() -> None:
    service = make_service(AsyncMock())
    lost_id = await new_account("status.gone", "7000000020", failures=90, status=404)
    await new_account("status.walled", "7000000021", failures=91, status=401)
    async with get_session() as session:
        await crud.set_setting(session, service._identity_key(lost_id), json.dumps({
            "since": "2026-11-10T13:35:00+00:00", "misses": 3, "reason": "id_blocked",
            "routes": {ROUTE_REEL: 401, ROUTE_USER_INFO: 403},
        }))
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"monitor": service}))
    text = await handlers._render_status_message(context)
    expect("/status tells a lost username from a block",
           "@status.gone (90×, username gone)" in text and "@status.walled (91×)" in text,
           text)


async def test_add_replies_with_the_relinked_entry() -> None:
    """/add of the new username, through the handler: the reply is the old
    entry's card under its new name, saying nothing was added twice."""
    old_id = await new_account("reply.old", "7000000018", failures=9, status=404)
    wire = Wire()
    wire.page_is("reply.new", "7000000018", followers=77)
    status_msg = SimpleNamespace(edit_text=AsyncMock(), message_id=2, chat_id=7)
    update = SimpleNamespace(
        callback_query=None,
        message=SimpleNamespace(reply_text=AsyncMock(return_value=status_msg), message_id=1),
        effective_chat=SimpleNamespace(id=7), effective_user=SimpleNamespace(id=99),
    )
    async with InstagramClient(max_retries=1, session=wire) as client:
        service = make_service(client)
        context = SimpleNamespace(
            bot=SimpleNamespace(send_message=AsyncMock(), edit_message_text=AsyncMock()),
            user_data={}, args=[], application=SimpleNamespace(bot_data={"monitor": service}),
        )
        await handlers._do_add(update, context, "reply.new")
    text = status_msg.edit_text.call_args.args[0] if status_msg.edit_text.call_args else ""
    expect("the reply says which entry this is",
           "already monitored as <b>@reply.old</b>" in text and "nothing was added twice" in text,
           text)
    expect("and carries that entry's card under the new name",
           "<b>@reply.new</b>" in text and "7000000018" in text and "State: 🟢 active" in text,
           text)
    expect("not 'Now monitoring', as for a new account", "Now monitoring" not in text, text)
    expect("one entry", [a.id for a in await entries_with_id("7000000018")] == [old_id])


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Keep the checks fast and deterministic.
    service_mod._SWEEP_STAGGER_SECONDS = 0.0
    service_mod._OFF_SCHEDULE_MIN_GAP_SECONDS = 0.0
    settings.sweep_stagger_max_seconds = 0
    settings.sweep_breaker_threshold = 3
    settings.sweep_breaker_cooldown_seconds = 0
    settings.sweep_retry_rounds = 0
    settings.sweep_concurrency = 1
    settings.dark_radar_days = 0
    settings.ig_proxy_url = WORKER
    settings.ig_session_cookie = None
    settings.home_fetch_token = None
    home_fetch.broker = home_fetch.HomeFetchBroker()

    await test_the_second_id_route_answers_when_the_reel_query_is_walled()
    await test_a_reel_answer_needs_no_second_route()
    await test_a_refusal_is_never_an_answer()
    await test_an_answer_must_be_about_the_id_that_was_asked()
    await test_a_sweep_stops_knocking_on_a_refusing_second_route()
    await test_the_session_cookie_never_reaches_the_worker()
    await test_add_by_numeric_id_uses_both_routes()

    await test_a_sweep_finds_the_new_username_by_id()
    await test_a_manual_check_asks_the_second_route_only_when_it_must()
    await test_the_same_username_is_not_a_rename()

    await test_a_block_is_not_a_deletion_and_not_a_rename()
    await test_a_username_that_is_gone_and_an_id_nobody_answers_for()
    await test_an_id_that_no_longer_resolves_is_gone_not_renamed()

    await test_a_username_taken_over_by_another_account_is_not_this_account()
    test_only_a_real_id_decides_whose_profile_it_is()
    await test_adding_the_new_username_relinks_the_old_entry()
    await test_a_username_with_its_own_id_is_a_new_account()
    await test_a_rename_absorbs_the_copy_added_before_it_was_known()
    await test_a_sweep_skips_the_check_of_an_entry_folded_in_meanwhile()
    await test_a_username_held_by_another_account_is_said_once()
    await test_one_id_is_never_stored_on_two_entries()
    await test_a_sweep_relinks_and_recovers_in_one_pass()

    test_the_state_line_is_green_only_for_an_account_that_answers()
    await test_status_says_which_failing_account_lost_its_username()
    await test_add_replies_with_the_relinked_entry()

    await dispose_engine()
    if DB_FILE.exists():
        DB_FILE.unlink()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("All rename-by-id tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
