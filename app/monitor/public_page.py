"""Profile data from the logged-out profile page's embedded Relay payload.

When `web_profile_info` answers 401 there is a second door: the plain page at
instagram.com/<username>/. What it carries is NOT the Open Graph block — see
below — but the payload the page itself renders from, inlined in a script tag:

    "xig_user_by_username":{"pk":"7880052534","username":"65xim",
      "is_private":true,"biography":"…","full_name":"Mohamad",
      "is_verified":false,"follower_count":118,"following_count":577, …}

Same shape the API returns, same numbers the app shows, and it includes the
privacy flag — so a private account is never mistaken for a public one.

What it does NOT carry, on any capture measured so far: `all_media_count` is
null for public and private accounts alike, so the post count never comes from
here. Nor do reels_count, story_count or is_business exist in the payload. The
avatar it gives is the 150×150 variant — SMALLER than the API's
profile_pic_url_hd (~320px), and it cannot be upsized: the CDN signature covers
the size transform, so editing `stp=` earns a 403 "URL signature mismatch"
(measured 2026-08-12).

What it does carry beyond the profile, for a public account: the whole
highlight catalog when Instagram lists all of it, and the first page of the
post grid (see `_highlight_catalog` and `_grid_empty`, read since 2026-11-04,
when the reel query that used to supply the catalog went login-only).

## Why not the og: tags (this parser's first version, withdrawn 2026-08-12)

Because they are a stale cache, and a page can carry both at once. Verified on
a real profile, one response contained:

    <meta property="og:description" content="118 Followers, 677 Following, …">
    "follower_count":118,"following_count":577          <- and the rendered UI
                                                           said 577, as did the
                                                           Instagram app

Followers and posts agreed; following was 100 stale. Nothing in the meta block
marks it as old, so there is no read-time test that separates a good value from
a bad one — the whole surface had to go. The lesson generalises: a source being
LIVE says nothing about it being CORRECT, and only the embedded payload was
ever checked against ground truth and passed.

Fetched DIRECTLY, never through the Worker: sent that way it carries curl_cffi's
real Chrome TLS fingerprint, while a Worker hop would carry Cloudflare's runtime
fingerprint under a Chrome User-Agent. It also sends no `x-ig-app-id`, so it
reads as a page view rather than a private-API call.

Blocked pages and login walls simply parse to None, leaving the caller's error
intact. Absent fields stay absent (never `None` written over a known value, and
never a `0` invented for a count Instagram omitted).

One page with no payload IS an answer: Instagram's own "isn't available" page,
which it serves — with HTTP 200 — for a username that has no profile (see
`is_unavailable_page`).
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Optional

from app.utils.logger import logger

# The payload sits in an inlined <script> below <head>, so the scan window is
# large — but bounded, because an endless or malformed document must not turn
# into unbounded work on a page this parser only ever sees when something is
# already broken.
_SCAN_LIMIT = 4_000_000
# One profile object is a few KB; this ceiling only stops a runaway brace scan
# on malformed input.
_OBJECT_LIMIT = 200_000
_KEY = '"xig_user_by_username":'
# The page carries the key more than once — see `_profile_object`. Two
# occurrences on every capture so far; the cap only bounds a malformed page.
_MAX_OCCURRENCES = 8
# Fields only the PROFILE block has. The posts-grid block that shares its key
# carries pk, id and the timeline connection, and none of these.
_PROFILE_MARKERS = ("follower_count", "following_count", "is_private")

# What the payload is trusted for. Everything else Instagram's API returns
# (reels_count, story_count, is_business) is absent here and must stay absent
# rather than being guessed — see the partial-data rules in the caller.
PARTIAL_FIELDS = (
    "username",
    "full_name",
    "biography",
    "followers_count",
    "following_count",
    "posts_count",
    "is_private",
    "is_verified",
    "profile_pic_url",
    "external_url",
    "instagram_id",
)


# The route a page was served for, as the logged-out web app names it in the
# page's own bootstrap data. A profile is "profilePage", the login page
# "CAAIGLoginHomepage", and the page for a username with no profile is the
# error route.
_ERROR_ROUTE_RE = re.compile(
    r'"polarisRouteConfig":\{[^{}]*"pageID":"httpErrorPage"'
    r'|"canonicalRouteName":"comet\.igweb\.PolarisErrorRoute"'
)


def is_unavailable_page(page: str) -> bool:
    """True for Instagram's own "Profile isn't available" page: what it
    serves for a username that has no profile — renamed away from,
    deactivated, deleted, or never taken.

    It comes with HTTP 200, not 404 (measured 2026-11-10 from a home
    connection: a made-up username and @8dvuu, a target that had answered
    until that day, got the same 516 KB page, and the bot's own log had read
    it as "no profile payload — login wall or markup change" twice that
    afternoon). So the status says nothing; the page says it itself, in the
    route it was served for: `PolarisErrorRoot`, pageID "httpErrorPage",
    where a profile's page is "profilePage" and carries the payload.

    The two refusals this must not be confused with look different, on pages
    captured the same day. A login wall for the IP is a redirect to
    /accounts/login/ (pageID "CAAIGLoginHomepage"). A profile Instagram shows
    only to logged-in visitors (an age-restricted brand account) is still
    "profilePage", with `"xig_user_by_username":null`. Neither matches.
    """
    return bool(page) and _ERROR_ROUTE_RE.search(page, 0, _SCAN_LIMIT) is not None


def strip_bidi(text: str) -> str:
    """Drop invisible bidi control marks, keeping the visible text identical.

    Instagram wraps RTL names in LRM/RLM marks in some surfaces and not others,
    which diffed as a name "changing" to a character-for-character identical
    value. Format characters (Unicode category Cf) are exactly this class.
    """
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def _extract_object(page: str, start: int) -> Optional[str]:
    """Return the JSON object beginning at `start`, by brace matching.

    A regex can't do this safely — the payload contains nested objects and
    braces inside strings — so this walks the text once, tracking string and
    escape state, and stops at the matching close brace or the size ceiling.
    """
    if start >= len(page) or page[start] != "{":
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, min(len(page), start + _OBJECT_LIMIT)):
        char = page[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return page[start : index + 1]
    return None


def _blocks(window: str, username: str) -> list[dict[str, Any]]:
    """Every `xig_user_by_username` block on the page, in page order.

    The page preloads two Relay queries under that same key: the profile
    (PolarisLoggedOutDesktopWWWProfileRootContentQuery — counts, bio, flags)
    and the posts grid (…ProfilePostsTabContentQuery — just pk, id and the
    timeline). Instagram streams them in either order: measured 2026-10-18,
    @65xim and @whos.lisianna came profile-first, @leen_m204, @jeeda.younes
    and @talia.takheen grid-first, and @95rxi both ways on two fetches a
    minute apart. Taking the first occurrence failed every grid-first page
    as "no profile payload", the same accounts sweep after sweep.
    """
    blocks: list[dict[str, Any]] = []
    start = 0
    for _ in range(_MAX_OCCURRENCES):
        key_at = window.find(_KEY, start)
        if key_at < 0:
            break
        start = key_at + len(_KEY)
        raw = _extract_object(window, start)
        if raw is None:
            continue
        try:
            block = json.loads(raw)
        except ValueError:
            logger.debug("A payload block for @{} was not valid JSON", username)
            continue
        if isinstance(block, dict):
            blocks.append(block)
    return blocks


def _highlight_catalog(user: dict[str, Any]) -> Optional[dict[str, str]]:
    """The highlight catalog (reel id -> title) when the page lists ALL of it.

    `lox_highlights_connection` carries the same highlight-reel ids as the
    reel query: the canonical ones, which resolve at
    instagram.com/stories/highlights/<id>/ to this account. Verified
    2026-11-04: @whos.lisianna's page listed the 3 reels the reel query had
    stored the day before, and its first id opened as "☆ = @whos.lisianna".
    It is the catalog source that still answers: by then the reel query said
    "require_login" even from a home IP.

    The connection is paginated, so it is a catalog only when Instagram says
    there is no next page. A partial list would read as highlights being
    removed. None means unknown, never empty.
    """
    connection = user.get("lox_highlights_connection")
    if not isinstance(connection, dict):
        return None
    edges = connection.get("edges")
    page_info = connection.get("page_info")
    if not isinstance(edges, list) or not isinstance(page_info, dict):
        return None
    if page_info.get("has_next_page") is not False:
        return None
    catalog: dict[str, str] = {}
    for edge in edges:
        node = edge.get("node") if isinstance(edge, dict) else None
        highlight_id = node.get("id") if isinstance(node, dict) else None
        if not isinstance(highlight_id, (str, int)) or not _ascii_digits(highlight_id):
            return None  # a list with an unreadable entry is not a complete one
        catalog[str(highlight_id)] = _text(node.get("title")) or ""
    return catalog


def _grid_empty(blocks: list[dict[str, Any]], pk: Optional[str]) -> Optional[bool]:
    """Whether this account's post grid, as the page lists it, is empty.

    The grid block carries the newest posts and Instagram's own
    `has_next_page`: @natgeo's page listed 12 with a next page, while
    @whos.lisianna's and @opscn1's listed none and no next page (both have 0
    posts). True only for that explicit "none, and nothing after"; False when
    any post is listed; None when the block is missing or is not this
    account's.
    """
    if not pk:
        return None
    for block in blocks:
        connection = block.get("polaris_ordered_timeline_connection")
        if not isinstance(connection, dict) or str(block.get("pk")) != pk:
            continue
        edges = connection.get("edges")
        page_info = connection.get("page_info")
        if not isinstance(edges, list) or not isinstance(page_info, dict):
            return None
        if edges:
            return False
        return True if page_info.get("has_next_page") is False else None
    return None


def _ascii_digits(value: Any) -> bool:
    """An id Instagram sends is plain ASCII digits; `str.isdigit` would also
    pass superscripts and other scripts' digits."""
    text = str(value)
    return text.isascii() and text.isdigit()


