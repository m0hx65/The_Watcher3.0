"""Instagram's "isn't available" page is an answer, not a block.

2026-11-10, 13:35 UTC: @8dvuu, a target that had answered every sweep, came
back as "served via the page tunnel (514144 bytes) but carried no profile
payload — login wall or markup change", and again on the retry. The other 16
accounts answered through the same exit. Fetched from a home connection that
evening, @8dvuu and a made-up username got the same 516 KB page with HTTP 200:
Instagram's own "Profile isn't available" page. The account was gone, and the
bot was reporting a block — every sweep, with no word about why, and with each
try counted against the page door.

The page used to come with a 404, which the bot already read as "the page's
own answer". The fixtures below are trimmed from the pages captured that day.

What must hold:
- the "isn't available" page is told from a profile, from the login page and
  from a profile Instagram shows only to logged-in visitors;
- for one username it is handed up as a 404 — through this host's door and
  the home fetcher's — and is no strike against the page door;
- for one username after another, with no profile between them, it is not a
  verdict on any of them (the username API answered 404 for everyone on
  2026-11-04, and six live accounts were reported missing);
- the check reports the account as not found on the second miss running,
  counted in "not found" answers, not in failed checks.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "home_fetcher"))

DB_FILE = ROOT / "test_page_unavailable.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")
os.environ["HOME_FETCH_ENGINE"] = "stdlib"

import home_fetcher as worker  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import crud  # noqa: E402
from app.database.models import Base, MonitoredAccount  # noqa: E402
from app.database.session import dispose_engine, engine, get_session  # noqa: E402
from app.monitor import home_fetch  # noqa: E402
from app.monitor.instagram import IdProbe, InstagramClient, ProfileFetchResult  # noqa: E402
from app.monitor.public_page import is_unavailable_page, parse_public_profile  # noqa: E402
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


# Every page carries this table, whatever it is a page of. It names the error
# route too, and must not be what the detector reads.
QPL_TABLE = (
    '"553648129.polaris.feedPage":455.4,"553648129.polaris.httpErrorPage":28.2,'
    '"553648129.polaris.loginPage":46.4,"553648129.polaris.profilePage":446.5'
)


def _html(route: str, *, body: str = "", padding: int = 0) -> str:
    return (
        '<!DOCTYPE html><html class="_9dls _ar44" lang="en" dir="ltr"><head>'
        "<title>Instagram</title></head><body>"
        f'<script type="application/json">{{"qpl":{{{QPL_TABLE}}}}}</script>'
        + ("<!-- pad -->" * padding)
        + f'<script type="application/json" data-sjs>{{{route}}}</script>{body}'
        "</body></html>"
    )


def unavailable_page(username: str, *, padding: int = 0) -> str:
    """The route block of the page Instagram served for a username with no
    profile, as captured (2026-11-10) — with HTTP 200."""
    view = (
        '{"allResources":[{"__dr":"PolarisErrorRoot.react"},'
        '{"__dr":"PolarisErrorRoot.entrypoint"}],"resource":{"__dr":"PolarisErrorRoot.react"},'
        '"props":{"page_logging":{"name":"httpErrorPage","params":{}},"failure_reason":null,'
        '"restricted_age":null,"page_type":"PROFILE","show_lox_redesigned_404_page":true},'
        '"entryPoint":{"__dr":"PolarisErrorRoot.entrypoint"}}'
    )
    route = (
        f'"initialRouteInfo":{{"route":{{"actorID":"0","rootView":{view},'
        '"tracePolicy":"polaris.httpErrorPage","meta":{"title":null,"accessory":null,'
        '"favicon":null},"prefetchable":true,'
        f'"canonicalRouteName":"comet.igweb.PolarisErrorRoute","hostableView":{view},'
        '"polarisRouteConfig":{"pageID":"httpErrorPage"},'
        f'"url":"\\/{username}\\/","params":{{}},"routePath":null}},"timeSpentMetaData":null}}'
    )
    return _html(route, padding=padding)


def profile_page(username: str, *, followers: int = 127) -> str:
    route = (
        '"initialRouteInfo":{"route":{"actorID":"0","rootView":{"allResources":['
        '{"__dr":"PolarisProfilePostsTabRoot.react"},'
        '{"__dr":"PolarisLoggedOutDesktopWWWProfileRoot.react"}]},'
        '"polarisRouteConfig":{"topNavPrimaryCTA":"login","topNavSecondaryCTA":"signup",'
        f'"pageID":"profilePage"}},"url":"\\/{username}\\/"}}}}'
    )
    payload = (
        '<script type="application/json" data-sjs>{"require":[["RelayPrefetchedStreamCache",'
        '"next",[],[{"__bbox":{"result":{"data":{"xig_user_by_username":'
        f'{{"pk":"528817151","username":"{username}","is_private":true,"biography":"b",'
        f'"full_name":"N","is_verified":false,"follower_count":{followers},'
        '"following_count":273,"all_media_count":null,"id":"17841400000000000"}'
        "}}}}]]]}</script>"
    )
    return _html(route, body=payload)


# The page behind a redirect to /accounts/login/ — the refusal Instagram gives
# an IP it does not trust.
LOGIN_PAGE = _html(
    '"initialRouteInfo":{"route":{"rootView":{"resource":{"__dr":"CAAIGLoginHomepageRoot.react"}},'
    '"polarisRouteConfig":{"pageID":"CAAIGLoginHomepage"},"url":"\\/accounts\\/login\\/"}}'
)

# A profile Instagram shows only to logged-in visitors (an age-restricted
# brand account, captured 2026-11-10): the profile route, and no profile.
RESTRICTED_PAGE = _html(
    '"initialRouteInfo":{"route":{"polarisRouteConfig":{"topNavPrimaryCTA":"login",'
    '"topNavSecondaryCTA":"signup","pageID":"profilePage"},"url":"\\/heineken\\/"}}',
    body=(
        '<script type="application/json" data-sjs>{"data":{"xig_user_by_username":null}}</script>'
        '<script type="application/json" data-sjs>{"data":{"xig_user_by_username":null}}</script>'
    ),
)


class _Response:
    def __init__(self, status_code: int, text: str = "", url: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self.url = url

    def json(self) -> Any:
        return {}


class _Session:
    """Answers the profile page per username; refuses everything else (the
    username API, the Worker's id route and the reel query, as since 11-04)."""

    def __init__(self, pages: Callable[[str], _Response]) -> None:
        self.pages = pages
        self.urls: list[str] = []

    async def get(self, url: str, *, params: Any = None, headers: Any = None, **kw):
        self.urls.append(url)
        prefix = "https://www.instagram.com/"
        if url.startswith(prefix) and "/api/" not in url and "graphql" not in url:
            return self.pages(url[len(prefix):].strip("/"))
        return _Response(401)

    async def close(self) -> None:
        pass


def _gone(name: str) -> _Response:
    return _Response(200, unavailable_page(name), f"https://www.instagram.com/{name}/")


# ---------- the page itself --------------------------------------------------------

def test_the_page_is_told_from_the_others() -> None:
    gone = unavailable_page("8dvuu")
    expect("Instagram's “isn't available” page is recognised", is_unavailable_page(gone))
    expect("and carries no profile", parse_public_profile(gone, "8dvuu") is None)
    profile = profile_page("nasa")
    expect("a profile's page is not it", not is_unavailable_page(profile))
    expect("(and still parses)", (parse_public_profile(profile, "nasa") or {}).get("followers_count") == 127)
    expect("the login page is not it", not is_unavailable_page(LOGIN_PAGE))
    expect("a profile shown only to logged-in visitors is not it",
           not is_unavailable_page(RESTRICTED_PAGE)
           and parse_public_profile(RESTRICTED_PAGE, "heineken") is None)
    expect("an empty page is not it", not is_unavailable_page(""))
    only_route_name = _html('"route":{"canonicalRouteName":"comet.igweb.PolarisErrorRoute"}')
    expect("either name of the error route is enough", is_unavailable_page(only_route_name))
    reordered = _html('"polarisRouteConfig":{"topNav":"login","pageID":"httpErrorPage"}')
    expect("whatever else its route config carries", is_unavailable_page(reordered))


# ---------- this host's door -------------------------------------------------------

async def test_one_missing_username_is_a_404_not_a_refusal() -> None:
    session = _Session(_gone)
    async with InstagramClient(max_retries=2, session=session) as client:
        outcome = await client.probe_public_page("8dvuu", allow_home=False)
        expect("the page is handed on as the 404 it used to be", outcome.get("status") == 404,
               repr(outcome))
        expect("saying what it is", "isn't available" in str(outcome.get("error")),
               repr(outcome.get("error")))
        expect("it is no strike against the page door",
               client.direct_page_failures == 0 and not client.direct_page_door_failing,
               repr(client.direct_page_failures))

        result = await client.fetch_profile("8dvuu", api=False)
        expect("the profile fetch reports not found, from the page",
               result.http_status == 404 and not result.success
               and result.source == "public_page", repr(result))

        for _ in range(4):
            await client.probe_public_page("8dvuu", allow_home=False)
        expect("asked again and again, the same username stays a verdict and never a strike",
               client.direct_page_failures == 0
               and (await client.fetch_profile("8dvuu", api=False)).http_status == 404,
               repr(client.direct_page_failures))


async def test_the_refusals_are_still_refusals() -> None:
    pages = {
        "walled": _Response(200, LOGIN_PAGE, "https://www.instagram.com/accounts/login/?next=%2Fwalled%2F"),
        "heineken": _Response(200, RESTRICTED_PAGE, "https://www.instagram.com/heineken/"),
    }
    session = _Session(lambda name: pages[name])
    async with InstagramClient(max_retries=2, session=session) as client:
        outcome = await client.probe_public_page("walled", allow_home=False)
        expect("a redirect to the login page is still that",
               outcome.get("status") == 200 and outcome.get("error") == "redirected to the login page",
               repr(outcome.get("error")))
        outcome = await client.probe_public_page("heineken", allow_home=False)
        expect("a page with no profile for a logged-out visitor is still no payload",
               outcome.get("status") == 200
               and outcome.get("error") == "no profile payload in the page", repr(outcome.get("error")))
        expect("and both count against the door", client.direct_page_failures == 2,
               repr(client.direct_page_failures))


async def test_the_same_answer_for_everyone_is_not_a_verdict() -> None:
    answers: dict[str, Callable[[str], _Response]] = {}

    def pages(name: str) -> _Response:
        return answers.get(name, _gone)(name)

    session = _Session(pages)
    async with InstagramClient(max_retries=2, session=session) as client:
        async def ask(name: str) -> dict:
            return await client.probe_public_page(name, allow_home=False, force_direct=True)

        first = [await ask(name) for name in ("one", "two", "three")]
        expect("up to three different usernames in a row are taken at Instagram's word",
               [o.get("status") for o in first] == [404, 404, 404], repr([o.get("status") for o in first]))
        fourth = await ask("four")
        expect("a fourth, with no profile read between them, is not",
               fourth.get("status") == 200 and "not a verdict" in str(fourth.get("error")),
               repr(fourth))
        expect("and counts as the refusal it then is", client.direct_page_failures == 1,
               repr(client.direct_page_failures))
        again = await ask("one")
        expect("nor are the first ones when asked again — their second miss must not alert",
               again.get("status") == 200 and "not a verdict" in str(again.get("error")), repr(again))
        result = await client.fetch_profile("two", api=False)
        expect("so the check reads as blocked, not as not found", result.http_status == 401,
               repr((result.http_status, result.error)))

        answers["alive"] = lambda name: _Response(
            200, profile_page(name), f"https://www.instagram.com/{name}/"
        )
        alive = await ask("alive")
        expect("a page that carries a profile ends the run", alive.get("parsed") is not None, repr(alive))
        after = await ask("one")
        expect("and the next missing username is a verdict again", after.get("status") == 404,
               repr(after))


# ---------- the home fetcher's door ------------------------------------------------

class _Broker:
    connected = True
    outdated = False

    def __init__(self, pages: dict) -> None:
        self.pages = pages

    def describe(self) -> str:
        return "connected (worker fake)"

    def cached(self, username: str):
        return None

    def cached_reel(self, user_id: str):
        return None

    async def request_page(self, username: str, *, timeout: float = 30.0, fresh: bool = False):
        return self.pages.get(username)


async def test_the_home_fetcher_says_it_too() -> None:
    page = unavailable_page("8dvuu", padding=8000)  # the route block sits far into the page
    raw = page.encode("utf-8")
    at = raw.find(b'"pageID":"httpErrorPage"')
    expect("(the fixture's route block is past the worker's 64 KB cut)",
           at > worker.NO_PAYLOAD_BODY_LIMIT, repr(at))
    sent = worker.no_payload_body(raw)
    expect("the worker sends the page's head and the block that says what it is",
           len(sent) < worker.NO_PAYLOAD_BODY_LIMIT + 4096
           and is_unavailable_page(sent.decode("utf-8", "replace")), repr(len(sent)))
    expect("which the head alone does not",
           not is_unavailable_page(raw[:worker.NO_PAYLOAD_BODY_LIMIT].decode("utf-8", "replace")))
    plain = LOGIN_PAGE.encode("utf-8") + b" " * (worker.NO_PAYLOAD_BODY_LIMIT + 10)
    expect("any other page without a payload is cut at the limit, as before",
           worker.no_payload_body(plain) == plain[:worker.NO_PAYLOAD_BODY_LIMIT])

    old_token, old_broker = settings.home_fetch_token, home_fetch.broker
    settings.home_fetch_token = "sekrit"
    home_fetch.broker = _Broker({
        "8dvuu": home_fetch.PageResult(200, sent.decode("utf-8", "replace"),
                                       "https://www.instagram.com/8dvuu/"),
    })
    try:
        # This host's door is refused (a 429), so the home fetcher is asked.
        session = _Session(lambda name: _Response(429))
        async with InstagramClient(max_retries=2, session=session) as client:
            outcome = await client.probe_home_page("8dvuu")
            expect("the page the phone fetched is read as a 404 as well",
                   outcome.get("status") == 404 and outcome.get("door") == "home", repr(outcome))
            result = await client.fetch_profile("8dvuu", api=False)
            expect("and the profile fetch reports not found through that door",
                   result.http_status == 404 and result.source == "public_page", repr(result))
    finally:
        settings.home_fetch_token, home_fetch.broker = old_token, old_broker


# ---------- the check, and what the chat is told -----------------------------------

def _service(instagram: Any = None) -> MonitorService:
    notifier = AsyncMock()
    notifier.send_text = AsyncMock(return_value=True)
    notifier.create_forum_topic = AsyncMock(return_value=None)
    return MonitorService(
        instagram=instagram or AsyncMock(), hasher=AsyncMock(), notifier=notifier, stories=None,
    )


def _alerts(service: MonitorService) -> list[str]:
    return [c.args[0] for c in service.notifier.send_text.await_args_list]


async def _account(username: str, instagram_id: Optional[str] = "7880052534") -> int:
    async with get_session() as session:
        account = MonitoredAccount(username=username, active=True, instagram_id=instagram_id)
        session.add(account)
        await session.flush()
        return account.id


GONE = ProfileFetchResult(
    username="x", http_status=404, error="User not found (public page)", source="public_page",
)
BLOCKED = ProfileFetchResult(username="x", http_status=401, error="blocked")
ID_BLOCKED = IdProbe(user_id="7880052534", status=401)


async def test_not_found_is_counted_in_not_found_answers() -> None:
    """@8dvuu had failed twice as a block before its page was first read as
    "not found". Counted in failed checks, its alert would have waited for the
    fifth failure and called it "5 checks in a row"."""
    service = _service()
    account_id = await _account("blockedthengone")
    for _ in range(2):
        await service._handle_failure(account_id, "blockedthengone", BLOCKED, id_probe=ID_BLOCKED)
    await service._handle_failure(account_id, "blockedthengone", GONE, id_probe=ID_BLOCKED)
    expect("the first “not found” after two blocks is still quiet (it can be a flake)",
           _alerts(service) == [], repr(_alerts(service)))
    await service._handle_failure(account_id, "blockedthengone", GONE, id_probe=ID_BLOCKED)
    texts = _alerts(service)
    expect("the second one running is announced", len(texts) == 1, repr(texts))
    expect("as two checks, not as the four that failed",
           "doesn't exist (2 checks in a row)" in texts[0], texts[0] if texts else "")
    expect("saying the rename could not be checked",
           "ID lookup was blocked" in texts[0], texts[0] if texts else "")
    async with get_session() as session:
        account = await session.get(MonitoredAccount, account_id)
        expect("the card's count is still every failed check",
               account.consecutive_failures == 4 and account.last_status_code == 404,
               repr((account.consecutive_failures, account.last_status_code)))
    for _ in range(2):
        await service._handle_failure(account_id, "blockedthengone", GONE, id_probe=ID_BLOCKED)
    expect("no reminder at the fifth failed check, which is only the third miss",
           len(_alerts(service)) == 1, repr(_alerts(service)))
    await service._handle_failure(account_id, "blockedthengone", GONE, id_probe=ID_BLOCKED)
    texts = _alerts(service)
    expect("the reminder comes at the fifth miss", len(texts) == 2 and "5 checks in a row" in texts[-1],
           repr(texts))


async def test_a_block_between_two_misses_starts_the_count_over() -> None:
    service = _service()
    account_id = await _account("flaky")
    await service._handle_failure(account_id, "flaky", BLOCKED, id_probe=ID_BLOCKED)
    await service._handle_failure(account_id, "flaky", GONE, id_probe=ID_BLOCKED)
    expect("one block and then one miss is not two misses (it used to alert here)",
           _alerts(service) == [], repr(_alerts(service)))
    await service._handle_failure(account_id, "flaky", BLOCKED, id_probe=ID_BLOCKED)
    await service._handle_failure(account_id, "flaky", GONE, id_probe=ID_BLOCKED)
    expect("a miss, a block, a miss: still no two in a row", _alerts(service) == [],
           repr(_alerts(service)))
    await service._handle_failure(account_id, "flaky", GONE, id_probe=ID_BLOCKED)
    expect("until two misses do follow each other", len(_alerts(service)) == 1, repr(_alerts(service)))

    restarted = _service()  # the count is stored, not held in memory
    await restarted._handle_failure(account_id, "flaky", GONE, id_probe=ID_BLOCKED)
    expect("a restart does not announce the same run again", _alerts(restarted) == [],
           repr(_alerts(restarted)))


async def test_a_run_announced_before_the_count_existed_is_not_announced_again() -> None:
    """An account already reported missing when this shipped has an alert on
    record and no run count. Its next miss must carry on, not start a run of
    one that is announced again at two."""
    service = _service()
    account_id = await _account("alreadyreported")
    async with get_session() as session:
        account = await session.get(MonitoredAccount, account_id)
        account.consecutive_failures, account.last_status_code = 6, 404
        await crud.set_setting(session, service._not_found_key(account_id), "2026-11-09T00:00:00+00:00")
    for _ in range(3):  # failures 7, 8, 9
        await service._handle_failure(account_id, "alreadyreported", GONE, id_probe=ID_BLOCKED)
    expect("the misses after it are not announced as a new run", _alerts(service) == [],
           repr(_alerts(service)))
    await service._handle_failure(account_id, "alreadyreported", GONE, id_probe=ID_BLOCKED)
    texts = _alerts(service)
    expect("and its reminder comes where it would have, at the tenth",
           len(texts) == 1 and "10 checks in a row" in texts[0], repr(texts))


async def test_a_sweep_check_of_a_missing_account_says_so() -> None:
    """The whole path, as on 2026-11-10: the username API is known shut, the
    id route is refused, and the page is Instagram's “isn't available” page."""
    session = _Session(_gone)
    old_proxy = settings.ig_proxy_url
    settings.ig_proxy_url = "https://ig-proxy.example.workers.dev"
    try:
        async with InstagramClient(max_retries=2, session=session) as client:
            service = _service(client)
            account_id = await _account("8dvuu")
            first = await service._run_check(
                account_id, "8dvuu", thorough=False, skip_username_api=True, paced=True,
            )
            expect("the check fails as not found, not as blocked",
                   first.get("ok") is False and first.get("status") == 404, repr(first))
            expect("its id route's refusal is on record too", first.get("id_status") in (401, 403),
                   repr(first.get("id_status")))
            expect("one miss is not announced", _alerts(service) == [], repr(_alerts(service)))
            second = await service._run_check(
                account_id, "8dvuu", thorough=False, skip_username_api=True, paced=True,
            )
            texts = _alerts(service)
            expect("the second check announces it",
                   second.get("status") == 404 and len(texts) == 1
                   and "@8dvuu" in texts[0] and "doesn't exist" in texts[0], repr(texts))
            expect("and the page door has no strike from either",
                   client.direct_page_failures == 0, repr(client.direct_page_failures))
            async with get_session() as session_:
                marker = await crud.get_setting(session_, service._not_found_key(account_id))
            expect("the alert is on record, to be corrected if the account answers again",
                   marker is not None)
    finally:
        settings.ig_proxy_url = old_proxy


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    test_the_page_is_told_from_the_others()
    await test_one_missing_username_is_a_404_not_a_refusal()
    await test_the_refusals_are_still_refusals()
    await test_the_same_answer_for_everyone_is_not_a_verdict()
    await test_the_home_fetcher_says_it_too()
    await test_not_found_is_counted_in_not_found_answers()
    await test_a_block_between_two_misses_starts_the_count_over()
    await test_a_run_announced_before_the_count_existed_is_not_announced_again()
    await test_a_sweep_check_of_a_missing_account_says_so()

    await dispose_engine()
    if DB_FILE.exists():
        DB_FILE.unlink()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("All page-unavailable tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
