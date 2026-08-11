"""Regression tests: story/live status is LIVE data or an honest "unavailable".

The bug this locks down: an account Instagram was blocking (a small account
that 401s on the anonymous gate) kept getting "🎬 HAS STORY" in every sweep,
because the story phase read `reel_data` back out of the newest SUCCESSFUL
snapshot — which for such an account is days old. Its Story button, a live
saveinsta fetch, said "no active story" at the same moment.

Covered here:
- a blocked reel query never re-reports a stored status (says "unavailable"),
- a blocked check never moves the transition baseline, so it can't manufacture
  or swallow a "just posted a story!",
- the "just …" alert fires exactly once per transition, not once per sweep,
- a 401/404 profile fetch still records last-checked/status/failure count, so
  the card can't show a days-old check as a healthy HTTP 200.

Runs offline on sqlite with fakes.
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

DB_FILE = ROOT / "test_story_status.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.database import crud  # noqa: E402
from app.database.models import (  # noqa: E402
    AccountSnapshot,
    Base,
    MonitoredAccount,
)
from app.database.session import engine, get_session  # noqa: E402
from app.monitor.instagram import ProfileFetchResult  # noqa: E402
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


class FakeInstagram:
    """Reel query that answers whatever the test sets (None = blocked)."""

    def __init__(self) -> None:
        self.reel: dict | None = None
        self.calls = 0

    async def fetch_reel_user(self, user_id):
        self.calls += 1
        return self.reel


class FakeStories:
    """saveinsta stand-in: the account has no story media."""

    async def fetch_stories(self, username):
        return []

    async def fetch_highlight_items(self, username, highlight_id, title):
        return []


def _make_service(instagram: FakeInstagram) -> MonitorService:
    notifier = AsyncMock()
    notifier.send_text = AsyncMock(return_value=True)
    notifier.create_forum_topic = AsyncMock(return_value=None)
    return MonitorService(
        instagram=instagram, hasher=AsyncMock(),
        notifier=notifier, stories=FakeStories(),
    )


def _reel(*, story: bool = False, live: bool = False) -> dict:
    return {"has_public_story": story, "is_live": live, "highlights": {}}


async def _story_phase(service, account_id, username, reel_data=None) -> list[str]:
    """Run the sweep's story phase; return the texts it sent."""
    service.notifier.send_text.reset_mock()
    await service._check_stories_and_highlights(
        account_id, username, instagram_id="555", reel_data=reel_data
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
    expect("blocked check says the status is unavailable",
           any("unavailable" in t for t in texts), str(texts))

    async with get_session() as session:
        state = await crud.get_setting(
            session, MonitorService._story_state_key(account_id)
        )
    expect("a blocked check stores no status baseline", state is None, repr(state))


# ---------- 2. Live status, and one alert per transition ----------

async def test_transitions_alert_once() -> None:
    account_id = await _new_account("target")
    instagram = FakeInstagram()
    service = _make_service(instagram)

    # First observation: a story is up, but with no prior observation there is
    # no transition to announce.
    texts = await _story_phase(service, account_id, "target", _reel(story=True))
    expect("first observation reports HAS STORY",
           any("HAS STORY" in t for t in texts), str(texts))
    expect("first observation is not a 'just posted' alert",
           not any("just posted" in t for t in texts), str(texts))

    # Same story still up on the next sweep — status line, not a fresh alert.
    texts = await _story_phase(service, account_id, "target", _reel(story=True))
    expect("an unchanged story repeats the status line",
           any("HAS STORY" in t for t in texts), str(texts))
    expect("an unchanged story does not re-alert",
           not any("just posted" in t for t in texts), str(texts))

    # Story expires.
    texts = await _story_phase(service, account_id, "target", _reel())
    expect("expiry reports NO STORY", any("NO STORY" in t for t in texts), str(texts))

    # A new story after that IS a transition.
    texts = await _story_phase(service, account_id, "target", _reel(story=True))
    expect("a new story alerts once",
           any("just posted a story" in t for t in texts), str(texts))

    # Going live is its own transition.
    texts = await _story_phase(service, account_id, "target", _reel(live=True))
    expect("going live alerts", any("just went live" in t for t in texts), str(texts))
    texts = await _story_phase(service, account_id, "target", _reel(live=True))
    expect("still live does not re-alert",
           not any("just went live" in t for t in texts), str(texts))
    expect("still live reports LIVE NOW",
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
    texts = await _story_phase(service, account_id, "gapped")
    expect("blocked sweep reports unavailable",
           any("unavailable" in t for t in texts), str(texts))
    async with get_session() as session:
        state = await crud.get_setting(
            session, MonitorService._story_state_key(account_id)
        )
    expect("blocked sweep leaves the baseline at the last observation",
           state == "story", repr(state))

    # Access returns and the SAME story is still up — that is not a new story.
    texts = await _story_phase(service, account_id, "gapped", _reel(story=True))
    expect("recovery does not re-alert for the same story",
           not any("just posted" in t for t in texts), str(texts))
    expect("recovery reports HAS STORY",
           any("HAS STORY" in t for t in texts), str(texts))


# ---------- 4. 401/404 failures are recorded, not swallowed ----------

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