def _text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = strip_bidi(value)
    return cleaned


def _count(value: Any) -> Optional[int]:
    """Counts only. `null` means Instagram didn't say — never read as zero.

    `all_media_count` is null on EVERY logged-out page view measured so far —
    private (@65xim) and public (@b_rand_s, 1,006 posts by its own og: tag)
    alike. So the post count is simply not available from this door; a `0` here
    would read as "they deleted every post" on an account that has a thousand.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def parse_public_profile(page: str, username: str) -> Optional[dict[str, Any]]:
    """Parse the page's embedded profile payload, or None.

    None means the payload wasn't there — a login wall, a block page, or a
    markup change. The caller must treat that as "no data", never as zeros.
    """
    if not page:
        return None

    blocks = _blocks(page[:_SCAN_LIMIT], username)
    user = next(
        (b for b in blocks if any(k in b for k in _PROFILE_MARKERS)), None
    )
    if user is None:
        return None

    parsed: dict[str, Any] = {}

    handle = _text(user.get("username"))
    # The page is the authority on its own handle — it reflects a rename the
    # stored username hasn't caught up with yet.
    parsed["username"] = (handle or username).strip().lower()

    for field, key in (
        ("full_name", "full_name"),
        ("biography", "biography"),
        ("profile_pic_url", "profile_pic_url"),
    ):
        value = _text(user.get(key))
        if value is not None:
            parsed[field] = value

    for field, key in (
        ("followers_count", "follower_count"),
        ("following_count", "following_count"),
        ("posts_count", "all_media_count"),
    ):
        value = _count(user.get(key))
        if value is not None:
            parsed[field] = value

    for field in ("is_private", "is_verified"):
        value = user.get(field)
        if isinstance(value, bool):
            parsed[field] = value

    # `pk` is the numeric id the rest of the bot keys on (the sibling `id` is a
    # different, app-scoped identifier — storing it would break the reel query).
    pk = user.get("pk")
    if isinstance(pk, (str, int)) and _ascii_digits(pk):
        parsed["instagram_id"] = str(pk)

    # Story status. `latest_reel_media` is the newest active story's timestamp
    # and 0 when there is none — verified 2026-09-05 against the reel query on
    # the same accounts (@natgeo 1788624868 ↔ has_public_story True, @nasa 0 ↔
    # False). A logged-out viewer sees null for private accounts, so it only
    # exists for public ones; and it knows a story, not a live broadcast.
    latest_reel = user.get("latest_reel_media")
    if isinstance(latest_reel, int) and not isinstance(latest_reel, bool):
        parsed["has_public_story"] = latest_reel > 0

    links = user.get("bio_links")
    if isinstance(links, list) and links:
        first = links[0]
        if isinstance(first, dict):
            url = _text(first.get("url"))
            if url:
                parsed["external_url"] = url

    # Highlights and the post grid, for PUBLIC accounts only: a logged-out
    # viewer sees a private account's lists as empty whatever they hold.
    if parsed.get("is_private") is False:
        catalog = _highlight_catalog(user)
        if catalog is not None:
            parsed["highlights"] = catalog
        grid_empty = _grid_empty(blocks, parsed.get("instagram_id"))
        if grid_empty is not None:
            parsed["grid_empty"] = grid_empty

    # A payload with no counts and no privacy flag isn't a profile — refuse it
    # rather than hand back a near-empty dict that looks like a reading.
    if not any(
        key in parsed
        for key in ("followers_count", "following_count", "is_private")
    ):
        return None

    logger.debug(
        "Page payload for @{}: followers={} following={} posts={} private={}",
        username,
        parsed.get("followers_count"),
        parsed.get("following_count"),
        parsed.get("posts_count"),
        parsed.get("is_private"),
    )
    return parsed
