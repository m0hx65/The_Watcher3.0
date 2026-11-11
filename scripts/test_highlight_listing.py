"""Highlight listings: what a sweep and a tap on a highlight do with them.

Found reading the log of 2026-11-07..10 and the code around it:

- Tapping a highlight answered the button twice: "Downloading…", then the
  outcome. A callback query takes one answer, so Telegram dropped the second
  ("already answered", which _safe_answer ignores) — and a download that
  failed showed nothing at all. Both downloads tried by hand on 2026-11-08
  ended that way.
- A reel whose listing came back with nothing when an account was baselined
  had no item recorded, and was written off as needing no quiet first
  listing: the first listing that worked delivered the whole reel as new.
  The same on unmuting a reel that could not be listed just then.
- "Once per HIGHLIGHT_SCAN_INTERVAL" was asked once per sweep with both set
  to 6 h, and settled by seconds of jitter (due by two on 2026-11-09): a miss
  put the listing off to the next sweep, twelve hours on.
- Only a listing's failures were logged, so one that found nothing new and
  one that never ran looked the same.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_highlight_listing.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("TELEGRAM_ADMIN_IDS", "99")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.bot import handlers  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import crud  # noqa: E402
from app.database.models import Base, MonitoredAccount  # noqa: E402
from app.database.session import dispose_engine, engine, get_session  # noqa: E402
from app.monitor import service as service_mod  # noqa: E402
from app.monitor.instagram import IdProbe, ProfileFetchResult  # noqa: E402
from app.monitor.service import MonitorService  # noqa: E402
from app.monitor.stories import StoryItem  # noqa: E402
from app.monitor.storynavigation import Copy  # noqa: E402
from app.utils.logger import logger  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


class RefusingInstagram:
    """No door of Instagram's answers: the stored catalog stands."""

    direct_page_door_failing = False
    direct_page_failures = 0

    async def fetch_profile(self, username: str, **kw) -> ProfileFetchResult:
        return ProfileFetchResult(username=username, http_status=401, error="HTTP 401", api_status=401)

    async def probe_by_id(self, user_id: str, **kw) -> IdProbe:
        return IdProbe(user_id=str(user_id), status=401)

    async def probe_public_page(self, username: str, **kw) -> dict:
        return {"status": 0, "parsed": None}

    async def fetch_reel_user(self, user_id: str):
        return None

    def reel_in_hand(self, user_id: str):
        return None


class ListingStories:
    """Lists whatever a test scripts per reel; [] is a listing that failed."""

    def __init__(self, items_by_reel: dict[str, list[str]], copy: Optional[Copy] = None) -> None:
        self.items_by_reel = items_by_reel
        self.copy = copy
        self.asked: list[str] = []

    async def fetch_stories(self, username):
        return []

    async def fetch_highlight_items(self, username, highlight_id, title):
        self.asked.append(highlight_id)
        return [
            StoryItem(pk=pk, taken_at=0, media_type="image",
                      url=f"https://scontent.cdninstagram.com/v/{pk}.jpg", source="highlight",
                      highlight_id=highlight_id, highlight_title=title, copy=self.copy)
            for pk in self.items_by_reel.get(highlight_id, [])
        ]

    async def fetch_posts(self, username, limit: int = 12):
        return []

    def refusal_since(self, since):
        return None

    def highlight_failure_since(self, since):
        return None


def _service(stories) -> tuple[MonitorService, list[str]]:
    """A service whose deliveries are recorded instead of downloaded."""
    notifier = AsyncMock()
    notifier.send_text = AsyncMock(return_value=True)
    notifier.create_forum_topic = AsyncMock(return_value=None)
    service = MonitorService(
        instagram=RefusingInstagram(), hasher=AsyncMock(), notifier=notifier, stories=stories,
    )
    delivered: list[str] = []

    async def deliver(account_id, username, items, seen, **kw):
        delivered.extend(item.pk for item in items)
        return len(items)

    service._deliver_story_items = deliver  # type: ignore[method-assign]
    return service, delivered


async def _new_account(username: str, catalog: Optional[dict[str, str]] = None) -> int:
    async with get_session() as session:
        account = MonitoredAccount(username=username, active=True, instagram_id="4301")
        session.add(account)
        await session.flush()
        if catalog:
            await crud.replace_highlight_catalog(session, account.id, catalog)
        return account.id


async def _check(service: MonitorService, account_id: int, username: str, catalog: dict[str, str]) -> None:
    await service._check_stories_and_highlights(
        account_id, username, instagram_id="4301",
        reel_data={
            "has_public_story": False, "is_live": False, "from_page": True,
            "highlights": catalog,
        },
    )


