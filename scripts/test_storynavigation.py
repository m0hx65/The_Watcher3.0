"""storynavigation.com as the highlight media source: each copy with its age.

It fetches a reel from Instagram on the first request and serves that copy
from storage for a day or more; nothing in an answer says how old it is. Until
2026-11-10 an answer counted only when it took 2.5 s or more (or was this
client's own copy of the last 15 minutes), which served 3 of ~24 sweep
listings in three days and neither download asked for by hand — a fetch can
take 1.2 s, and the copy a sweep's own request makes it fetch is then the
stored one for everybody after.

What must hold:
- an answer that took seconds was fetched now: served, dated exactly;
- a stored copy is served too, with what is known of its age — exactly when
  this client saw it fetched (however long ago, and whatever other reels it
  has fetched since), at most when it replaced a copy this client was served
  before, unknown otherwise;
- a copy this client was served before is that copy however long its answer
  took: timing never re-dates it;
- a time between a stored answer's and a fetch's is settled by asking once
  more — and only then;
- an empty answer (their failed fetch, or the [] they then store) is a
  failure, never "this highlight is empty"; so is a copy with an expired link;
- HIGHLIGHT_COPY_MAX_AGE refuses a copy older than it, or of unknown age;
- a stale CSRF token is renewed once; every refusal says why;
- the client asks storynavigation first and saveinsta only when it has
  nothing; the items carry their copy's age, a download says it, and an error
  names every source's reason — a story's error never storynavigation's.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_storynavigation.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.config import settings  # noqa: E402
from app.database.models import Base  # noqa: E402
from app.database.session import dispose_engine, engine  # noqa: E402
from app.monitor import service as service_mod  # noqa: E402
from app.monitor import storynavigation as storynav_mod  # noqa: E402
from app.monitor.service import MonitorService  # noqa: E402
from app.monitor.stories import StoriesClient, StoryItem  # noqa: E402
from app.monitor.storynavigation import Copy, StoryNavigation, copies_note  # noqa: E402

# The real lines are 2.5 s (a fetch), 0.9 s (a stored copy) and a 0.7 s margin
# for the second ask. Scaled down: a test's fetch takes LIVE, an answer that
# could be either takes BETWEEN, a stored one answers at once.
storynav_mod._LIVE_MIN_SECONDS = 0.45
storynav_mod._STORED_MAX_SECONDS = 0.12
storynav_mod._LIVE_MARGIN_SECONDS = 0.1
LIVE = 0.6
BETWEEN = 0.25

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def b64(url: str) -> str:
    return base64.b64encode(url.encode()).decode()


def oe(hours: float) -> str:
    """A link expiry `hours` from now, the way Instagram writes it."""
    return format(int(time.time() + hours * 3600), "X")


def video(n: int, oh: str = "s", *, expires: float = 33.0) -> dict:
    return {
        "type": "video", "createdTime": "2026-11-04 20:01:32",
        "videoUrl": b64(f"https://scontent-lga3-1.cdninstagram.com/o1/v/t2/f2/m86/clip{n}.mp4?oe={oe(expires)}&oh={oh}{n}"),
        "thumbnailUrl": b64(f"https://scontent-lga3-1.cdninstagram.com/v/t51.71878-15/70{n}0001_18{n}000001_1_n.jpg?oh=t{n}"),
    }


def image(n: int, oh: str = "s") -> dict:
    return {
        "type": "image", "createdTime": "2026-04-10 15:39:05", "videoUrl": None,
        "thumbnailUrl": b64(f"https://scontent-lga3-1.cdninstagram.com/v/t51.71878-15/66{n}61457_18{n}99661067_1_n.jpg?oe={oe(105)}&oh={oh}{n}"),
    }


def url_of(entry: dict) -> str:
    return base64.b64decode(entry.get("videoUrl") or entry["thumbnailUrl"]).decode()


class Answer:
    def __init__(self, status: int = 200, body=None, *, text: str | None = None, delay: float = 0.0) -> None:
        self.status_code = status
        self.text = text if text is not None else ("" if body is None else json.dumps(body))
        self.delay = delay

    def json(self):
        return json.loads(self.text)


HOME = Answer(200, text='<html><head><meta name="csrf-token" content="tok1"></head></html>')


class Site:
    """storynavigation as the bot meets it: the home page with its token,
    then scripted answers to /get-highlight-stories."""

    def __init__(self, *answers: Answer, home: Answer = HOME) -> None:
        self.answers, self.home = list(answers), home
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict, dict]] = []
        self.in_flight = self.peak = 0

    async def get(self, url, **kw):
        self.gets.append(url)
        return self.home

    async def post(self, url, headers=None, data=None, **kw):
        self.posts.append((url, dict(headers or {}), json.loads(data)))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(answer.delay)
        finally:
            self.in_flight -= 1
        return answer

    async def close(self) -> None:
        pass


REEL = "18023864549864045"


async def ask(site: Site, highlight_id: str = REEL, nav: StoryNavigation | None = None):
    nav = nav or StoryNavigation()
    since = time.monotonic()
    reel = await nav.highlight_media(site, highlight_id)
    return reel, nav.failure_since(since), nav


def age_backdated(nav: StoryNavigation, seconds: float, highlight_id: str = REEL) -> None:
    """Make the copy on record `seconds` older, as time passing would."""
    seen = nav._reels[highlight_id]
    seen.seen_at -= seconds
    if seen.fetched_at is not None:
        seen.fetched_at -= seconds
    if seen.after is not None:
        seen.after -= seconds


# ---------- fetched now, or a stored copy ------------------------------------------

async def test_a_fetch_is_served_and_dated_now() -> None:
    playlist = [video(1), image(2)]
    site = Site(Answer(200, playlist, delay=LIVE))
    reel, why, _ = await ask(site, f"highlight:{REEL}")
    expect("an answer that took seconds is served, in order",
           reel.media == [("video", url_of(playlist[0])), ("image", url_of(playlist[1]))],
           repr(reel.media))
    expect("its copy is dated exactly, as fetched now",
           reel.copy is not None and reel.copy.exact and reel.copy.age is not None
           and reel.copy.age < 1 and reel.copy.fresh, repr(reel.copy))
    expect("with no failure on record", why is None, repr(why))
    url, headers, body = site.posts[0]
    expect("asked exactly as the page asks",
           url == "https://storynavigation.com/get-highlight-stories"
           and body == {"highlightId": f"highlight:{REEL}"}
           and headers.get("X-CSRF-TOKEN") == "tok1"
           and headers.get("X-Requested-With") == "XMLHttpRequest", repr(site.posts[0]))
    expect("the token came from its home page", site.gets == ["https://storynavigation.com/"], repr(site.gets))
    expect("and one request was enough", len(site.posts) == 1, repr(len(site.posts)))


async def test_a_stored_copy_is_served_with_what_is_known() -> None:
    site = Site(Answer(200, [video(1), video(2)]))
    reel, why, _ = await ask(site)
    expect("a quick answer is a stored copy, and is served", len(reel.media) == 2 and why is None,
           repr((reel.media, why)))
    expect("on a first look nothing says how old it is",
           reel.copy == Copy() and not reel.copy.fresh
           and reel.copy.describe() == "stored copy, of unknown age", repr(reel.copy))
    expect("and no second request was spent on a quick answer", len(site.posts) == 1, repr(len(site.posts)))


async def test_its_own_fetch_is_dated_however_long_ago() -> None:
    """The copy a sweep's own request made storynavigation fetch is the one
    it serves for the next day. It was refused past 15 minutes ("only had
    the copy it fetched ~6 h ago"); now it is served, with that age."""
    playlist = [video(1), video(2)]
    site = Site(Answer(200, playlist, delay=LIVE), Answer(200, playlist), Answer(200, playlist))
    nav = StoryNavigation()
    await ask(site, nav=nav)
    reel, why, _ = await ask(site, nav=nav)
    expect("the same copy minutes later is still fresh (a listing, then a download)",
           len(reel.media) == 2 and reel.copy.fresh and reel.copy.exact, repr(reel.copy))
    age_backdated(nav, 6 * 3600)
    reel, why, _ = await ask(site, nav=nav)
    expect("six hours on it is served all the same", len(reel.media) == 2 and why is None,
           repr((reel.media, why)))
    expect("as the copy from six hours ago",
           reel.copy.exact and not reel.copy.fresh
           and reel.copy.describe() == "copy from ~6 h ago", repr(reel.copy))


async def test_another_reel_s_fetch_forgets_nothing() -> None:
    """Measured 2026-11-08: one reel's live fetch dropped every record older
    than 15 minutes, so the other reel's copy — fetched by this client six
    hours before — read as "a stored copy, of unknown age" from then on."""
    mine, other = [video(1)], [video(7, oh="z")]
    site = Site(Answer(200, mine, delay=LIVE), Answer(200, other, delay=LIVE), Answer(200, mine))
    nav = StoryNavigation()
    await ask(site, nav=nav)
    age_backdated(nav, 6 * 3600)
    await ask(site, "17874420114681748", nav=nav)
    reel, _, _ = await ask(site, nav=nav)
    expect("the first reel's copy still has its age after another reel is fetched",
           reel.copy.exact and reel.copy.describe() == "copy from ~6 h ago", repr(reel.copy))


async def test_a_copy_that_replaced_another_is_at_most_that_old() -> None:
    first, second = [video(1, oh="a")], [video(1, oh="b"), video(2, oh="b")]
    site = Site(Answer(200, first), Answer(200, second), Answer(200, second))
    nav = StoryNavigation()
    await ask(site, nav=nav)
    age_backdated(nav, 6 * 3600)
    reel, _, _ = await ask(site, nav=nav)
    expect("a new copy from storage was fetched since the last look",
           reel.copy.age is not None and not reel.copy.exact
           and reel.copy.describe() == "copy from the last ~6 h", repr(reel.copy))
    age_backdated(nav, 3600)
    reel, _, _ = await ask(site, nav=nav)
    expect("and that bound grows with it", reel.copy.describe() == "copy from the last ~7 h",
           repr(reel.copy))


async def test_a_copy_that_comes_back_is_not_dated() -> None:
    old, new = [video(1, oh="a")], [video(1, oh="b")]
    site = Site(Answer(200, old), Answer(200, new), Answer(200, old))
    nav = StoryNavigation()
    await ask(site, nav=nav)
    await ask(site, nav=nav)
    reel, _, _ = await ask(site, nav=nav)
    expect("a copy served again after another one took its place has no known age",
           len(reel.media) == 1 and reel.copy == Copy(), repr(reel.copy))


async def test_a_known_copy_is_never_re_dated_by_timing() -> None:
    playlist = [video(1)]
    site = Site(Answer(200, playlist, delay=LIVE), Answer(200, playlist, delay=LIVE))
    nav = StoryNavigation()
    await ask(site, nav=nav)
    age_backdated(nav, 3 * 3600)
    reel, _, _ = await ask(site, nav=nav)
    expect("the same signatures are the same copy, however slowly they arrive",
           reel.copy.describe() == "copy from ~3 h ago", repr(reel.copy))
    expect("and no second request is spent on it", len(site.posts) == 2, repr(len(site.posts)))


# ---------- between a stored answer's time and a fetch's ---------------------------

async def test_a_quick_fetch_is_recognised_by_asking_again() -> None:
    """Measured 2026-11-10: two reels of 11 items were fetched in 1.2 and
    1.3 s — under the 2.5 s line, so until then "a stored copy, of unknown
    age". Their second ask came back in 0.3 s with the same copy."""
    playlist = [video(1), video(2)]
    site = Site(Answer(200, playlist, delay=BETWEEN), Answer(200, playlist))
    reel, why, _ = await ask(site)
    expect("a fetch quicker than the sure line is served", len(reel.media) == 2 and why is None,
           repr((reel.media, why)))
    expect("the same copy coming back much quicker shows the first ask was the fetch",
           reel.copy.exact and reel.copy.fresh, repr(reel.copy))
    expect("at the price of one more request", len(site.posts) == 2, repr(len(site.posts)))


async def test_a_slow_stored_answer_is_not_taken_for_a_fetch() -> None:
    playlist = [video(1)]
    site = Site(Answer(200, playlist, delay=BETWEEN), Answer(200, playlist, delay=BETWEEN))
    reel, _, _ = await ask(site)
    expect("a second ask that is no quicker: storage on a slow connection, undated",
           len(reel.media) == 1 and reel.copy == Copy(), repr(reel.copy))

    site = Site(Answer(200, playlist, delay=BETWEEN), Answer(200, [video(1, oh="other")]))
    reel, _, _ = await ask(site)
    expect("a second ask with another copy settles nothing: undated",
           len(reel.media) == 1 and reel.copy == Copy(), repr(reel.copy))

    site = Site(Answer(200, playlist, delay=BETWEEN), ConnectionError("reset"))
    reel, why, _ = await ask(site)
    expect("a second ask that fails costs the first answer nothing",
           len(reel.media) == 1 and reel.copy == Copy() and why is None, repr((reel.copy, why)))


# ---------- what is still refused --------------------------------------------------

async def test_an_empty_answer_is_a_failure_not_an_empty_reel() -> None:
    site = Site(Answer(200, text="", delay=LIVE))
    reel, why, _ = await ask(site)
    expect("their failed fetch (an empty body) serves nothing",
           reel.media == [] and reel.copy is None and why == "sent nothing for this highlight",
           repr((reel, why)))
    site = Site(Answer(200, []))
    reel, why, _ = await ask(site)
    expect("and so does the [] they store after it",
           reel.media == [] and why == "sent nothing for this highlight", repr((reel, why)))


async def test_a_copy_with_an_expired_link_is_refused() -> None:
    site = Site(Answer(200, [video(1), video(2, expires=-1.0)]))
    reel, why, _ = await ask(site)
    expect("a link past its `oe` would arrive as a missing item: not served",
           reel.media == [] and why == "only had a copy whose links have expired", repr((reel, why)))
    site = Site(Answer(200, [{"type": "image", "thumbnailUrl": "https://scontent.cdninstagram.com/v/t51.2885-15/plain.jpg?oh=p"}]))
    reel, why, _ = await ask(site)
    expect("a link that names no expiry is taken as it is", len(reel.media) == 1 and why is None,
           repr((reel, why)))


async def test_the_owner_can_still_refuse_old_copies() -> None:
    playlist = [video(1)]
    original = settings.highlight_copy_max_age
    settings.highlight_copy_max_age = 900
    try:
        site = Site(Answer(200, playlist))
        reel, why, _ = await ask(site)
        expect("with a limit set, a copy of unknown age is refused, saying so",
               reel.media == [] and why == "only had a stored copy, of unknown age", repr((reel, why)))

        site = Site(Answer(200, playlist, delay=LIVE), Answer(200, playlist), Answer(200, playlist))
        nav = StoryNavigation()
        reel, why, _ = await ask(site, nav=nav)
        expect("a copy fetched now is within any limit", len(reel.media) == 1 and why is None,
               repr((reel, why)))
        reel, why, _ = await ask(site, nav=nav)
        expect("and so is the same copy minutes later", len(reel.media) == 1 and why is None,
               repr((reel, why)))
        age_backdated(nav, 2000)
        reel, why, _ = await ask(site, nav=nav)
        expect("past the limit it is refused, with its age",
               reel.media == [] and why == "only had a copy from ~33 min ago", repr((reel, why)))
    finally:
        settings.highlight_copy_max_age = original


# ---------- tokens and refusals ----------------------------------------------------

async def test_a_stale_token_is_renewed_once() -> None:
    mismatch = Answer(419, {"message": "CSRF token mismatch."})
    site = Site(mismatch, Answer(200, [video(1)], delay=LIVE))
    reel, why, _ = await ask(site)
    expect("a 419 renews the token and asks again",
           len(reel.media) == 1 and len(site.gets) == 2 and len(site.posts) == 2,
           repr((reel.media, site.gets)))
    site = Site(mismatch, Answer(419, {"message": "CSRF token mismatch."}))
    reel, why, _ = await ask(site)
    expect("but only once", reel.media == [] and why == "answered HTTP 419" and len(site.posts) == 2,
           repr((why, site.posts)))


async def test_every_refusal_says_why() -> None:
    cases = [
        (Site(Answer(403, text="<title>Just a moment...</title>")), "was stopped by Cloudflare's bot check"),
        (Site(Answer(500, text="oops")), "answered HTTP 500"),
        (Site(ConnectionError("reset")), "could not be reached (ConnectionError)"),
        (Site(home=Answer(200, text="<html>no token</html>")), "sent its page without the token its requests need"),
        (Site(home=Answer(503, text="down")), "answered HTTP 503"),
        (Site(home=Answer(403, text="<title>Just a moment...</title>")), "was stopped by Cloudflare's bot check"),
    ]
    for site, reason in cases:
        reel, why, _ = await ask(site)
        expect(f"refused: {reason}", reel.media == [] and why == reason, repr((reel.media, why)))


async def test_entries_are_read_carefully() -> None:
    playlist = [
        {"type": "video", "videoUrl": None, "thumbnailUrl": video(1)["thumbnailUrl"]},  # no video file
        {"type": "video", "videoUrl": "not base64 at all!"},
        {"type": "image", "thumbnailUrl": b64("file:///etc/passwd")},
        "not an entry",
        {"type": "image", "thumbnailUrl": "https://scontent.cdninstagram.com/v/t51.2885-15/plain.jpg?oh=p"},
        image(5),
    ]
    site = Site(Answer(200, playlist, delay=LIVE))
    reel, why, _ = await ask(site)
    expect("only usable links are kept: a video's still is never its video",
           reel.media == [("image", "https://scontent.cdninstagram.com/v/t51.2885-15/plain.jpg?oh=p"),
                          ("image", url_of(playlist[5]))], repr(reel.media))


async def test_one_token_and_two_at_a_time() -> None:
    site = Site(*(Answer(200, [video(n)], delay=LIVE) for n in range(5)))
    nav = StoryNavigation()
    results = await asyncio.gather(*(nav.highlight_media(site, f"1800000000000000{n}") for n in range(5)))
    expect("every reel served", all(len(r.media) == 1 for r in results), repr(results))
    expect("never more than two at once", site.peak == 2, repr(site.peak))
    expect("one token page for all of them", len(site.gets) == 1, repr(site.gets))


def test_the_record_of_reels_is_bounded() -> None:
    nav = StoryNavigation()
    original = storynav_mod._MAX_REELS
    storynav_mod._MAX_REELS = 3
    try:
        for n in range(5):
            nav._date(f"reel{n}", frozenset({f"s{n}"}), True)
        nav._date("reel2", frozenset({"s2"}), False)  # asked again: kept longest
        nav._date("reel5", frozenset({"s5"}), True)
        expect("the least recently asked reels go first",
               list(nav._reels) == ["reel4", "reel2", "reel5"], repr(list(nav._reels)))
    finally:
        storynav_mod._MAX_REELS = original


# ---------- the age in words -------------------------------------------------------

def test_the_note_says_how_old_what_was_handed_over_is() -> None:
    fresh, six, bound, unknown = Copy(120.0, exact=True), Copy(6 * 3600.0, exact=True), Copy(20 * 3600.0), Copy()
    expect("nothing to add for a copy fetched minutes ago", copies_note([fresh]) is None)
    expect("or for none at all", copies_note([]) is None)
    expect("one reel: its copy, exactly",
           copies_note([six]) == "storynavigation's copy from ~6 h ago", repr(copies_note([six])))
    expect("one reel: at most",
           copies_note([bound]) == "storynavigation's copy from the last ~20 h", repr(copies_note([bound])))
    expect("one reel: unknown",
           copies_note([unknown]) == "storynavigation's stored copy, of unknown age",
           repr(copies_note([unknown])))
    expect("several reels: the oldest",
           copies_note([fresh, six, bound]) == "storynavigation's copies, up to ~20 h old",
           repr(copies_note([fresh, six, bound])))
    expect("several reels, some undated",
           copies_note([six, unknown]) == "storynavigation's stored copies, some of unknown age",
           repr(copies_note([six, unknown])))
    expect("several reels, none dated",
           copies_note([unknown, unknown]) == "storynavigation's stored copies, of unknown age",
           repr(copies_note([unknown, unknown])))
    expect("one undated reel among fresh ones is “some”, not all",
           copies_note([fresh, unknown]) == "storynavigation's stored copies, some of unknown age",
           repr(copies_note([fresh, unknown])))
    expect("days are days", Copy(3 * 86400.0, exact=True).describe() == "copy from ~3 days ago",
           Copy(3 * 86400.0, exact=True).describe())


# ---------- the client and the service ---------------------------------------------

async def test_the_client_asks_storynavigation_first() -> None:
    client = StoriesClient()
    await client._session.close()
    playlist = [image(7), video(8)]
    client._session = Site(Answer(200, playlist, delay=LIVE))  # type: ignore[assignment]
    client._fetch_media_html = AsyncMock(return_value="")  # type: ignore[method-assign]
    items = await client.fetch_highlight_items("nasa", "18195781759377100", "Roman")
    expect("its items come back as highlight items",
           [(i.media_type, i.url) for i in items] == [("image", url_of(playlist[0])), ("video", url_of(playlist[1]))]
           and all(i.source == "highlight" and i.highlight_id == "18195781759377100"
                   and i.highlight_title == "Roman" for i in items), repr(items))
    expect("keyed by the media id in the CDN path, as saveinsta's are",
           items[0].pk == "66761457", repr(items[0].pk))
    expect("each with the age of the copy it came from",
           all(i.copy is not None and i.copy.fresh for i in items), repr([i.copy for i in items]))
    expect("and saveinsta is not asked", client._fetch_media_html.await_count == 0)

    client._session = Site(Answer(200, playlist))  # type: ignore[assignment]  # a stored copy
    client._storynav = StoryNavigation()
    items = await client.fetch_highlight_items("nasa", "18195781759377100", "Roman")
    expect("a stored copy is served by the client too, undated",
           len(items) == 2 and all(i.copy == Copy() for i in items), repr(items))
    expect("and saveinsta is still not asked", client._fetch_media_html.await_count == 0)

    client._session = Site(Answer(200, []))  # type: ignore[assignment]  # their failed fetch, stored
    since = time.monotonic()
    items = await client.fetch_highlight_items("nasa", "18195781759377100", "Roman")
    expect("with nothing from storynavigation, saveinsta is asked for the reel",
           items == [] and client._fetch_media_html.await_args.args[0]
           == "https://www.instagram.com/stories/highlights/18195781759377100/",
           repr(client._fetch_media_html.await_args))
    expect("and storynavigation's reason is kept for the error",
           client.highlight_failure_since(since) == "sent nothing for this highlight",
           repr(client.highlight_failure_since(since)))


class Reasons:
    def __init__(self, storynav, saveinsta) -> None:
        self.storynav, self.saveinsta = storynav, saveinsta

    def highlight_failure_since(self, since):
        return self.storynav

    def refusal_since(self, since):
        return self.saveinsta


def service_with(stories) -> MonitorService:
    notifier = AsyncMock()
    notifier.create_forum_topic = AsyncMock(return_value=None)
    return MonitorService(
        instagram=AsyncMock(), hasher=AsyncMock(), notifier=notifier, stories=stories,
    )


def test_the_error_names_every_source() -> None:
    def error(storynav, saveinsta, **kw) -> str:
        return service_with(Reasons(storynav, saveinsta))._media_unavailable(0.0, **kw)

    both = error("sent nothing for this highlight", "Video is private.")
    expect("both sources' reasons, storynavigation first", both == (
        "No anonymous source served it — storynavigation sent nothing for this highlight; "
        "saveinsta refused it: “Video is private.” Highlight names and story status "
        "still work."), both)
    one = error("sent nothing for this highlight", None)
    expect("one reason reads as a sentence", one == (
        "No anonymous source served it — storynavigation sent nothing for this highlight. "
        "Highlight names and story status still work."), one)
    expect("saveinsta's alone still reads",
           "saveinsta refused it: “Video is private.”" in error(None, "Video is private."))
    expect("no reason at all keeps the old message",
           error(None, None) == service_mod._DOWNLOAD_UNAVAILABLE_MSG)
    story = error("sent nothing for this highlight", "Too many requests (HTTP 429).", highlights=False)
    expect("a story's error never quotes storynavigation, which a story is not asked of",
           "storynavigation" not in story and "Too many requests" in story, story)


class OneReel:
    """A stories client with one highlight reel to hand over."""

    def __init__(self, copy: Copy | None) -> None:
        self.copy = copy

    async def fetch_highlight_items(self, username, highlight_id, title):
        return [
            StoryItem(pk=f"{highlight_id}{n}", taken_at=0, media_type="image",
                      url=f"https://cdn.example/{highlight_id}/{n}.jpg", source="highlight",
                      highlight_id=highlight_id, highlight_title=title, copy=self.copy)
            for n in range(2)
        ]

    async def download(self, item, username):
        return Path("item.jpg")

    def highlight_failure_since(self, since):
        return None

    def refusal_since(self, since):
        return None


async def test_a_download_says_which_copy_it_sent() -> None:
    old = service_with(OneReel(Copy(6 * 3600.0, exact=True)))
    result = await old.download_highlights_from_catalog("nasa", {"18195781759377100": "Roman"})
    expect("an old copy is sent", result.get("ok") is True and result.get("count") == 2, repr(result))
    expect("and the result says how old it is",
           result.get("note") == "storynavigation's copy from ~6 h ago", repr(result.get("note")))

    fresh = service_with(OneReel(Copy(30.0, exact=True)))
    result = await fresh.download_highlights_from_catalog("nasa", {"18195781759377100": "Roman"})
    expect("a copy fetched just now needs no note",
           result.get("ok") is True and result.get("note") is None, repr(result))

    saveinsta = service_with(OneReel(None))
    result = await saveinsta.download_highlights_from_catalog("nasa", {"1": "One", "2": "Two"})
    expect("nor do items another source served",
           result.get("ok") is True and result.get("count") == 4 and result.get("note") is None,
           repr(result))

    mixed = service_with(OneReel(Copy()))
    result = await mixed.download_highlights_from_catalog("nasa", {"1": "One", "2": "Two"})
    expect("several undated reels are summed up as that",
           result.get("note") == "storynavigation's stored copies, of unknown age",
           repr(result.get("note")))


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await test_a_fetch_is_served_and_dated_now()
    await test_a_stored_copy_is_served_with_what_is_known()
    await test_its_own_fetch_is_dated_however_long_ago()
    await test_another_reel_s_fetch_forgets_nothing()
    await test_a_copy_that_replaced_another_is_at_most_that_old()
    await test_a_copy_that_comes_back_is_not_dated()
    await test_a_known_copy_is_never_re_dated_by_timing()
    await test_a_quick_fetch_is_recognised_by_asking_again()
    await test_a_slow_stored_answer_is_not_taken_for_a_fetch()
    await test_an_empty_answer_is_a_failure_not_an_empty_reel()
    await test_a_copy_with_an_expired_link_is_refused()
    await test_the_owner_can_still_refuse_old_copies()
    await test_a_stale_token_is_renewed_once()
    await test_every_refusal_says_why()
    await test_entries_are_read_carefully()
    await test_one_token_and_two_at_a_time()
    test_the_record_of_reels_is_bounded()
    test_the_note_says_how_old_what_was_handed_over_is()
    await test_the_client_asks_storynavigation_first()
    test_the_error_names_every_source()
    await test_a_download_says_which_copy_it_sent()

    await dispose_engine()
    if DB_FILE.exists():
        DB_FILE.unlink()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("All storynavigation tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
