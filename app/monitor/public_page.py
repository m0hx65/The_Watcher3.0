"""Profile stats from Instagram's public profile page, as a fallback source.

When `web_profile_info` answers 401 there is still a second, quite different
door: the plain profile page at instagram.com/<username>/. Every logged-out
render of it carries an Open Graph block for link previews —

    <meta property="og:description"
          content="1,234 Followers, 567 Following, 89 Posts - Name (@user)…">
    <meta property="og:title" content="Name (@user) • Instagram photos…">
    <meta property="og:image" content="https://scontent…jpg">

— which is exactly the handful of numbers this bot alerts on. It is worth
trying precisely because it is *unlike* the API call that just failed:

- no `x-ig-app-id` header, so it reads as a browser opening a page rather than
  a client calling a private API;
- fetched DIRECTLY with curl_cffi's Chrome impersonation, so the TLS/HTTP2
  fingerprint genuinely is Chrome's. Through the Worker it would be
  Cloudflare's runtime fingerprint carrying a header claiming to be Chrome —
  a contradiction that is itself a bot signal;
- served as a public link-preview surface, which Meta has every reason to keep
  answerable to crawlers.

It may still be blocked, in which case the parse simply yields nothing and the
caller keeps its original failure. What it must never do is *invent* data: the
fields it cannot see stay absent (see PARTIAL_FIELDS) rather than defaulting,
so a fallback observation can't be diffed into a phantom "bio removed".
"""

from __future__ import annotations

import html
import re
from typing import Any, Optional

from app.utils.logger import logger

# A profile page runs to several megabytes, almost all of it inlined JSON and
# script below the fold. Everything this parser wants is in <head>, so the scan
# is capped: cut at </head> when it appears, else at this many characters. It
# bounds the work regardless of what Instagram serves — including an endless or
# malformed document, which is exactly the shape that hurts a regex.
_HEAD_LIMIT = 256_000
# The numeric id lives in the inlined JSON below <head>, so it gets its own
# (larger, still bounded) window. The patterns for it are simple and linear.
_ID_SCAN_LIMIT = 1_000_000

# "1,234 Followers, 567 Following, 89 Posts" — with abbreviations on big
# accounts ("1.2M Followers"). The separator between them is a plain comma in
# every locale we ask for (Accept-Language is en-US).
_COUNTS_RE = re.compile(
    r"([\d.,]+\s*[KMB]?)\s+Followers?,\s*"
    r"([\d.,]+\s*[KMB]?)\s+Following,\s*"
    r"([\d.,]+\s*[KMB]?)\s+Posts?",
    re.IGNORECASE,
)
# Meta tags are matched ONE AT A TIME, bounded by the tag's own closing ">".
#
# The obvious pattern — one regex spanning content=… and property=… with a lazy
# `(.*?)` under DOTALL — is a trap on a document this size. For every `<meta`
# that doesn't match, the lazy group expands across the ENTIRE remaining
# document before failing, so the cost is quadratic in page size. On a real
# Instagram profile page (megabytes, hundreds of meta tags) that pinned the CPU
# for minutes, and since it runs on the event loop it stalled the health
# endpoint until Render killed the instance.
#
# So: find each tag with a length-bounded pattern that cannot cross `>`, then
# read attributes from within that one short string. Linear, and the work per
# tag has a hard ceiling.
_META_TAG_RE = re.compile(r"<meta\b[^>]{0,4000}>", re.IGNORECASE)
_OG_KEY_RE = re.compile(
    r"(?:property|name)\s*=\s*[\"']og:(description|title|image)[\"']", re.IGNORECASE
)
_CONTENT_RE = re.compile(
    r"content\s*=\s*(?:\"([^\"]{0,4000})\"|'([^']{0,4000})')", re.IGNORECASE
)
# "Name (@username) • Instagram photos and videos" -> "Name"
_TITLE_RE = re.compile(r"^(.*?)\s*\(@([^)]+)\)")
# The numeric id appears in the page's embedded JSON under a few different
# keys depending on the render. Any of them is the same id.
_ID_RES = (
    re.compile(r'"profile_id"\s*:\s*"(\d+)"'),
    re.compile(r'"owner"\s*:\s*\{\s*"id"\s*:\s*"(\d+)"'),
    re.compile(r'"user_id"\s*:\s*"(\d+)"'),
)