async def _quiet(account_id: int) -> Optional[list]:
    async with get_session() as session:
        raw = await crud.get_setting(session, f"highlight_quiet_reels:{account_id}")
    return json.loads(raw) if raw is not None else None


# ---------- nothing old is delivered as new ----------------------------------------

async def test_a_reel_unlisted_at_the_baseline_does_not_flood_later() -> None:
    old_interval = settings.highlight_scan_interval
    settings.highlight_scan_interval = 0  # every check re-lists every reel
    try:
        catalog = {"h1": "Trips", "h2": "Pets"}
        account_id = await _new_account("freshtarget")
        stories = ListingStories({"h1": ["a1", "a2"], "h2": []})  # h2's listing fails
        service, delivered = _service(stories)

        await _check(service, account_id, "freshtarget", catalog)
        expect("a baseline delivers nothing", delivered == [], repr(delivered))
        expect("the reel that could not be listed stays owed a quiet first listing",
               await _quiet(account_id) == ["h2"], repr(await _quiet(account_id)))

        stories.items_by_reel = {"h1": ["a1", "a2"], "h2": ["b1", "b2", "b3"]}
        await _check(service, account_id, "freshtarget", catalog)
        expect("its first listing that works is recorded, not delivered as three new items",
               delivered == [], repr(delivered))
        async with get_session() as session:
            seen = await crud.get_seen_story_pks(session, account_id)
        expect("its items are on record now", {"b1", "b2", "b3"} <= set(seen), repr(seen))
        expect("and nothing is owed any more", await _quiet(account_id) == [],
               repr(await _quiet(account_id)))

        stories.items_by_reel = {"h1": ["a1", "a2"], "h2": ["b1", "b2", "b3", "b4"]}
        await _check(service, account_id, "freshtarget", catalog)
        expect("an item added to it afterwards is delivered", delivered == ["b4"], repr(delivered))
    finally:
        settings.highlight_scan_interval = old_interval


async def test_unmuting_a_reel_that_cannot_be_listed_keeps_its_items_quiet() -> None:
    old_interval = settings.highlight_scan_interval
    settings.highlight_scan_interval = 0
    try:
        catalog = {"h1": "Trips", "h2": "Pets"}
        account_id = await _new_account("mutedtarget", catalog)
        async with get_session() as session:
            # Past its own first pass: nothing owed before the reel is unmuted.
            await crud.set_setting(session, f"highlight_quiet_reels:{account_id}", "[]")
            await crud.set_highlight_tracked(session, account_id, "h1", False)
        stories = ListingStories({"h1": [], "h2": ["t1"]})  # h1 cannot be listed just now
        service, delivered = _service(stories)

        result = await service.toggle_highlight_tracking("mutedtarget", 0)
        expect("the reel is tracked again", result.get("ok") and result.get("tracked") is True,
               repr(result))
        expect("and owed a quiet listing, since nothing could be recorded for it",
               await _quiet(account_id) == ["h1"], repr(await _quiet(account_id)))

        stories.items_by_reel = {"h1": ["m1", "m2"], "h2": ["t1"]}
        async with get_session() as session:
            await crud.mark_story_items_seen(session, account_id, [StoryItem(
                pk="t1", taken_at=0, media_type="image", url="x", source="highlight",
                highlight_id="h2")])
        await _check(service, account_id, "mutedtarget", catalog)
        expect("what was posted while it was muted stays skipped", delivered == [], repr(delivered))
        stories.items_by_reel = {"h1": ["m1", "m2", "m3"], "h2": ["t1"]}
        await _check(service, account_id, "mutedtarget", catalog)
        expect("and what is posted after is delivered", delivered == ["m3"], repr(delivered))
    finally:
        settings.highlight_scan_interval = old_interval


async def test_unmute_all_re_baselines_only_the_muted_reels() -> None:
    catalog = {"h1": "Trips", "h2": "Pets", "h3": "Food"}
    account_id = await _new_account("muteall", catalog)
    async with get_session() as session:
        await crud.set_setting(session, f"highlight_quiet_reels:{account_id}", "[]")
        await crud.set_highlight_tracked(session, account_id, "h1", False)
        await crud.set_highlight_tracked(session, account_id, "h3", False)
    stories = ListingStories({"h1": ["m1"], "h2": ["new-in-a-tracked-reel"], "h3": []})
    service, _ = _service(stories)

    result = await service.set_all_highlight_tracking("muteall", True)
    expect("every reel is tracked again", result.get("ok") and result.get("count") == 3, repr(result))
    expect("only the muted reels were listed", sorted(stories.asked) == ["h1", "h3"],
           repr(stories.asked))
    async with get_session() as session:
        seen = await crud.get_seen_story_pks(session, account_id)
    expect("the listed one's items are recorded as seen", "m1" in seen, repr(seen))
    expect("a reel that was never muted keeps an item the sweep has yet to deliver",
           "new-in-a-tracked-reel" not in seen, repr(seen))
    expect("and the muted one that could not be listed is owed a quiet listing",
           await _quiet(account_id) == ["h3"], repr(await _quiet(account_id)))


