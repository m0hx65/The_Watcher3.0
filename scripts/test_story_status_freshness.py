"""Regression tests: story/live status is LIVE data, said ONCE.

Two bugs are locked down here.

Freshness: an account Instagram was blocking (a small account that 401s on the
anonymous gate) kept getting "🎬 HAS STORY" in every sweep, because the story
phase read `reel_data` back out of the newest SUCCESSFUL snapshot — which for
such an account is days old. Its Story button, a live saveinsta fetch, said "no
active story" at the same moment.

Volume: one story used to produce a status line in every sweep it survived,
plus a "just posted a story!" alert, plus a "posted N new story items" header,
plus the media itself — four messages for one event, then one more per sweep.
The fix that stuck is the one enforced here: the status line is NEVER sent on
top of the media (or its text stand-in) that already announced the same story.

On top of that, STORY_STATUS_HEARTBEAT (default ON) decides whether a check that
has nothing else to say still reports where the account stands:
- heartbeat on  — every check answers, including "⭕ NO STORY", so silence never
  has to be interpreted;
- heartbeat off — only a CHANGE is announced, and "unknown" stays quiet.
Both modes are exercised; `quiet_mode()` below opts a test into the second.

Covered here:
- a blocked reel query never re-reports a stored status,
- a blocked check never moves the transition baseline, so it can't manufacture
  or swallow a "just posted a story!",
- the "just …" alert fires exactly once per transition, not once per sweep,
- delivered media is the only message about that story, in EITHER mode,
- the default answers every check; quiet mode stays silent until something
  changes; a manual Recheck always answers in both,
- a 401/404 profile fetch still records last-checked/status/failure count, so
  the card can't show a days-old check as a healthy HTTP 200.

Runs offline on sqlite with fakes.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_story_status.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.config import settings  # noqa: E402
from app.database import crud  # noqa: E402
from app.database.models import (  # noqa: E402
    AccountSnapshot,
    Base,
    MonitoredAccount,
)
from app.database.session import engine, get_session  # noqa: E402
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


@contextlib.contextmanager
def quiet_mode():
    """Run a test with STORY_STATUS_HEARTBEAT off (change-only announcements).

    The heartbeat is the default, so the tests that pin the quiet behaviour opt
    into it explicitly rather than relying on whatever the default happens to
    be — which is what let this suite silently encode one mode as "normal".
    """
    previous = settings.story_status_heartbeat
    settings.story_status_heartbeat = False
    try:
        yield
    finally:
        settings.story_status_heartbeat = previous


class FakeInstagram:
    """Reel query that answers whatever the test sets (None = blocked)."""

    def __init__(self) -> None:
        self.reel: dict | None = None
        self.calls = 0

    async def fetch_reel_user(self, user_id):
        self.calls += 1
        return self.reel


class FakeStories:
    """saveinsta stand-in. `items` is what a story listing returns (empty by
    default); downloads always "succeed" with a dummy path."""

    def __init__(self) -> None:
        self.items: list[StoryItem] = []

    async def fetch_stories(self, username):
        return list(self.items)

    async def fetch_highlight_items(self, username, highlight_id, title):
        return []

    async def download(self, item, username):
        return Path("/tmp/fake.jpg")


def _story_item(pk: str) -> StoryItem:
    return StoryItem(
        pk=pk, taken_at=0, media_type="image",
        url="https://example.invalid/x.jpg", source="story",
    )


def _make_service(instagram: FakeInstagram) -> MonitorService:
    notifier = AsyncMock()
    notifier.send_text = AsyncMock(return_value=True)
    notifier.send_photo = AsyncMock(return_value=True)
    notifier.send_video = AsyncMock(return_value=True)
    notifier.create_forum_topic = AsyncMock(return_value=None)
    return MonitorService(
        instagram=instagram, hasher=AsyncMock(),
        notifier=notifier, stories=FakeStories(),
    )


def _reel(*, story: bool = False, live: bool = False) -> dict:
    return {"has_public_story": story, "is_live": live, "highlights": {}}


async def _story_phase(
    service, account_id, username, reel_data=None, *, always_report=False
) -> list[str]:
    """Run the sweep's story phase; return the texts it sent."""
    service.notifier.send_text.reset_mock()
    service.notifier.send_photo.reset_mock()
    await service._check_stories_and_highlights(
        account_id, username, instagram_id="555", reel_data=reel_data,
        always_report=always_report,
    )
    return [c.args[0] for c in service.notifier.send_text.call_args_list]


