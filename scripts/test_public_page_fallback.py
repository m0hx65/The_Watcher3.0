"""Regression tests for the public-page fallback profile source.

When web_profile_info answers 401 there is a second door: the plain profile
page, whose Open Graph block carries the follower/following/post counts. It is
LIVE data from a different endpoint — not a cache — so it satisfies the rule
that the bot never presents old data as current.

What must hold:
- the counts parse out of both og:description formats Instagram serves, in
  either attribute order, abbreviated or not;
- a login wall / block page yields NOTHING rather than zeros;
- the bio is never taken from og:description (it arrives truncated there, and
  storing a truncated bio would fire a bio-change alert every sweep);
- a partial observation never blanks a field it couldn't see, and never
  invents a change;
- the fallback only runs after the API is actually blocked.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.config import settings  # noqa: E402
from app.monitor.change_detector import _is_meaningful_change  # noqa: E402
from app.monitor.instagram import InstagramClient  # noqa: E402
from app.monitor.public_page import (  # noqa: E402
    _to_int,
    parse_public_profile,
)

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


# The classic render.
PAGE_CLASSIC = """
<html><head>
<meta property="og:title" content="Reina Saad (@rein__saad) &#8226; Instagram photos and videos" />
<meta property="og:description" content="1,234 Followers, 567 Following, 89 Posts - See Instagram photos and videos from Reina Saad (@rein__saad)" />
<meta property="og:image" content="https://scontent.cdninstagram.com/v/t51.2885-19/123_n.jpg" />
<script>{"profile_id":"7880052534","other":1}</script>
</head><body></body></html>
"""

# The newer one: bio in the description, attributes reversed, abbreviated counts.
PAGE_MODERN = """
<html><head>
<meta content="1.2M Followers, 2,345 Following, 1,678 Posts - Aya (@alyakarkoutly) on Instagram: &quot;bio text that is truncated here&hellip;&quot;" property="og:description">
<meta content="Aya (@alyakarkoutly) &#8226; Instagram photos and videos" property="og:title">
<meta content="https://scontent.cdninstagram.com/v/t51.2885-19/999_n.jpg" property="og:image">
</head><body></body></html>
"""

# What a login wall looks like: no counts anywhere.
PAGE_LOGIN_WALL = """
<html><head>
<meta property="og:title" content="Instagram" />
<meta property="og:description" content="Create an account or log in to Instagram - Share what you're into with the people who get you." />
</head><body></body></html>
"""


def test_classic_page_parses() -> None:
    parsed = parse_public_profile(PAGE_CLASSIC, "rein__saad")
    expect("classic page parses", parsed is not None)
    assert parsed
    expect("followers", parsed.get("followers_count") == 1234, repr(parsed))
    expect("following", parsed.get("following_count") == 567, repr(parsed))
    expect("posts", parsed.get("posts_count") == 89, repr(parsed))
    expect("full name", parsed.get("full_name") == "Reina Saad", repr(parsed))
    expect("username", parsed.get("username") == "rein__saad", repr(parsed))
    expect("avatar url", "t51.2885-19" in (parsed.get("profile_pic_url") or ""))
    expect("numeric id", parsed.get("instagram_id") == "7880052534", repr(parsed))
    expect("no bio is invented", "biography" not in parsed, repr(parsed))
    expect("no privacy flag is invented", "is_private" not in parsed, repr(parsed))


def test_modern_page_parses() -> None:
    parsed = parse_public_profile(PAGE_MODERN, "alyakarkoutly")
    expect("reversed-attribute page parses", parsed is not None)
    assert parsed
    expect("abbreviated followers", parsed.get("followers_count") == 1_200_000,
           repr(parsed))
    expect("following with separator", parsed.get("following_count") == 2345,
           repr(parsed))
    expect("posts", parsed.get("posts_count") == 1678, repr(parsed))
    expect("full name", parsed.get("full_name") == "Aya", repr(parsed))
    # The description carries a TRUNCATED bio — taking it would alert every
    # sweep against the API's full text.
    expect("the truncated bio is never stored", "biography" not in parsed,
           repr(parsed))


def test_real_sized_page_parses_fast() -> None:
    """The bug that killed the service: a lazy `.*?` under DOTALL rescanned the
    whole document for every non-matching <meta>, so cost grew quadratically
    with page size. A real profile page is megabytes, the parse ran on the
    event loop, the health endpoint stopped answering, and Render killed the
    instance mid-probe.

    So: parse something the size of the real thing, and hold it to a budget a
    quadratic implementation cannot possibly meet.
    """
    # A page shaped like Instagram's: a head full of meta tags that do NOT
    # match (the trigger), the og: block, then megabytes of inlined JSON.
    decoys = "\n".join(
        f'<meta name="decoy-{i}" content="{"x" * 200}">' for i in range(400)
    )
    body_json = '<script type="application/json">' + ('{"k":"' + "v" * 100 + '"},') * 20_000 + "</script>"
    page = (
        "<html><head>" + decoys + PAGE_CLASSIC.split("<head>")[1].split("</head>")[0]
        + "</head><body>" + body_json + "</body></html>"
    )
    size_mb = len(page) / 1_000_000
    expect("the fixture is realistically large", size_mb > 2, f"{size_mb:.1f} MB")

    start = time.monotonic()
    parsed = parse_public_profile(page, "rein__saad")
    elapsed = time.monotonic() - start

    expect("a real-sized page still parses", parsed is not None)
    assert parsed
    expect("and gets the right counts", parsed.get("followers_count") == 1234,
           repr(parsed))
    # The quadratic version took minutes on this shape; anything near a second
    # means the bound is gone again.
    expect("a multi-MB page parses in well under a second", elapsed < 1.0,
           f"{elapsed:.2f}s for {size_mb:.1f} MB")


def test_pathological_markup_is_bounded() -> None:
    """Unterminated attributes and no </head> must not send the scan wandering."""
    page = "<html><head>" + '<meta property="og:description" content="' + "x" * 500_000
    start = time.monotonic()
    parsed = parse_public_profile(page, "someone")
    elapsed = time.monotonic() - start
    expect("malformed markup yields nothing rather than hanging", parsed is None)
    expect("and returns immediately", elapsed < 1.0, f"{elapsed:.2f}s")


def test_login_wall_yields_nothing() -> None:
    expect("a login wall parses to None",
           parse_public_profile(PAGE_LOGIN_WALL, "someone") is None)
    expect("an empty page parses to None",
           parse_public_profile("", "someone") is None)
    expect("junk parses to None",
           parse_public_profile("<html>nope</html>", "someone") is None)


def test_count_parsing() -> None:
    expect("plain", _to_int("1234") == 1234)
    expect("separators", _to_int("1,234,567") == 1234567)
    expect("K", _to_int("12.3K") == 12300)
    expect("M", _to_int("4.5M") == 4500000)
    expect("B", _to_int("1.2B") == 1200000000)
    expect("garbage is None, never 0", _to_int("many") is None)
    expect("empty is None, never 0", _to_int("") is None)


def test_unknown_text_field_is_not_a_change() -> None:
    """The heart of the safety rule: 'I could not see the bio' must never be
    reported as 'the bio was cleared'. A real clearing arrives as "", which
    still reports."""
    expect("unknown bio is not a change",
           not _is_meaningful_change("biography", "some bio", None))
    expect("unknown bio is not a change either way",
           not _is_meaningful_change("biography", None, "some bio"))
    expect("a genuinely cleared bio IS a change",
           _is_meaningful_change("biography", "some bio", ""))
    expect("a real edit is still a change",
           _is_meaningful_change("biography", "old", "new"))


class _MockResponse:
    def __init__(self, status_code: int, body: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text

    def json(self) -> Any:
        return self._body


class _MockSession:
    def __init__(self, handler: Callable[[str, dict, dict], _MockResponse]) -> None:
        self.handler = handler
        self.urls: list[str] = []

    async def get(self, url: str, *, params: Any = None, headers: Any = None):
        self.urls.append(url)
        return self.handler(url, dict(params or {}), dict(headers or {}))

    async def close(self) -> None:
        pass


async def test_fallback_runs_only_after_a_block() -> None:
    def handler(url: str, params: dict, headers: dict) -> _MockResponse:
        if "instagram.com/rein__saad/" in url:
            return _MockResponse(200, text=PAGE_CLASSIC)
        return _MockResponse(401, {})

    session = _MockSession(handler)
    old = settings.ig_proxy_url
    settings.ig_proxy_url = "https://ig-proxy.example.workers.dev"
    try:
        async with InstagramClient(max_retries=5, session=session) as client:
            result = await client.fetch_profile("rein__saad", auth_attempts=1)
    finally:
        settings.ig_proxy_url = old

    expect("a blocked API falls back to the public page", result.success,
           repr(result.error))
    expect("the result is marked partial", result.partial, result.source)
    expect("the counts came through",
           (result.parsed or {}).get("followers_count") == 1234, repr(result.parsed))
    expect("the page was fetched directly, not through the worker",
           any("instagram.com/rein__saad/" in u for u in session.urls),
           repr(session.urls))


async def test_no_fallback_when_the_api_answers() -> None:
    payload = {
        "data": {"user": {
            "username": "rein__saad", "full_name": "R", "biography": "bio",
            "edge_followed_by": {"count": 10}, "edge_follow": {"count": 20},
            "edge_owner_to_timeline_media": {"count": 3},
            "is_private": False, "is_verified": False,
            "is_business_account": False, "id": "1",
        }}
    }
    session = _MockSession(lambda url, p, h: _MockResponse(200, payload))
    async with InstagramClient(max_retries=2, session=session) as client:
        result = await client.fetch_profile("rein__saad")

    expect("a healthy API needs no fallback", result.success and not result.partial,
           result.source)
    expect("exactly one request", len(session.urls) == 1, repr(session.urls))


async def test_blocked_page_keeps_the_original_error() -> None:
    """Both doors shut must report the block — never a success with no data."""
    session = _MockSession(lambda url, p, h: _MockResponse(401, {}, text=PAGE_LOGIN_WALL))
    old = settings.ig_proxy_url
    settings.ig_proxy_url = "https://ig-proxy.example.workers.dev"
    try:
        async with InstagramClient(max_retries=5, session=session) as client:
            result = await client.fetch_profile("nobody", auth_attempts=1)
    finally:
        settings.ig_proxy_url = old

    expect("both doors shut = failure", not result.success)
    expect("the 401 is preserved", result.http_status == 401, repr(result.http_status))
    expect("no invented data", result.parsed is None, repr(result.parsed))


async def main() -> int:
    test_classic_page_parses()
    test_modern_page_parses()
    test_real_sized_page_parses_fast()
    test_pathological_markup_is_bounded()
    test_login_wall_yields_nothing()
    test_count_parsing()
    test_unknown_text_field_is_not_a_change()
    await test_fallback_runs_only_after_a_block()
    await test_no_fallback_when_the_api_answers()
    await test_blocked_page_keeps_the_original_error()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All public-page fallback tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