# ---------- the sweep says what it found -------------------------------------------

async def test_the_sweep_says_what_a_listing_found() -> None:
    old_interval = settings.highlight_scan_interval
    settings.highlight_scan_interval = 0
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(message.record["message"]), level="INFO")
    try:
        catalog = {"h1": "Trips", "h2": "Pets"}
        account_id = await _new_account("loggedtarget", catalog)
        async with get_session() as session:
            await crud.set_setting(session, f"highlight_quiet_reels:{account_id}", "[]")
            await crud.mark_story_items_seen(session, account_id, [StoryItem(
                pk="a1", taken_at=0, media_type="image", url="x", source="highlight",
                highlight_id="h1")])
        stories = ListingStories({"h1": ["a1", "a2"], "h2": []}, copy=Copy(6 * 3600.0, exact=True))
        service, delivered = _service(stories)
        await _check(service, account_id, "loggedtarget", catalog)
        found = [line for line in lines if line.startswith("@loggedtarget: highlights")]
        expect("one line per listing, whichever way it went", len(found) == 1, repr(found))
        expect("with how many reels answered, what they held, and how old the copy was",
               found == ["@loggedtarget: highlights — 1 of 2 reel(s) listed, 2 item(s), "
                         "1 the chat has not seen (storynavigation's copy from ~6 h ago)"],
               repr(found))
        expect("and the new item is delivered from that copy", delivered == ["a2"], repr(delivered))
    finally:
        logger.remove(sink)
        settings.highlight_scan_interval = old_interval


# ---------- "once per interval", asked once per sweep ------------------------------

async def test_a_scan_is_due_a_little_early_not_a_sweep_late() -> None:
    due = service_mod._scan_due
    now = time.time()
    six_hours = 21600
    expect("a scan last run 6 h less two seconds ago is due (it was decided by those seconds)",
           due(now - (six_hours - 2), six_hours))
    expect("and one 15 minutes short is too", due(now - (six_hours - 890), six_hours))
    expect("one run an hour ago is not", not due(now - 3600, six_hours))
    expect("nor one 20 minutes short: the slack is 15 minutes at most",
           not due(now - (six_hours - 1200), six_hours))
    expect("for a short interval the slack is a tenth of it",
           due(now - 1625, 1800) and not due(now - 1500, 1800))
    expect("never run is due, and so is every sweep when the interval is 0",
           due(None, six_hours) and due(now, 0))

    old_highlight, old_post = settings.highlight_scan_interval, settings.post_scan_interval
    settings.highlight_scan_interval, settings.post_scan_interval = six_hours, 1800
    try:
        account_id = await _new_account("scantarget", {"h1": "Trips"})
        service, _ = _service(ListingStories({}))
        catalog = {"h1": "Trips"}
        async with get_session() as session:
            await crud.set_setting(
                session, service._highlight_scan_key(account_id), str(now - (six_hours - 30)))
            await crud.set_setting(
                session, service._post_scan_key(account_id), str(now - 1790))
            stamps = await crud.get_settings_by_prefix(session, "highlight_scan:")
        scan, full = await service._due_highlight_scan(account_id, catalog, catalog)
        expect("the story phase lists every reel a sweep that lands 30 s early",
               full and scan == catalog, repr((scan, full)))
        expect("and the sweep's own look ahead agrees with it",
               service._highlight_scan_overdue(account_id, stamps))
        expect("the post grid's clock has the same slack", await service._due_post_scan(account_id))
        async with get_session() as session:
            await crud.set_setting(
                session, service._highlight_scan_key(account_id), str(now - 3600))
        scan, full = await service._due_highlight_scan(account_id, catalog, catalog)
        expect("an hour after a scan, only new reels are listed", not full and scan == {},
               repr((scan, full)))
    finally:
        settings.highlight_scan_interval, settings.post_scan_interval = old_highlight, old_post


# ---------- a tap on a highlight always gets its outcome ---------------------------

def _tap(result: dict) -> tuple[SimpleNamespace, SimpleNamespace]:
    query = SimpleNamespace(
        data="", answer=AsyncMock(), edit_message_text=AsyncMock(),
        message=SimpleNamespace(message_id=42, delete=AsyncMock()),
    )
    update = SimpleNamespace(
        callback_query=query, message=None,
        effective_chat=SimpleNamespace(id=7), effective_user=SimpleNamespace(id=99),
    )
    monitor = SimpleNamespace(download_highlight=AsyncMock(return_value=result))
    context = SimpleNamespace(
        bot=SimpleNamespace(delete_message=AsyncMock(), send_message=AsyncMock()),
        user_data={}, application=SimpleNamespace(bot_data={"monitor": monitor}),
    )
    return update, context


