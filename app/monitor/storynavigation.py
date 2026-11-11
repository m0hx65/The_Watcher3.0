"""storynavigation.com: highlight media, each copy with its age.

saveinsta has answered "Video is private." for every highlight of every
public account since 2026-10-23, and Instagram's own logged-out highlight page
carries no items. storynavigation's viewer still plays highlights: its page
posts {highlightId} to /get-highlight-stories with the Laravel CSRF token any
visitor gets with the page, and receives the reel's items as Instagram CDN
links (images at full size). No captcha or other bot check sits in that flow
and no login of ours is involved: the bot makes the requests the page makes.

What it answers is rarely live. The first request for a reel fetches it from
Instagram, and that answer is then served from storage for a day or more with
the same signed links every time. Nothing in an answer, or in its headers,
says how old it is. Measured 2026-11-10 on six reels of 9 to 38 items: the
fetch took 1.2, 1.3, 2.7, 3.8, 4.2 and 6.3 s, a stored copy 0.2-0.8 s (eleven
answers). (On 2026-11-05 the fetches measured took 3.5-11.5 s.)

Until 2026-11-10 an answer counted only when it had taken 2.5 s or more, or
was the copy this client had fetched that way within 15 minutes. From
2026-11-07 to 11-10 that served 3 of about 24 listings of the two reels a
sweep tracks, and neither of two downloads asked for by hand:
- a sweep's own request is what makes storynavigation fetch a reel, so the
  copy is then stored for the next day and every later ask found it there;
- a fetch can take half of 2.5 s, and such a live answer was refused as "a
  stored copy, of unknown age" — as were that copy's next day of answers;
- one reel's live fetch dropped the record of every other reel's, so a copy
  this client had fetched itself read as "of unknown age" as well.

Now every answer that carries media is served, and what is worked out about
it is the age of its copy, which the caller says when it hands the media to a
person (see Copy):
- exact, when this client saw the fetch: its request took the seconds a fetch
  takes — 2.5 s or more, or, for a time between a stored answer's and that, a
  second ask that came back with the same copy markedly quicker;
- at most, when the copy replaced one this client was served before: it was
  fetched since then;
- unknown otherwise: a first look at a reel, answered from storage.
A copy is the same copy when its links carry the same signatures (`oh`), since
Instagram signs every fetch anew — whatever the answer's timing.

Still refused, each with its reason: an empty answer (their failed fetch, and
the [] they then store, is never "this highlight is empty"), a copy with a
link that has expired (`oe`: 32-36 h after the fetch for most videos, 104-108 h
for images, on the copies measured), and anything but an HTTP 200.
HIGHLIGHT_COPY_MAX_AGE, when set, also refuses a copy older than that many
seconds, or one of unknown age.

Its own highlight LIST is never used: for a small account it answered []
while Instagram's page listed three. The catalog comes from Instagram; this is
asked for one known reel.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import parse_qs, urlsplit

from app.config import settings
from app.monitor.health import ERROR, OK, STORYNAV, fetch_health
from app.utils.logger import logger

_BASE = "https://storynavigation.com"
_HOME = f"{_BASE}/"
_HIGHLIGHT_URL = f"{_BASE}/get-highlight-stories"
_CSRF_RE = re.compile(r'<meta name="csrf-token" content="([^"]+)"')
_CHALLENGE_RE = re.compile(r"Just a moment|cf-chl|challenge-platform", re.I)
# The session cookie a token is bound to lives two hours (Max-Age=7200).
_TOKEN_TTL = 5400.0
# Their backend takes seconds per reel it has to fetch from Instagram; two at
# a time keeps an account's whole catalog from landing on it at once.
_CONCURRENCY = 2
# An answer that took this long was fetched from Instagram by this request.
_LIVE_MIN_SECONDS = 2.5
# One quicker than this came from storage: stored answers took 0.2-0.8 s, the
# quickest fetch 1.2 s.
_STORED_MAX_SECONDS = 0.9
# Between the two the reel is asked for once more: when the first request was
# the fetch, the second gets the same copy from storage, this much quicker
# (the two quick fetches measured were 0.9 and 1.0 s slower than their second
# ask). What this can get wrong is a stored copy this client has never been
# served, whose answer was held up in transit by that much: read as fetched
# now. A copy it has been served before is never re-dated by timing.
_LIVE_MARGIN_SECONDS = 0.7
# A copy fetched this recently is current: no note about its age is needed.
FRESH_SECONDS = 900.0
# A link this close to its expiry is not handed on to be downloaded.
_EXPIRY_MARGIN_SECONDS = 120.0
# How many reels' copies are remembered (the least recently asked go first).
_MAX_REELS = 2048


class _NoToken(Exception):
    """The page that issues the CSRF token did not give one."""


@dataclass(frozen=True)
class Copy:
    """How old the copy behind one answer is, as far as this client can tell.

    `age` is in seconds. With `exact` it is measured: this client saw
    storynavigation fetch the copy from Instagram. Without, it is the most the
    copy can be: the time since this client was last served a different copy
    of the reel. None when nothing says."""

    age: Optional[float] = None
    exact: bool = False

    @property
    def fresh(self) -> bool:
        return self.age is not None and self.age <= FRESH_SECONDS

    def describe(self) -> str:
        """The copy in words for the user, e.g. "copy from ~6 h ago"."""
        if self.age is None:
            return "stored copy, of unknown age"
        if self.exact:
            return f"copy from {_span(self.age)} ago"
        return f"copy from the last {_span(self.age)}"


@dataclass(frozen=True)
class Reel:
    """One reel as it was served: (media type, URL) per item, in order, and
    the age of the copy they came from. No media: nothing was served, and the
    reason is kept for failure_since."""

    media: list[tuple[str, str]] = field(default_factory=list)
    copy: Optional[Copy] = None


@dataclass
class _Seen:
    """The copy of one reel this client was last served."""

    signatures: frozenset[str]
    seen_at: float
    # When storynavigation fetched it, if this client saw that happen.
    fetched_at: Optional[float] = None
    # Else when this client was last served the copy before it: this one is
    # newer than that.
    after: Optional[float] = None
    # The signatures of the copy before it.
    earlier: Optional[frozenset[str]] = None

    def holds(self, signatures: frozenset[str]) -> bool:
        return signatures in (self.signatures, self.earlier)


def copies_note(copies: Iterable[Copy]) -> Optional[str]:
    """One phrase on how old the highlight media just handed over is, for
    whoever asked for it: "storynavigation's copy from ~6 h ago". None when
    every copy is fresh, since there is then nothing to add."""
    copies = list(copies)
    stale = [copy for copy in copies if not copy.fresh]
    if not stale:
        return None
    if len(copies) == 1:
        return f"storynavigation's {stale[0].describe()}"
    undated = sum(1 for copy in copies if copy.age is None)
    if undated == len(copies):
        return "storynavigation's stored copies, of unknown age"
    if undated:
        return "storynavigation's stored copies, some of unknown age"
    oldest = max(copy.age for copy in stale if copy.age is not None)
    return f"storynavigation's copies, up to {_span(oldest)} old"


class StoryNavigation:
    """One highlight reel's media from storynavigation.com, with the age of
    the copy it came from."""

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._token_until = 0.0
        self._token_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(_CONCURRENCY)
        # highlight id -> the copy this client was last served, least recently
        # asked first.
        self._reels: dict[str, _Seen] = {}
        # (monotonic time, reason) of the last reel it did not serve.
        self._last_failure: Optional[tuple[float, str]] = None

    def failure_since(self, since: float) -> Optional[str]:
        """Why the last reel asked at or after `since` (a time.monotonic()
        value) came back with nothing usable; None when none failed since."""
        if self._last_failure is not None and self._last_failure[0] >= since:
            return self._last_failure[1]
        return None

    async def highlight_media(self, session: Any, highlight_id: str) -> Reel:
        """The reel's items and the age of the copy they came from; a Reel
        with no media when nothing was served, the reason kept for
        failure_since. `session` is the caller's curl_cffi session, so a test
        that swaps the caller's session swaps this one too."""
        numeric = str(highlight_id).split(":")[-1]
        media: list[tuple[str, str, str, Optional[float]]] = []
        expired = fetched_now = False
        try:
            async with self._slots:
                response, took = await self._ask(session, numeric)
                if response.status_code == 419:
                    # Laravel's "CSRF token mismatch": the session behind the
                    # token ended early. A fresh token, and one more try.
                    self._token = None
                    response, took = await self._ask(session, numeric)
                if response.status_code == 200:
                    media = _media(response)
                    expired = _expired(media)
                if media and not expired:
                    fetched_now = await self._fetched_now(session, numeric, media, took)
        except _NoToken as exc:
            return self._fail(numeric, str(exc))
        except Exception as exc:
            fetch_health.record(STORYNAV, ERROR)
            return self._fail(numeric, f"could not be reached ({type(exc).__name__})")
        if response.status_code != 200:
            fetch_health.record_status(STORYNAV, response.status_code)
            return self._fail(numeric, _refusal(response))
        if not media:
            fetch_health.record(STORYNAV, ERROR)
            return self._fail(numeric, "sent nothing for this highlight")
        if expired:
            fetch_health.record(STORYNAV, ERROR)
            return self._fail(numeric, "only had a copy whose links have expired")
        copy = self._date(numeric, _signatures(media), fetched_now)
        # It answered, with the reel: a refusal below is this bot's own rule.
        fetch_health.record(STORYNAV, OK)
        limit = max(0, settings.highlight_copy_max_age)
        if limit and (copy.age is None or copy.age > limit):
            return self._fail(numeric, f"only had a {copy.describe()}")
        if not copy.fresh:
            logger.debug(
                "storynavigation served highlight {} from a {}", numeric, copy.describe()
            )
        return Reel([(kind, url) for kind, url, _, _ in media], copy)

    async def _ask(self, session: Any, numeric: str) -> tuple[Any, float]:
        token = await self._csrf_token(session)
        start = time.monotonic()
        response = await session.post(
            _HIGHLIGHT_URL,
            headers={
                "X-CSRF-TOKEN": token,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": _BASE,
                "Referer": _HOME,
            },
            data=json.dumps({"highlightId": f"highlight:{numeric}"}),
        )
        return response, time.monotonic() - start

    async def _csrf_token(self, session: Any) -> str:
        if self._token and time.monotonic() < self._token_until:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_until:
                return self._token
            page = await session.get(_HOME)
            if page.status_code != 200:
                fetch_health.record_status(STORYNAV, page.status_code)
                raise _NoToken(_refusal(page))
            match = _CSRF_RE.search(page.text or "")
            if match is None:
                fetch_health.record(STORYNAV, ERROR)
                raise _NoToken(
                    _refusal(page) if _CHALLENGE_RE.search(page.text or "")
                    else "sent its page without the token its requests need"
                )
            self._token, self._token_until = match.group(1), time.monotonic() + _TOKEN_TTL
            return self._token

    async def _fetched_now(
        self,
        session: Any,
        numeric: str,
        media: list[tuple[str, str, str, Optional[float]]],
        took: float,
    ) -> bool:
        """Whether this very request made storynavigation fetch the reel.

        A copy this client has been served before is that copy however long
        its answer took. A new one is this request's when it took the seconds
        a fetch takes. For a time in between — a fetch can take 1.2 s, and a
        stored answer on a slow connection nearly as long — the reel is asked
        for again: after a fetch the second answer is the same copy from
        storage, and markedly quicker.
        """
        signatures = _signatures(media)
        known = self._reels.get(numeric)
        if known is not None and known.holds(signatures):
            return False
        if took >= _LIVE_MIN_SECONDS:
            return True
        if took < _STORED_MAX_SECONDS:
            return False
        try:
            again, took_again = await self._ask(session, numeric)
        except Exception:
            return False  # undecided: the copy stays undated, and is served
        return (
            again.status_code == 200
            and _signatures(_media(again)) == signatures
            and took - took_again >= _LIVE_MARGIN_SECONDS
        )

    def _date(self, numeric: str, signatures: frozenset[str], fetched_now: bool) -> Copy:
        """The age of the copy just served, and the record of it."""
        now = time.monotonic()
        known = self._reels.pop(numeric, None)
        if known is None or known.signatures != signatures:
            # A copy that comes BACK after another one was served is not the
            # single store this reasoning relies on: it stays undated.
            back = known is not None and known.earlier == signatures
            known = _Seen(
                signatures,
                now,
                fetched_at=now if fetched_now else None,
                after=(
                    known.seen_at
                    if known is not None and not fetched_now and not back
                    else None
                ),
                earlier=known.signatures if known is not None else None,
            )
        known.seen_at = now
        self._reels[numeric] = known
        while len(self._reels) > _MAX_REELS:
            self._reels.pop(next(iter(self._reels)))
        if known.fetched_at is not None:
            return Copy(now - known.fetched_at, exact=True)
        if known.after is not None:
            return Copy(now - known.after)
        return Copy()

    def _fail(self, numeric: str, reason: str) -> Reel:
        # Once per new reason: a sweep asks for every tracked reel, and the
        # same answer for each of them would bury the log.
        changed = self._last_failure is None or self._last_failure[1] != reason
        self._last_failure = (time.monotonic(), reason)
        (logger.info if changed else logger.debug)(
            "storynavigation did not serve highlight {}: {}", numeric, reason
        )
        return Reel()


