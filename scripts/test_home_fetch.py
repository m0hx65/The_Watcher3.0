"""Regression tests for the home fetcher's job broker and its two endpoints.

The owner's phone (or PC) polls the bot for profile pages to fetch and posts
the HTML back — no tunnel, no inbound port, because the home line is behind
carrier-grade NAT and an unrooted phone can run neither a port forward nor a
Tailscale Funnel.

What must hold:
- a check asks the broker and gets Instagram's answer back, matched by job id;
- with no worker polling, the broker answers "not connected" at once — a
  phone that is off must never stall a sweep;
- a worker that takes a job and never answers costs the check its timeout,
  nothing more, and a late answer is refused rather than mis-delivered;
- a job whose check already gave up is never handed to the worker;
- the endpoints refuse a missing/wrong token and say so when the door is
  disabled, and a gzip-compressed delivery is decoded.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import gzip
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from app.api.routes import router  # noqa: E402
from app.config import settings  # noqa: E402
from app.monitor import home_fetch  # noqa: E402
from app.monitor.home_fetch import HomeFetchBroker, PageResult  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


# ---------- 1. the broker ----------------------------------------------------

async def test_not_connected_answers_at_once() -> None:
    broker = HomeFetchBroker()
    started = time.monotonic()
    result = await broker.request_page("anyone", timeout=5.0)
    expect("no worker -> None", result is None)
    expect("and it does not wait", time.monotonic() - started < 0.2)
    expect("described as never polled", "no worker has ever polled" in broker.describe(),
           broker.describe())


async def test_round_trip() -> None:
    broker = HomeFetchBroker()

    async def worker() -> None:
        job = await broker.next_job(wait=5.0, worker="xiaomi")
        assert job is not None and job.username == "target"
        await asyncio.sleep(0.05)  # "fetching"
        ok = broker.deliver(job.id, PageResult(200, "<html>page</html>", "https://www.instagram.com/target/"))
        expect("the delivery is accepted", ok)

    # The worker must have polled once to count as connected; start it, give
    # it a moment to register, then ask.
    task = asyncio.create_task(worker())
    await asyncio.sleep(0.02)
    expect("a polling worker counts as connected", broker.connected, broker.describe())
    result = await broker.request_page("target", timeout=5.0)
    await task
    expect("the check gets Instagram's answer", result is not None and result.status == 200
           and result.body == "<html>page</html>", repr(result))
    expect("delivered count", broker.delivered == 1, repr(broker.delivered))
    expect("described as connected, by name", "connected (worker xiaomi" in broker.describe(),
           broker.describe())


async def test_a_silent_worker_costs_only_the_timeout() -> None:
    broker = HomeFetchBroker()

    async def worker() -> None:
        await broker.next_job(wait=5.0, worker="phone")  # takes it, never answers

    task = asyncio.create_task(worker())
    await asyncio.sleep(0.02)
    started = time.monotonic()
    result = await broker.request_page("slow", timeout=0.3)
    elapsed = time.monotonic() - started
    await task
    expect("no answer -> None", result is None)
    expect("after roughly the timeout", 0.25 <= elapsed < 1.5, f"{elapsed:.2f}s")
    expect("counted as timed out", broker.timed_out == 1)
    expect("a late answer is refused", not broker.deliver("nonexistent", PageResult(200, "x")))


async def test_an_abandoned_job_is_not_handed_out() -> None:
    broker = HomeFetchBroker()
    # A worker polls, then a check asks and gives up before the worker polls
    # again; the next poll must not receive the stale job.
    first = await broker.next_job(wait=0.05, worker="phone")
    expect("idle poll returns nothing", first is None)
    result = await broker.request_page("gone", timeout=0.05)
    expect("the check gave up", result is None)
    stale = await broker.next_job(wait=0.2, worker="phone")
    expect("the abandoned job is dropped, not handed out", stale is None, repr(stale))


def test_low_battery_alerts_once_and_rearms() -> None:
    """The phone lives on a charger. A reading that drops while NOT charging
    means the charger fell out: one alert at the threshold, one at half of
    it, then "charging again" — never one message per poll."""
    broker = HomeFetchBroker()
    broker._worker = "xiaomi"
    note = lambda b, c: broker.note_device(battery=b, charging=c, threshold=20)  # noqa: E731
    expect("charging at 68% says nothing", note(68, True) is None)
    expect("25% not charging is above the line", note(25, False) is None)
    first = note(20, False)
    expect("20% not charging alerts", first is not None and "20%" in first and "xiaomi" in first, repr(first))
    expect("18% does not alert again", note(18, False) is None)
    expect("12% does not alert again", note(12, False) is None)
    second = note(10, False)
    expect("10% (half the threshold) alerts once more", second is not None and "10%" in second, repr(second))
    expect("9% is silent", note(9, False) is None)
    back = note(30, True)
    expect("charging again is announced, once", back is not None and "charging again" in back, repr(back))
    expect("and only because an alert had gone out", note(31, True) is None)
    expect("a later drop alerts again", note(19, False) is not None)
    broker._last_poll = time.monotonic()  # the reading arrives with a poll
    expect("the battery shows in describe()", "battery 19%, not charging" in broker.describe(),
           broker.describe())
    expect("threshold 0 disables the alert",
           HomeFetchBroker().note_device(battery=5, charging=False, threshold=0) is None)
    expect("an unknown charging state below the line stays quiet (no false alarm)",
           HomeFetchBroker().note_device(battery=5, charging=None, threshold=20) is None)


# ---------- 2. the endpoints ------------------------------------------------

def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


async def test_endpoints() -> None:
    old_token = settings.home_fetch_token
    old_broker = home_fetch.broker
    home_fetch.broker = HomeFetchBroker()
    transport = httpx.ASGITransport(app=_app())
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://bot") as client:
            settings.home_fetch_token = None
            r = await client.get("/home-fetch/jobs?wait=0.1", headers={"X-Watcher-Token": "t"})
            expect("door disabled -> 404", r.status_code == 404, repr(r.status_code))

            settings.home_fetch_token = "sekrit"
            r = await client.get("/home-fetch/jobs?wait=0.1")
            expect("missing token -> 401", r.status_code == 401, repr(r.status_code))
            r = await client.get("/home-fetch/jobs?wait=0.1", headers={"X-Watcher-Token": "wrong"})
            expect("wrong token -> 401", r.status_code == 401, repr(r.status_code))

            r = await client.get("/home-fetch/jobs?wait=0.1",
                                 headers={"X-Watcher-Token": "sekrit", "X-Watcher-Worker": "xiaomi"})
            expect("idle poll -> no job", r.status_code == 200 and r.json() == {"job": None}, r.text)
            expect("the poll registered the worker", home_fetch.broker.connected,
                   home_fetch.broker.describe())

            # Full round trip through HTTP: a check asks while the worker polls.
            async def worker() -> None:
                poll = await client.get("/home-fetch/jobs?wait=5",
                                        headers={"X-Watcher-Token": "sekrit", "X-Watcher-Worker": "xiaomi"})
                job = poll.json()["job"]
                assert job and job["username"] == "target", poll.text
                delivery = await client.post(
                    f"/home-fetch/jobs/{job['id']}",
                    content=gzip.compress(b"<html>from the phone</html>"),
                    headers={
                        "X-Watcher-Token": "sekrit", "X-Watcher-Worker": "xiaomi",
                        "Content-Encoding": "gzip", "Content-Type": "text/html",
                        "X-IG-Status": "200",
                        "X-IG-Final-Url": "https://www.instagram.com/target/",
                    },
                )
                expect("the delivery is accepted over HTTP",
                       delivery.status_code == 200 and delivery.json() == {"ok": True}, delivery.text)

            task = asyncio.create_task(worker())
            await asyncio.sleep(0.05)
            result = await home_fetch.broker.request_page("target", timeout=5.0)
            await task
            expect("the check received the gunzipped page",
                   result is not None and result.status == 200
                   and result.body == "<html>from the phone</html>"
                   and result.final_url.endswith("/target/"), repr(result))

            r = await client.post("/home-fetch/jobs/nobody-waits", content=b"x",
                                  headers={"X-Watcher-Token": "sekrit", "X-IG-Status": "200"})
            expect("a delivery nobody waits for is reported as such",
                   r.status_code == 200 and r.json() == {"ok": False}, r.text)

            # The battery rides along with the poll and a low reading reaches
            # the owner through the monitor's notifier.
            from types import SimpleNamespace
            from unittest.mock import AsyncMock
            notifier = SimpleNamespace(send_text=AsyncMock(return_value=True))
            transport.app.state.monitor = SimpleNamespace(notifier=notifier)
            settings.home_fetch_low_battery_percent = 20
            r = await client.get("/home-fetch/jobs?wait=0.1", headers={
                "X-Watcher-Token": "sekrit", "X-Watcher-Worker": "xiaomi",
                "X-Watcher-Battery": "15", "X-Watcher-Charging": "no",
            })
            await asyncio.sleep(0.05)
            expect("the poll still answers", r.status_code == 200, r.text)
            expect("the battery is recorded", home_fetch.broker.battery == 15
                   and home_fetch.broker.charging is False, home_fetch.broker.describe())
            expect("and the owner is alerted once", notifier.send_text.await_count == 1
                   and "15%" in notifier.send_text.await_args.args[0],
                   repr(notifier.send_text.await_args_list))
            r = await client.get("/home-fetch/jobs?wait=0.1", headers={
                "X-Watcher-Token": "sekrit", "X-Watcher-Worker": "xiaomi",
                "X-Watcher-Battery": "14", "X-Watcher-Charging": "no",
            })
            await asyncio.sleep(0.05)
            expect("the next poll does not alert again", notifier.send_text.await_count == 1)
    finally:
        settings.home_fetch_token = old_token
        home_fetch.broker = old_broker


async def main() -> int:
    await test_not_connected_answers_at_once()
    await test_round_trip()
    await test_a_silent_worker_costs_only_the_timeout()
    await test_an_abandoned_job_is_not_handed_out()
    test_low_battery_alerts_once_and_rearms()
    await test_endpoints()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All home-fetch tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