async def _new_account(username: str) -> int:
    async with get_session() as session:
        account = MonitoredAccount(
            username=username, active=True, instagram_id="555"
        )
        session.add(account)
        await session.flush()
        return account.id


# ---------- 1. A blocked reel query never re-reports a stored status ----------

async def test_blocked_check_never_reports_stored_status() -> None:
    account_id = await _new_account("blocked")
    # A days-old successful snapshot that DID see a story, exactly like the
    # account whose card still showed "Last check: <3 days ago> · HTTP 200".
    async with get_session() as session:
        session.add(
            AccountSnapshot(
                account_id=account_id,
                username="blocked",
                http_status=200,
                is_private=False,
                raw_response={
                    "data": {"user": {"id": "555"}},
                    "reel_data": _reel(story=True),
                },
            )
        )

    instagram = FakeInstagram()
    instagram.reel = None  # Instagram is blocking us right now
    service = _make_service(instagram)

    texts = await _story_phase(service, account_id, "blocked")

    expect("blocked check tried a live reel query", instagram.calls == 1,
           f"calls={instagram.calls}")
    expect("blocked check never claims HAS STORY",
           not any("HAS STORY" in t for t in texts), str(texts))
    expect("blocked check never claims NO STORY",
           not any("NO STORY" in t for t in texts), str(texts))
    # The whole point of the default: a check that couldn't reach Instagram says
    # so. "NO STORY" now prints routinely, so silence here would be read as
    # "nothing is up" — the one thing a blocked check must never imply.
    expect("the default says the status is unavailable",
           any("unavailable" in t for t in texts), str(texts))

    # Quiet mode drops the per-account notice; the sweep summary lists them.
    with quiet_mode():
        quiet = await _story_phase(service, account_id, "blocked")
    expect("quiet mode says nothing per-account", quiet == [], str(quiet))
    async with get_session() as session:
        rows = await crud.notifications_since(
            session, datetime.now(timezone.utc) - timedelta(minutes=5)
        )
    expect("the unavailable status is still logged",
           any(getattr(n, "change_type", None) == "story_status_unknown"
               for n, _ in rows), repr([getattr(n, "change_type", None) for n, _ in rows]))

    # A manual Recheck is a question, so it gets an answer either way.
    texts = await _story_phase(service, account_id, "blocked", always_report=True)
    expect("a manual recheck reports the status as unavailable",
           any("unavailable" in t for t in texts), str(texts))

    async with get_session() as session:
        state = await crud.get_setting(
            session, MonitorService._story_state_key(account_id)
        )
    expect("a blocked check stores no status baseline", state is None, repr(state))


# ---------- 2. Live status, and one alert per transition ----------

async def test_transitions_alert_once() -> None:
    """Change-only mode (STORY_STATUS_HEARTBEAT=false), start to finish."""
    account_id = await _new_account("target")
    instagram = FakeInstagram()
    service = _make_service(instagram)

    with quiet_mode():
        # First observation: a story is up, but with no prior observation there
        # is no transition to announce.
        texts = await _story_phase(service, account_id, "target", _reel(story=True))
        expect("first observation reports HAS STORY",
               any("HAS STORY" in t for t in texts), str(texts))
        expect("first observation is not a 'just posted' alert",
               not any("just posted" in t for t in texts), str(texts))

        # Same story still up on the next sweep — nothing has changed, so
        # nothing is said. This is the per-sweep spam quiet mode exists for.
        texts = await _story_phase(service, account_id, "target", _reel(story=True))
        expect("an unchanged story is silent", texts == [], str(texts))
        texts = await _story_phase(service, account_id, "target", _reel(story=True))
        expect("still silent on the sweep after that", texts == [], str(texts))

        # Story expires — that IS a change.
        texts = await _story_phase(service, account_id, "target", _reel())
        expect("expiry reports NO STORY",
               any("NO STORY" in t for t in texts), str(texts))
        expect("expiry says it once", len(texts) == 1, str(texts))
        texts = await _story_phase(service, account_id, "target", _reel())
        expect("staying storyless is silent", texts == [], str(texts))

        # A new story after that IS a transition.
        texts = await _story_phase(service, account_id, "target", _reel(story=True))
        expect("a new story alerts once",
               texts == [t for t in texts if "just posted a story" in t]
               and len(texts) == 1,
               str(texts))

        # Going live is its own transition.
        texts = await _story_phase(service, account_id, "target", _reel(live=True))
        expect("going live alerts",
               any("just went live" in t for t in texts), str(texts))
        texts = await _story_phase(service, account_id, "target", _reel(live=True))
        expect("still live does not re-alert",
               not any("just went live" in t for t in texts), str(texts))
        expect("still live is silent", texts == [], str(texts))

        # A manual Recheck reports the live status even when it hasn't changed.
        texts = await _story_phase(
            service, account_id, "target", _reel(live=True), always_report=True
        )
        expect("a manual recheck still reports LIVE NOW",
               any("LIVE NOW" in t for t in texts), str(texts))