def _refusal(response: Any) -> str:
    """Why an answer carries no media, in words for the user."""
    if _CHALLENGE_RE.search(getattr(response, "text", "") or ""):
        return "was stopped by Cloudflare's bot check"
    return f"answered HTTP {response.status_code}"


def _media(response: Any) -> list[tuple[str, str, str, Optional[float]]]:
    """(type, URL, signature, expiry) per usable entry of the playlist. Not
    JSON — an empty body is how a failed fetch from Instagram comes back — is
    none."""
    try:
        entries = response.json()
    except Exception:
        return []
    if not isinstance(entries, list):
        return []
    return [item for item in map(_item, entries) if item is not None]


def _item(entry: Any) -> Optional[tuple[str, str, str, Optional[float]]]:
    """One playlist entry. A video's own file only: its thumbnail is a still,
    not the video. The signature (`oh`) tells one copy of the reel from
    another, since Instagram signs every fetch anew; `oe` is when the link
    stops working (unix seconds, in hex)."""
    if not isinstance(entry, dict):
        return None
    kind = "video" if entry.get("type") == "video" else "image"
    url = _decode(entry.get("videoUrl" if kind == "video" else "thumbnailUrl"))
    if url is None:
        return None
    query = parse_qs(urlsplit(url).query)
    try:
        expires: Optional[float] = float(int(query["oe"][0], 16))
    except (KeyError, ValueError):
        expires = None
    return kind, url, query.get("oh", [url])[0], expires


def _signatures(media: list[tuple[str, str, str, Optional[float]]]) -> frozenset[str]:
    return frozenset(signature for _, _, signature, _ in media)


def _expired(media: list[tuple[str, str, str, Optional[float]]]) -> bool:
    """Whether any of the copy's links has run out: a reel that would arrive
    with items missing is not that reel."""
    deadline = time.time() + _EXPIRY_MARGIN_SECONDS
    return any(expires is not None and expires <= deadline for *_, expires in media)


def _decode(value: Any) -> Optional[str]:
    """A link as the page gets it: base64 of the URL (a plain one passes)."""
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("https://"):
        return value
    try:
        url = base64.b64decode(value, validate=True).decode("ascii")
    except (binascii.Error, ValueError):
        return None
    return url if url.startswith("https://") else None


def _span(seconds: float) -> str:
    if seconds < 3600:
        return f"~{max(1, round(seconds / 60))} min"
    if seconds < 172800:
        return f"~{round(seconds / 3600)} h"
    return f"~{round(seconds / 86400)} days"
