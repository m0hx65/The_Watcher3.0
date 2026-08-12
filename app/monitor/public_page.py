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
"""

from __future__ import annotations

import json
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

    window = page[:_SCAN_LIMIT]
    key_at = window.find(_KEY)
    if key_at < 0:
        return None
    raw = _extract_object(window, key_at + len(_KEY))
    if raw is None:
        return None
    try:
        user = json.loads(raw)
    except ValueError:
        logger.debug("Profile payload for @{} was not valid JSON", username)
        return None
    if not isinstance(user, dict):
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
    if isinstance(pk, (str, int)) and str(pk).isdigit():
        parsed["instagram_id"] = str(pk)

    links = user.get("bio_links")
    if isinstance(links, list) and links:
        first = links[0]
        if isinstance(first, dict):
            url = _text(first.get("url"))
            if url:
                parsed["external_url"] = url

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