# ---------- 3. A block in the middle can't fake a transition ----------

async def test_block_between_observations_keeps_baseline() -> None:
    account_id = await _new_account("gapped")
    instagram = FakeInstagram()
    service = _make_service(instagram)

    await _story_phase(service, account_id, "gapped", _reel())          # baseline: none
    await _story_phase(service, account_id, "gapped", _reel(story=True))  # alert
    # Instagram blocks the next two sweeps — status unknown, baseline untouched.
    instagram.reel = None
    await _story_phase(service, account_id, "gapped")
    async with get_session() as session:
        state = await crud.get_setting(
            session, MonitorService._story_state_key(account_id)
        )
    expect("blocked sweep leaves the baseline at the last observation",
           state == "story", repr(state))

    # Access returns and the SAME story is still up — that is not a new story,
    # and it isn't a status change either: the gap is a non-event.
    with quiet_mode():
        texts = await _story_phase(service, account_id, "gapped", _reel(story=True))
    expect("recovery does not re-alert for the same story",
           not any("just posted" in t for t in texts), str(texts))
    expect("a blocked sweep in the middle produces no message at all",
           texts == [], str(texts))

    # But a status that changed WHILE blocked is announced on recovery.
    instagram.reel = None
    await _story_phase(service, account_id, "gapped")
    texts = await _story_phase(service, account_id, "gapped", _reel())
    expect("a change that happened during the gap is announced",
           any("NO STORY" in t for t in texts), str(texts))


# ---------- 4. A delivered story is announced exactly once ----------

async def test_delivered_story_is_one_message() -> None:
    """The screenshot that started this: "just posted a story!", then "posted 1
    new story item", then the media — three messages, one story."""
    account_id = await _new_account("poster")
    instagram = FakeInstagram()
    service = _make_service(instagram)

    await _story_phase(service, account_id, "poster", _reel())  # baseline: none

    service.stories.items = [_story_item("s1")]
    texts = await _story_phase(service, account_id, "poster", _reel(story=True))
    expect("the media is delivered", service.notifier.send_photo.await_count == 1,
           repr(service.notifier.send_photo.await_count))
    expect("the media is the ONLY message about it", texts == [], str(texts))

    # The status baseline still moved, so in quiet mode the next sweep stays
    # quiet too. (With the heartbeat on it reports HAS STORY — the story is
    # still up and nothing else is speaking for it. See the default test.)
    with quiet_mode():
        texts = await _story_phase(service, account_id, "poster", _reel(story=True))
    expect("the sweep after a delivery is silent", texts == [], str(texts))

    # …and the event is still logged, so the digest sees it.
    async with get_session() as session:
        rows = await crud.notifications_since(
            session, datetime.now(timezone.utc) - timedelta(minutes=5)
        )
    kinds = [getattr(n, "change_type", None) for n, _ in rows]
    expect("a suppressed status is still logged for the digest",
           "story_posted" in kinds, repr(kinds))


async def test_failed_download_still_reports_the_story() -> None:
    """If the media can't be downloaded there is nothing to speak for it, so
    the text alert stands in — but still only once."""
    account_id = await _new_account("undownloadable")
    instagram = FakeInstagram()
    service = _make_service(instagram)

    async def failed_download(item, username):
        return None

    service.stories.download = failed_download  # type: ignore[assignment]
    await _story_phase(service, account_id, "undownloadable", _reel())

    service.stories.items = [_story_item("s2")]
    texts = await _story_phase(
        service, account_id, "undownloadable", _reel(story=True)
    )
    expect("no media went out", service.notifier.send_photo.await_count == 0)
    expect("the story is still reported", len(texts) == 1, str(texts))
    expect("the fallback names the story",
           "new story item" in texts[0], str(texts))


