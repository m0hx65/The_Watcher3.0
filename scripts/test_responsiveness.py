"""The bot answers a tap before the network does, and Render never puts it to sleep.

1. The account card (open, recheck, pause/resume) used to wait for the live
   story check (the Worker, the direct route, then saveinsta's three round
   trips) before drawing anything: seconds on a normal day, minutes on a bad
   one. It now draws at once with "Story: ⏳ checking…" and fills the line in
   when the check answers, unless the user has already moved on.
2. Render's free plan sleeps after 15 minutes without an inbound request and
   the next tap waits about a minute for the boot. The scheduler now asks the
   bot's own public /health often enough that it never does.

Runs offline on sqlite with fakes — no Telegram, no network.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_responsiveness.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.bot import handlers  # noqa: E402
from app.config import settings  # noqa: E402
from app.database.models import AccountSnapshot, Base, MonitoredAccount  # noqa: E402
from app.database.session import engine, get_session  # noqa: E402
from app.utils import tasks  # noqa: E402
from app.workers import scheduler as scheduler_module  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


async def _account(username: str, *, private: bool) -> None:
    async with get_session() as session:
        account = MonitoredAccount(username=username, active=True, instagram_id="77")
        session.add(account)
        await session.flush()
        session.add(AccountSnapshot(
            account_id=account.id, username=username, http_status=200,
            followers_count=10, is_private=private,
        ))


def _tap(service, message_id: int):
    query = SimpleNamespace(
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(message_id=message_id, chat_id=7),
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"monitor": service}),
        bot=SimpleNamespace(edit_message_text=AsyncMock()),
    )
    return query, context


def _service(reel):
    return SimpleNamespace(
        instagram=SimpleNamespace(fetch_reel_user=reel), stories=None
    )


async def _settle() -> None:
    for _ in range(200):
        if tasks.pending() == 0:
            return
        await asyncio.sleep(0.01)


async def test_card_draws_before_the_live_check_answers() -> None:
    await _account("fastcard", private=False)
    answer = asyncio.Event()

    async def slow_reel(user_id):
        await answer.wait()
        return {"has_public_story": True, "is_live": False}

    query, context = _tap(_service(slow_reel), 101)
    shown = await asyncio.wait_for(
        handlers._show_account_card(query, context, "fastcard"), timeout=2
    )
    first = query.edit_message_text.await_args.kwargs["text"]
    expect("the card is on screen while the live check is still out",
           shown is True and query.edit_message_text.await_count == 1)
    expect("with the story line marked as being checked",
           f"Story: {handlers.STORY_CHECKING}" in first, first)
    expect("and nothing filled in yet", context.bot.edit_message_text.await_count == 0)

    answer.set()
    await _settle()
    expect("the answer fills the line in place",
           context.bot.edit_message_text.await_count == 1)
    filled = context.bot.edit_message_text.await_args.kwargs
    expect("on the same message", filled.get("message_id") == 101, repr(filled))
    expect("with the live result", "Story: 🎬 has an active story" in filled["text"],
           filled["text"])
    expect("and the rest of the card unchanged",
           filled["text"].replace("Story: 🎬 has an active story",
                                  f"Story: {handlers.STORY_CHECKING}") == first)


async def test_a_late_answer_never_overwrites_where_the_user_went() -> None:
    await _account("movedon", private=False)
    answer = asyncio.Event()

    async def slow_reel(user_id):
        await answer.wait()
        return {"has_public_story": False, "is_live": False}

    query, context = _tap(_service(slow_reel), 202)
    await handlers._show_account_card(query, context, "movedon")
    handlers.remember_render(202, "the main menu", None)  # the user tapped on
    answer.set()
    await _settle()
    expect("a message showing something else is left alone",
           context.bot.edit_message_text.await_count == 0)


async def test_a_hung_check_gives_up() -> None:
    await _account("hung", private=False)

    async def never(user_id):
        await asyncio.Event().wait()

    query, context = _tap(_service(never), 303)
    old_timeout = handlers._CARD_LIVE_TIMEOUT
    handlers._CARD_LIVE_TIMEOUT = 0.05
    try:
        await handlers._show_account_card(query, context, "hung")
        await _settle()
    finally:
        handlers._CARD_LIVE_TIMEOUT = old_timeout
    text = context.bot.edit_message_text.await_args.kwargs["text"]
    expect("a check that never answers says so instead of 'checking…' forever",
           "didn't answer" in text and handlers.STORY_CHECKING not in text, text)


async def test_a_private_card_has_nothing_to_fill() -> None:
    await _account("privcard", private=True)
    reel = AsyncMock(return_value={"has_public_story": True})
    query, context = _tap(_service(reel), 404)
    await handlers._show_account_card(query, context, "privcard")
    await _settle()
    text = query.edit_message_text.await_args.kwargs["text"]
    expect("a private account's card has no story line", "Story:" not in text, text)
    expect("and asks Instagram nothing", reel.await_count == 0)


async def test_an_unmonitored_account_is_reported() -> None:
    query, context = _tap(_service(AsyncMock()), 505)
    shown = await handlers._show_account_card(query, context, "nobody")
    expect("an account that isn't monitored draws nothing",
           shown is False and query.edit_message_text.await_count == 0)


class _FakeClient:
    calls: list[str] = []
    kwargs: dict = {}

    def __init__(self, *a, **k) -> None:
        _FakeClient.kwargs = k

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def get(self, url):
        _FakeClient.calls.append(url)
        return SimpleNamespace(status_code=200)


class _NullService:
    async def check_all(self) -> None:
        pass


async def test_keep_awake() -> None:
    old_url = settings.render_external_url
    try:
        settings.render_external_url = None
        sched = scheduler_module.WatcherScheduler(_NullService())
        await sched.start()
        expect("off Render (no public URL) nothing is pinged",
               sched.scheduler.get_job(scheduler_module.KEEP_AWAKE_JOB_ID) is None)
        await sched.shutdown()

        settings.render_external_url = "https://the-watcher.onrender.com/"
        sched = scheduler_module.WatcherScheduler(_NullService())
        await sched.start()
        job = sched.scheduler.get_job(scheduler_module.KEEP_AWAKE_JOB_ID)
        expect("on Render the keep-awake job runs",
               job is not None, repr(job))
        interval = job.trigger.interval.total_seconds() if job else 0
        expect("often enough that one failed ping still leaves the next inside "
               "Render's 15-minute idle limit", 0 < 2 * interval < 900, repr(interval))
        expect("a tick delayed by a busy sweep still runs (APScheduler's default "
               "1 s grace would drop it)",
               job is not None and (job.misfire_grace_time or 0) >= interval,
               repr(job and job.misfire_grace_time))

        real_client = scheduler_module.httpx.AsyncClient
        scheduler_module.httpx.AsyncClient = _FakeClient
        try:
            await sched._keep_awake()
        finally:
            scheduler_module.httpx.AsyncClient = real_client
        expect("it asks the bot's own public /health",
               _FakeClient.calls == ["https://the-watcher.onrender.com/health"],
               repr(_FakeClient.calls))
        expect("never through the Instagram proxy in HTTP(S)_PROXY",
               _FakeClient.kwargs.get("trust_env") is False, repr(_FakeClient.kwargs))

        def _socks_client(*a, **k):
            raise ImportError("Using SOCKS proxy, but the 'socksio' package is not installed.")

        scheduler_module.httpx.AsyncClient = _socks_client
        try:
            await sched._keep_awake()
            survived = True
        except Exception:
            survived = False
        finally:
            scheduler_module.httpx.AsyncClient = real_client
        expect("a ping that fails in any way is logged, never raised", survived)
        await sched.shutdown()
    finally:
        settings.render_external_url = old_url


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await test_card_draws_before_the_live_check_answers()
    await test_a_late_answer_never_overwrites_where_the_user_went()
    await test_a_hung_check_gives_up()
    await test_a_private_card_has_nothing_to_fill()
    await test_an_unmonitored_account_is_reported()
    await test_keep_awake()

    await engine.dispose()
    if DB_FILE.exists():
        DB_FILE.unlink()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All responsiveness tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
