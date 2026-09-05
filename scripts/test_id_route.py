"""Regression tests for the numeric-id route (2026-09-05).

Instagram's username-keyed profile API (web_profile_info) started answering
401 with a login wall for anonymous clients from every network measured —
residential ones included, even for @instagram — while the graphql reel query
BY NUMERIC ID kept answering through the Worker. Three sweeps in a row failed
every check, and a target renamed during the outage failed eight checks
without a word, because rename recovery waited for a 404 that a shut door
never sends.

What must hold:
- the throttle books the two doors separately: a shut username door closes
  THAT door for the sweep, while the gate is down only when nothing answers;
- a check asks by id first and a rename is persisted + announced even when
  the profile fetch after it is blocked;
- an id-only reading is live and PARTIAL: it carries the last full reading
  forward and alerts on nothing it did not see;
- a username 404 is surfaced, and worded by what the id route said;
- the sweep summary counts what was actually checked and answered.

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

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
DB_FILE = ROOT / "test_id_route.db"
if DB_FILE.exists():
    DB_FILE.unlink()
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.config import settings  # noqa: E402
from app.database import crud  # noqa: E402
from app.database.models import AccountSnapshot, Base, MonitoredAccount  # noqa: E402
from app.database.session import engine, get_session  # noqa: E402
from app.monitor import service as service_mod  # noqa: E402
from app.monitor.instagram import IdProbe, InstagramClient, ProfileFetchResult  # noqa: E402
from app.monitor.service import MonitorService, _SweepThrottle  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


# ---------- 1. the throttle knows two doors --------------------------------

def test_username_door_closes_while_the_id_route_answers() -> None:
    t = _SweepThrottle(base_stagger=0.0, max_stagger=0.0, breaker_threshold=3)
    for _ in range(3):
        t.record(401, id_status=200)
    expect("the username door closes at the threshold", t.username_door_closed)
    expect("but the gate is NOT down — Instagram answered by id", not t.gate_down)
    expect("and the breaker did not open", not t.is_open())
    expect("every check counted as answered", t.answered == 3, repr(t.answered))
    expect("no full-block streak was recorded", t.peak_consecutive_blocks == 0,
           repr(t.peak_consecutive_blocks))
    expect("the username-door streak is reported",
           t.peak_consecutive_user_blocks == 3, repr(t.peak_consecutive_user_blocks))


def test_one_username_success_keeps_the_door_open() -> None:
    t = _SweepThrottle(base_stagger=0.0, max_stagger=0.0, breaker_threshold=2)
    t.record(200, id_status=200)
    t.record(401, id_status=200)
    t.record(401, id_status=200)
    t.record(401, id_status=200)
    expect("a door that answered once is a throttle, not a shut door",
           not t.username_door_closed)


def test_gate_down_needs_both_routes_blocked() -> None:
    t = _SweepThrottle(base_stagger=0.0, max_stagger=0.0, breaker_threshold=2,
                       cooldown=90.0)
    t.record(401, id_status=401)
    t.record(401, id_status=401)
    expect("both routes blocked, nothing answered -> gate down", t.gate_down)
    expect("and the breaker is open", t.is_open())

    t2 = _SweepThrottle(base_stagger=0.0, max_stagger=0.0, breaker_threshold=2)
    t2.record(None, id_status=401)   # id-only check, blocked
    t2.record(None, id_status=401)
    expect("an id-only check that is blocked counts as a block", t2.gate_down)

    t3 = _SweepThrottle(base_stagger=0.0, max_stagger=0.0, breaker_threshold=2)
    t3.record(None, id_status=None)  # nothing was asked at all
    t3.record(None, id_status=None)
    expect("a check that asked nothing is neutral", not t3.is_open())

    t4 = _SweepThrottle(base_stagger=0.0, max_stagger=0.0, breaker_threshold=2)
    t4.record(401, id_status=404)
    t4.record(401, id_status=404)
    expect("a 404 by id is an answer, not a block", not t4.gate_down)


# ---------- 2. the check path -----------------------------------------------

class ScriptedInstagram:
    """fetch_profile and probe_by_id follow whatever the test scripts."""

    def __init__(self) -> None:
        self.profile: Callable[[str], ProfileFetchResult] = lambda u: ProfileFetchResult(
            username=u, http_status=401, error="HTTP 401"
        )
        self.probe: Callable[[str], IdProbe] = lambda i: IdProbe(user_id=i, status=401)
        self.profile_calls: list[str] = []
        self.probe_calls: list[str] = []

    async def fetch_profile(self, username: str, **kw) -> ProfileFetchResult:
        self.profile_calls.append(username)
        return self.profile(username)

    async def probe_by_id(self, user_id: str) -> IdProbe:
        self.probe_calls.append(str(user_id))
        return self.probe(str(user_id))

    async def fetch_reel_user(self, user_id: str):
        return None

    async def fetch_hd_pic_url(self, user_id: str):
        raise AssertionError("must not be called without a session cookie")


def _answered(user_id: str, username: str, *, story: bool = False) -> IdProbe:
    return IdProbe(
        user_id=user_id, status=200, username=username, profile_pic_url=None,
        reel_data={"has_public_story": story, "is_live": False, "highlights": {}},
    )


def _api(username: str, **overrides: Any) -> ProfileFetchResult:
    parsed = {
        "username": username, "full_name": "T", "biography": "hello",
        "followers_count": 100, "following_count": 5, "posts_count": 3,
        "reels_count": 0, "story_count": 0, "is_private": False,
        "is_verified": False, "is_business": False,
        "profile_pic_url": None, "external_url": None, "instagram_id": "42",
    }
    parsed.update(overrides)
    return ProfileFetchResult(
        username=username, http_status=200, parsed=parsed,
        raw_response={"data": {"user": {"id": "42"}}},
    )


def _service(instagram) -> MonitorService:
    notifier = AsyncMock()
    notifier.send_text = AsyncMock(return_value=True)
    notifier.send_document = AsyncMock(return_value=True)
    notifier.create_forum_topic = AsyncMock(return_value=None)
    return MonitorService(
        instagram=instagram,
        hasher=AsyncMock(hash_url=AsyncMock(return_value=None)),
        notifier=notifier, stories=None,
    )


def _sent(service) -> list[str]:
    return [c.args[0] for c in service.notifier.send_text.await_args_list]


async def _new_account(username: str, instagram_id: Optional[str] = "42") -> int:
    async with get_session() as session:
        account = MonitoredAccount(username=username, active=True, instagram_id=instagram_id)
        session.add(account)
        await session.flush()
        return account.id


async def _seed_api_snapshot(account_id: int, username: str) -> None:
    async with get_session() as session:
        session.add(AccountSnapshot(
            account_id=account_id, username=username, http_status=200,
            full_name="T", biography="hello", followers_count=100,
            following_count=5, posts_count=3, is_private=False,
            raw_response={"data": {"user": {"id": "42"}}},
        ))


async def test_rename_survives_a_blocked_profile_fetch() -> None:
    """The bug: a target renamed while the username route was shut. The id
    route says the new name; the profile fetch by that name is still blocked.
    The rename must be kept and announced anyway."""
    account_id = await _new_account("oldname")
    await _seed_api_snapshot(account_id, "oldname")
    ig = ScriptedInstagram()
    ig.probe = lambda i: _answered(i, "newname")
    service = _service(ig)

    result = await service.check_username("oldname")

    expect("the check is ok — the id route answered", result.get("ok") is True, repr(result))
    expect("it is marked as an id-only reading", result.get("partial") == "id_probe",
           repr(result.get("partial")))
    expect("the username route's answer is kept for the guard",
           result.get("status") == 401 and result.get("id_status") == 200, repr(result))
    expect("the id was asked first", ig.probe_calls == ["42"], repr(ig.probe_calls))
    expect("and the profile was fetched under the NEW name",
           ig.profile_calls == ["newname"], repr(ig.profile_calls))

    async with get_session() as session:
        account = await session.get(MonitoredAccount, account_id)
        latest = await crud.get_latest_snapshot(session, account_id)
        by_name = await crud.get_account(session, "newname")
    expect("the account row carries the new username", account.username == "newname",
           repr(account.username))
    expect("and is found by it", by_name is not None and by_name.id == account_id)
    expect("the check counted as a success", account.consecutive_failures == 0,
           repr(account.consecutive_failures))

    texts = _sent(service)
    renames = [t for t in texts if "changed username" in t]
    expect("the rename was announced", len(renames) == 1, repr(texts))
    expect("naming both handles", "@oldname" in renames[0] and "@newname" in renames[0],
           renames[0])
    expect("and nothing else was announced", len(texts) == 1, repr(texts))

    expect("an id-only snapshot was written", crud.snapshot_source(latest) == "id_probe",
           repr(crud.snapshot_source(latest)))
    expect("under the new username", latest.username == "newname", repr(latest.username))
    expect("carrying the last full reading forward, not blanks",
           latest.followers_count == 100 and latest.biography == "hello",
           repr((latest.followers_count, latest.biography)))
    expect("and the privacy flag too", latest.is_private is False, repr(latest.is_private))


async def test_an_unchanged_id_only_check_is_silent() -> None:
    account_id = await _new_account("steady")
    await _seed_api_snapshot(account_id, "steady")
    ig = ScriptedInstagram()
    ig.probe = lambda i: _answered(i, "steady")
    service = _service(ig)

    first = await service.check_username("steady")
    async with get_session() as session:
        rows_after_first = await crud.recent_snapshots(session, account_id, limit=10)
    second = await service.check_username("steady")
    async with get_session() as session:
        rows_after_second = await crud.recent_snapshots(session, account_id, limit=10)

    expect("both checks ok", first.get("ok") and second.get("ok"), repr((first, second)))
    expect("the first id-only reading baselines its own door",
           len(rows_after_first) == 2, repr(len(rows_after_first)))
    expect("the second writes nothing new", len(rows_after_second) == 2,
           repr(len(rows_after_second)))
    expect("neither says a word", _sent(service) == [], repr(_sent(service)))


async def test_the_api_coming_back_does_not_repeat_the_rename() -> None:
    """After the id route renamed the account, the API's own diff (against
    its last reading, under the old name) must not announce the rename a
    second time — but a real change it sees still reports."""
    account_id = await _new_account("before")
    await _seed_api_snapshot(account_id, "before")
    ig = ScriptedInstagram()
    ig.probe = lambda i: _answered(i, "after")
    service = _service(ig)

    await service.check_username("before")          # rename via the id route
    ig.profile = lambda u: _api(u, followers_count=101)
    result = await service.check_username("after")  # the API answers again

    expect("the full reading is ok and not partial",
           result.get("ok") is True and result.get("partial") is None, repr(result))
    texts = _sent(service)
    expect("exactly one rename message overall",
           sum("changed username" in t for t in texts) == 1, repr(texts))
    expect("the diff's own copy of the rename is dropped",
           not any("Username changed" in t for t in texts), repr(texts))
    expect("the follower change the API saw still reports",
           any("followers" in t for t in texts), repr(texts))


async def test_a_gone_id_is_announced_at_once() -> None:
    account_id = await _new_account("vanished")
    ig = ScriptedInstagram()
    ig.probe = lambda i: IdProbe(user_id=i, status=404)
    service = _service(ig)

    result = await service.check_username("vanished")

    expect("the check fails with a 404", result.get("ok") is False and result.get("status") == 404,
           repr(result))
    expect("the username route was not even asked", ig.profile_calls == [],
           repr(ig.profile_calls))
    texts = _sent(service)
    expect("said on the first miss — two routes agree", len(texts) == 1, repr(texts))
    expect("and worded as deactivated/deleted, not renamed",
           texts and "deactivated or deleted" in texts[0], repr(texts))
    async with get_session() as session:
        account = await session.get(MonitoredAccount, account_id)
    expect("bookkeeping ran", account.consecutive_failures == 1 and account.last_status_code == 404,
           repr((account.consecutive_failures, account.last_status_code)))


async def test_a_username_404_with_a_blocked_id_route_is_surfaced_on_the_second_miss() -> None:
    account_id = await _new_account("maybe_renamed")
    ig = ScriptedInstagram()
    ig.profile = lambda u: ProfileFetchResult(username=u, http_status=404, error="User not found")
    service = _service(ig)

    await service.check_username("maybe_renamed")
    expect("one 404 alone is still quiet (could be a flake)", _sent(service) == [],
           repr(_sent(service)))
    await service.check_username("maybe_renamed")
    texts = _sent(service)
    expect("the second consecutive 404 is announced", len(texts) == 1, repr(texts))
    expect("and says the id lookup was blocked",
           texts and "blocked" in texts[0] and "@maybe_renamed" in texts[0], repr(texts))
    async with get_session() as session:
        account = await session.get(MonitoredAccount, account_id)
        row = await crud.get_latest_snapshot(session, account_id, successful_only=False)
    expect("no snapshot rows for a 404 (a gate answer, not history)", row is None, repr(row))
    expect("failures counted", account.consecutive_failures == 2, repr(account.consecutive_failures))


async def test_both_routes_blocked_stays_quiet() -> None:
    account_id = await _new_account("dark")
    ig = ScriptedInstagram()   # profile 401, probe 401
    service = _service(ig)
    result = await service.check_username("dark")
    expect("a fully blocked check fails", result.get("ok") is False, repr(result))
    expect("with both statuses reported",
           result.get("status") == 401 and result.get("id_status") == 401, repr(result))
    expect("and no per-account alert (the sweep summary names the block)",
           _sent(service) == [], repr(_sent(service)))
    async with get_session() as session:
        account = await session.get(MonitoredAccount, account_id)
    expect("but the failure is booked", account.consecutive_failures == 1,
           repr(account.consecutive_failures))


async def test_id_only_with_no_id_is_deferred_not_failed() -> None:
    account_id = await _new_account("idless", instagram_id=None)
    ig = ScriptedInstagram()
    service = _service(ig)
    result = await service._run_check(account_id, "idless", id_only=True)
    expect("deferred, not failed", result.get("skipped") is True and result.get("ok") is False,
           repr(result))
    expect("nothing was asked", ig.profile_calls == [] and ig.probe_calls == [],
           repr((ig.profile_calls, ig.probe_calls)))
    async with get_session() as session:
        account = await session.get(MonitoredAccount, account_id)
    expect("and no bookkeeping pretends a check happened",
           account.last_checked_at is None and account.consecutive_failures == 0,
           repr((account.last_checked_at, account.consecutive_failures)))


# ---------- 3. the sweep --------------------------------------------------

async def _sweep_with(profile, probe, usernames: list[str]) -> tuple[dict, list[str], ScriptedInstagram]:
    """Run check_all over a fresh set of accounts (everything else paused)."""
    async with get_session() as session:
        for a in await crud.list_accounts(session, only_active=True):
            await crud.set_account_active(session, a.username, False)
    for i, u in enumerate(usernames):
        await _new_account(u, instagram_id=str(1000 + i))
    ig = ScriptedInstagram()
    ig.profile = profile
    ig.probe = probe
    service = _service(ig)
    result = await service.check_all()
    return result, _sent(service), ig


async def test_a_shut_username_door_does_not_stop_the_sweep() -> None:
    names = [f"door{i}" for i in range(6)]
    result, texts, ig = await _sweep_with(
        lambda u: ProfileFetchResult(username=u, http_status=401, error="HTTP 401"),
        lambda i: _answered(i, f"door{int(i) - 1000}"),
        names,
    )
    summary = texts[-1]
    expect("every account was checked", result["checked"] == 6, repr(result))
    expect("and every one answered by id", result["answered"] == 6 and result["id_only"] == 6,
           repr(result))
    expect("nothing failed, nothing deferred", result["failed"] == 0 and result["deferred"] == 0,
           repr(result))
    expect("the sweep did NOT stop", "Sweep stopped" not in summary, summary)
    expect("the summary says the checks were id-only",
           "checked by Instagram ID only" in summary, summary)
    expect("and that the username door was shut",
           "refused every username lookup" in summary, summary)
    expect("the username door was asked only until it closed",
           len(ig.profile_calls) == settings.sweep_breaker_threshold,
           f"{len(ig.profile_calls)} vs threshold {settings.sweep_breaker_threshold}")
    expect("every account was asked by id", len(ig.probe_calls) == 6, repr(ig.probe_calls))


async def test_a_shut_gate_is_counted_honestly() -> None:
    names = [f"gate{i}" for i in range(7)]
    result, texts, ig = await _sweep_with(
        lambda u: ProfileFetchResult(username=u, http_status=401, error="HTTP 401"),
        lambda i: IdProbe(user_id=i, status=401),
        names,
    )
    summary = texts[-1]
    threshold = settings.sweep_breaker_threshold
    expect("the sweep stopped", "Sweep stopped" in summary, summary)
    expect("only the pre-breaker accounts count as checked",
           result["checked"] == threshold, repr(result))
    expect("the rest are deferred, not failed",
           result["deferred"] == 7 - threshold and result["failed"] == threshold, repr(result))
    expect("nothing 'got through'", "did get through" not in summary, summary)
    expect("the deferred count is the real one",
           f"left {7 - threshold} account(s) unchecked" in summary, summary)


# ---------- 4. the client's probe -----------------------------------------

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


REEL = {"data": {"user": {
    "has_public_story": True, "is_live": False,
    "reel": {"id": "42", "user": {
        "id": "42", "username": "NewName",
        "profile_pic_url": "https://scontent.cdninstagram.com/v/t51.2885-19/1_2_3_n.jpg?stp=x",
    }},
    "edge_highlight_reels": {"edges": []},
}}}


async def test_probe_reports_what_instagram_said() -> None:
    old = settings.ig_proxy_url
    settings.ig_proxy_url = "https://ig-proxy.example.workers.dev"
    try:
        session = _MockSession(lambda url, p: _MockResponse(200, REEL))
        async with InstagramClient(max_retries=1, session=session) as client:
            probe = await client.probe_by_id("42")
        expect("a 200 answers", probe.answered and probe.status == 200, repr(probe))
        expect("with the normalised username", probe.username == "newname", repr(probe.username))
        expect("the avatar URL", (probe.profile_pic_url or "").endswith("1_2_3_n.jpg?stp=x"),
               repr(probe.profile_pic_url))
        expect("and the story flag", probe.reel_data == {
            "has_public_story": True, "is_live": False, "highlights": {}}, repr(probe.reel_data))
        expect("one request, through the worker",
               len(session.requests) == 1 and session.requests[0]["params"] == {"user_id": "42"},
               repr(session.requests))

        session = _MockSession(lambda url, p: _MockResponse(404, {}))
        async with InstagramClient(max_retries=1, session=session) as client:
            probe = await client.probe_by_id("42")
        expect("a 404 through the worker is final", probe.gone and len(session.requests) == 1,
               repr((probe, session.requests)))

        session = _MockSession(lambda url, p: _MockResponse(401, {}))
        async with InstagramClient(max_retries=1, session=session) as client:
            probe = await client.probe_by_id("42")
        expect("a 401 on both paths reads as blocked", probe.blocked and probe.status == 401,
               repr(probe))
        expect("the direct path was tried after the worker",
               len(session.requests) == 2, repr(session.requests))
    finally:
        settings.ig_proxy_url = old


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Keep the sweep tests fast and deterministic.
    service_mod._SWEEP_STAGGER_SECONDS = 0.0
    settings.sweep_stagger_max_seconds = 0
    settings.sweep_breaker_threshold = 3
    settings.sweep_breaker_cooldown_seconds = 0
    settings.sweep_retry_rounds = 0
    settings.sweep_concurrency = 1
    settings.dark_radar_days = 0

    test_username_door_closes_while_the_id_route_answers()
    test_one_username_success_keeps_the_door_open()
    test_gate_down_needs_both_routes_blocked()

    await test_rename_survives_a_blocked_profile_fetch()
    await test_an_unchanged_id_only_check_is_silent()
    await test_the_api_coming_back_does_not_repeat_the_rename()
    await test_a_gone_id_is_announced_at_once()
    await test_a_username_404_with_a_blocked_id_route_is_surfaced_on_the_second_miss()
    await test_both_routes_blocked_stays_quiet()
    await test_id_only_with_no_id_is_deferred_not_failed()

    await test_a_shut_username_door_does_not_stop_the_sweep()
    await test_a_shut_gate_is_counted_honestly()

    await test_probe_reports_what_instagram_said()

    await engine.dispose()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All id-route tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