async def test_the_default_answers_for_every_check() -> None:
    """The default (STORY_STATUS_HEARTBEAT on): every check says where a public
    account stands, so silence never has to be interpreted — but a check that
    already delivered the story does NOT also get a status line."""
    account_id = await _new_account("heartbeat")
    instagram = FakeInstagram()
    service = _make_service(instagram)

    # No story, sweep after sweep: each one says so, out loud.
    first = await _story_phase(service, account_id, "heartbeat", _reel())
    expect("a storyless check reports NO STORY",
           any("NO STORY" in t for t in first), str(first))
    again = await _story_phase(service, account_id, "heartbeat", _reel())
    expect("and says it again on the next check — this is the whole point",
           any("NO STORY" in t for t in again), str(again))
    expect("exactly one line per check", len(again) == 1, str(again))
    expect("addressed to the account, in the owner's format",
           again[0].startswith("<b>@heartbeat</b> — ⭕"), repr(again[0]))

    # A story that is up but has nothing new to deliver still reports.
    standing = await _story_phase(service, account_id, "heartbeat", _reel(story=True))
    expect("a standing story reports HAS STORY",
           any("HAS STORY" in t or "just posted" in t for t in standing),
           str(standing))
    standing = await _story_phase(service, account_id, "heartbeat", _reel(story=True))
    expect("and keeps reporting it while it is up",
           any("HAS STORY" in t for t in standing), str(standing))

    # But the status line never piles on top of the media that just announced
    # the same story — that is the regression this suite exists for.
    service.stories.items = [_story_item("hb1")]
    delivered = await _story_phase(service, account_id, "heartbeat", _reel(story=True))
    expect("the media is delivered", service.notifier.send_photo.await_count == 1,
           repr(service.notifier.send_photo.await_count))
    expect("the heartbeat does not repeat what the media just said",
           delivered == [], str(delivered))


# ---------- 5. 401/404 failures are recorded, not swallowed ----------

async def test_gate_failures_are_recorded() -> None:
    account_id = await _new_account("gated")
    instagram = FakeInstagram()
    service = _make_service(instagram)

    fetch = ProfileFetchResult(
        username="gated", http_status=401, parsed=None, error="blocked"
    )
    await service._handle_failure(account_id, "gated", fetch)

    async with get_session() as session:
        account = await session.get(MonitoredAccount, account_id)
        expect("401 updates last_status_code", account.last_status_code == 401,
               repr(account.last_status_code))
        expect("401 stamps last_checked_at", account.last_checked_at is not None)
        expect("401 counts as a consecutive failure",
               account.consecutive_failures == 1, repr(account.consecutive_failures))
        snapshot = await crud.get_latest_snapshot(
            session, account_id, successful_only=False
        )
    expect("401 stores no snapshot row (flaky gate, not history)",
           snapshot is None, repr(snapshot))

    # Repeat blocks keep counting; a success clears it.
    await service._handle_failure(account_id, "gated", fetch)
    async with get_session() as session:
        account = await session.get(MonitoredAccount, account_id)
        expect("consecutive 401s accumulate", account.consecutive_failures == 2,
               repr(account.consecutive_failures))
        await crud.mark_checked(session, account_id, 200, success=True)
        account = await session.get(MonitoredAccount, account_id)
    expect("a success clears the failure streak",
           account.consecutive_failures == 0, repr(account.consecutive_failures))

    # 401/404 stay out of the per-account alert stream (the sweep summary
    # already names them) — only the bookkeeping runs.
    expect("no failure alert for a gate status",
           service.notifier.send_text.await_count == 0,
           repr(service.notifier.send_text.await_count))


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await test_blocked_check_never_reports_stored_status()
    await test_transitions_alert_once()
    await test_block_between_observations_keeps_baseline()
    await test_delivered_story_is_one_message()
    await test_failed_download_still_reports_the_story()
    await test_the_default_answers_for_every_check()
    await test_gate_failures_are_recorded()

    await engine.dispose()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All story-status freshness tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