# What this source can see. Everything else — biography, is_private,
# is_verified, is_business, external_url, reels_count, story_count — is left
# out entirely. The og:description does carry a bio fragment, but a TRUNCATED
# one ("bio text…"), and storing that would fire a bio-change alert on every
# sweep against the full text from the API.
PARTIAL_FIELDS = (
    "username",
    "full_name",
    "followers_count",
    "following_count",
    "posts_count",
    "profile_pic_url",
    "instagram_id",
)


# Bidi/formatting controls. Instagram's og:title wraps RTL display names in
# direction marks (LRM/RLM/isolates) so the preview renders correctly; the API
# returns the same name without them. Left in, an Arabic name alternating
# between the two sources reads as "Full name changed  لِ → ‎لِ‎" — two strings
# that are visibly identical. They carry no meaning for change detection.
_BIDI_MARKS = dict.fromkeys(
    (0x200E, 0x200F, 0x061C, 0x2066, 0x2067, 0x2068, 0x2069, 0x202A,
     0x202B, 0x202C, 0x202D, 0x202E, 0xFEFF, 0x200B),
    None,
)


def strip_bidi(text: Optional[str]) -> Optional[str]:
    """Remove invisible direction/zero-width marks from a display string."""
    if not text:
        return text
    return text.translate(_BIDI_MARKS).strip()


def _to_int(raw: str) -> Optional[int]:
    """Parse a count as rendered for humans: 1,234 / 12.3K / 4.5M / 1.2B.

    An abbreviated value is approximate by nature — "1.2M" is anything from
    1,150,000 to 1,249,999 — but it is the same approximation every time, so it
    still moves when the real number moves enough to matter.
    """
    text = raw.strip().replace(",", "").replace(" ", "")
    if not text:
        return None
    multiplier = 1
    if text[-1].upper() in ("K", "M", "B"):
        multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[text[-1].upper()]
        text = text[:-1]
    try:
        return int(round(float(text) * multiplier))
    except ValueError:
        return None


def _meta_tags(page: str) -> dict[str, str]:
    """Extract og:description / og:title / og:image, whatever the attribute order.

    One bounded pass: each tag is matched on its own and its attributes read
    from that short string, so attribute order costs nothing and no pattern
    can wander across the document. Stops as soon as all three are in hand —
    they sit in <head>, so a normal page exits within the first few hundred
    tags without touching the megabytes of body below.
    """
    found: dict[str, str] = {}
    for tag in _META_TAG_RE.finditer(page):
        chunk = tag.group(0)
        key_match = _OG_KEY_RE.search(chunk)
        if key_match is None:
            continue
        key = key_match.group(1).lower()
        if key in found:
            continue
        content = _CONTENT_RE.search(chunk)
        if content is None:
            continue
        found[key] = html.unescape(content.group(1) or content.group(2) or "")
        if len(found) == 3:
            break
    return found


def parse_public_profile(page: str, username: str) -> Optional[dict[str, Any]]:
    """Parse a public profile page into a PARTIAL profile dict, or None.

    None means the page carried no counts — a login wall, a block page, or a
    markup change. The caller must treat that as "no data", never as zeros.
    """
    if not page:
        return None

    head_end = page.find("</head>")
    head = page[: head_end if 0 <= head_end <= _HEAD_LIMIT else _HEAD_LIMIT]

    tags = _meta_tags(head)
    description = tags.get("description", "")
    counts = _COUNTS_RE.search(description)
    if not counts:
        return None

    followers = _to_int(counts.group(1))
    following = _to_int(counts.group(2))
    posts = _to_int(counts.group(3))
    if followers is None and following is None and posts is None:
        return None

    parsed: dict[str, Any] = {
        "username": username,
        "followers_count": followers,
        "following_count": following,
        "posts_count": posts,
    }

    title = _TITLE_RE.match(strip_bidi(tags.get("title", "")) or "")
    if title:
        full_name = strip_bidi(title.group(1))
        if full_name:
            parsed["full_name"] = full_name
        # The page is the authority on its own handle — it reflects a rename
        # the stored username hasn't caught up with yet.
        handle = title.group(2).strip().lower()
        if handle:
            parsed["username"] = handle

    image = tags.get("image")
    if image:
        parsed["profile_pic_url"] = image

    id_window = page[:_ID_SCAN_LIMIT]
    for pattern in _ID_RES:
        found = pattern.search(id_window)
        if found:
            parsed["instagram_id"] = found.group(1)
            break

    logger.debug(
        "Public page parsed for @{}: followers={} following={} posts={}",
        username, followers, following, posts,
    )
    return parsed