LISTING = {
    "ok": True, "items": [("h1", "Trips"), ("h2", "Pets")], "untracked": set(),
    "monitored": False, "error": None,
}


async def test_a_failed_download_tells_the_chat_why() -> None:
    reason = (
        "No anonymous source served it — storynavigation sent nothing for this "
        "highlight; saveinsta refused it: “Video is private.” Highlight names and "
        "story status still work."
    )
    update, context = _tap({"ok": False, "count": 0, "title": "Trips", "error": reason,
                            "listing": LISTING})
    await handlers._download_highlight(update, context, "nasa", "0")
    query = update.callback_query
    expect("the button is answered once — a callback query takes no second answer",
           query.answer.await_count == 1
           and query.answer.await_args.kwargs.get("text") == "Downloading…",
           repr(query.answer.await_args_list))
    expect("the outcome is written to the chat instead", query.edit_message_text.await_count == 1,
           repr(query.edit_message_text.await_count))
    shown = query.edit_message_text.await_args.kwargs
    text = shown.get("text", "")
    expect("with the reason", "was not downloaded" in text and "Video is private." in text, text)
    expect("under the highlight list, which stays usable",
           text.startswith("<b>✨ Highlights for @nasa</b> (2)")
           and "1. Trips" in text and shown.get("reply_markup") is not None, text)
    buttons = [b.callback_data for row in shown["reply_markup"].inline_keyboard for b in row]
    expect("each highlight still one tap away", "acc:hldl:0:nasa" in buttons and "acc:hldl:1:nasa" in buttons,
           repr(buttons))


async def test_a_download_says_what_it_sent_and_from_which_copy() -> None:
    update, context = _tap({
        "ok": True, "count": 5, "items": 5, "title": "Trips", "error": None, "listing": LISTING,
        "note": "storynavigation's copy from ~6 h ago",
    })
    await handlers._download_highlight(update, context, "nasa", "0")
    query = update.callback_query
    expect("answered once", query.answer.await_count == 1, repr(query.answer.await_args_list))
    expect("the list moves below the media it sent",
           query.message.delete.await_count == 1 and context.bot.send_message.await_count == 1,
           repr((query.message.delete.await_count, context.bot.send_message.await_count)))
    text = context.bot.send_message.await_args.kwargs.get("text", "")
    expect("saying what was sent", "✨ Sent 5 items from <b>Trips</b>." in text, text)
    expect("and which copy it was, since it is not this minute's",
           "Source: storynavigation's copy from ~6 h ago." in text
           and "anything added since is not in it" in text, text)

    update, context = _tap({
        "ok": True, "count": 2, "items": 2, "title": "Trips", "error": None, "listing": LISTING,
        "note": None,
    })
    await handlers._download_highlight(update, context, "nasa", "0")
    text = context.bot.send_message.await_args.kwargs.get("text", "")
    expect("a copy fetched just now gets no note", "Sent 2 items" in text and "Source:" not in text, text)

    update, context = _tap({
        "ok": True, "count": 0, "items": 3, "title": "Trips", "error": None, "listing": LISTING,
        "note": None,
    })
    await handlers._download_highlight(update, context, "nasa", "0")
    text = update.callback_query.edit_message_text.await_args.kwargs.get("text", "")
    expect("items that were listed but could not be downloaded are not “no items”",
           "3 item(s) listed, but none could be downloaded" in text, text)

    update, context = _tap({"ok": False, "count": 0, "title": None, "error": "Stories client unavailable"})
    await handlers._download_highlight(update, context, "nasa", "0")
    text = update.callback_query.edit_message_text.await_args.kwargs.get("text", "")
    expect("with no list to show, the outcome still reaches the chat",
           "Stories client unavailable" in text, text)


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await test_a_reel_unlisted_at_the_baseline_does_not_flood_later()
    await test_unmuting_a_reel_that_cannot_be_listed_keeps_its_items_quiet()
    await test_unmute_all_re_baselines_only_the_muted_reels()
    await test_the_sweep_says_what_a_listing_found()
    await test_a_scan_is_due_a_little_early_not_a_sweep_late()
    await test_a_failed_download_tells_the_chat_why()
    await test_a_download_says_what_it_sent_and_from_which_copy()

    await dispose_engine()
    if DB_FILE.exists():
        DB_FILE.unlink()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("All highlight-listing tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
