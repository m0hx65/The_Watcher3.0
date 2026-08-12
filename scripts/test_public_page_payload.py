"""Regression tests for the page-payload profile parser.

Built from a REAL captured response for @65xim (2026-08-12). That capture is
the whole point: the same page carried two different "following" numbers —

    og:description ........ 118 Followers, 677 Following, 0 Posts   <- stale
    embedded payload ...... "follower_count":118,"following_count":577
    rendered UI ........... 577 following
    Instagram app ......... 577 following

The first parser read the meta block and put 677 on the card. These tests pin
the parser to the payload, and assert the stale number never comes back.

Runs fully offline.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.monitor.public_page import (  # noqa: E402
    parse_public_profile,
    strip_bidi,
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


# Trimmed from the real capture: the stale og block AND the correct payload,
# in the order Instagram serves them.
REAL_PAGE = r"""<!DOCTYPE html><html><head>
<meta property="og:title" content="Mohamad (&#064;65xim) &#x2022; Instagram photos and videos" />
<meta property="og:description" content="118 Followers, 677 Following, 0 Posts - See Instagram photos and videos from Mohamad (&#064;65xim)" />
</head><body>
<script type="application/json" data-sjs>{"require":[["RelayPrefetchedStreamCache","next",[],["adp_PolarisLoggedOutDesktopWWWProfileRootContentQueryRelayPreloader_6a7c",{"__bbox":{"complete":true,"result":{"data":{"xig_user_by_username":{"pk":"7880052534","username":"65xim","profile_pic_url":"https:\/\/scontent-sea5-1.cdninstagram.com\/v\/t51.82787-19\/556145850.jpg?stp=dst-jpg&_nc_cat=105","is_private":true,"is_unpublished":false,"latest_reel_media":null,"biography":"فـلـس palestine\nCyber security engineer","full_name":"Mohamad","is_verified":false,"account_badges":[],"bio_links":[],"linked_fb_info":null,"is_memorialized":false,"pronouns":[],"follower_count":118,"following_count":577,"all_media_count":null,"id":"17841407816045006","lox_highlights_connection":{"edges":[],"page_info":{"end_cursor":null,"has_next_page":false}},"is_coppa_enforced":false,"has_any_clips":false}},"extensions":{"is_final":true}},"sequence_number":0}}]]]}</script>
</body></html>"""

# A second real capture (2026-08-12), this one a PUBLIC account with 1,006 posts.
# It settles two questions the private capture could not:
#   * og: is stale here too — "47 Following" against the payload's 44;
#   * all_media_count is null for a PUBLIC account as well, so the post count is
#     simply not available from this door.
REAL_PAGE_PUBLIC = r"""<!DOCTYPE html><html><head>
<meta property="og:title" content="Brands Brands (&#064;b_rand_s) &#x2022; Instagram photos and videos" />
<meta property="og:description" content="11K Followers, 47 Following, 1,006 Posts - See Instagram photos and videos from Brands Brands (&#064;b_rand_s)" />
</head><body>
<script type="application/json" data-sjs>{"require":[["RelayPrefetchedStreamCache","next",[],["adp_PolarisLoggedOutDesktopWWWProfileRootContentQueryRelayPreloader_6a7c",{"__bbox":{"complete":true,"result":{"data":{"xig_user_by_username":{"pk":"54222996077","username":"b_rand_s","profile_pic_url":"https:\/\/scontent-sea5-1.cdninstagram.com\/v\/t51.75761-19\/504076967_17992596584804078_3391207303348888903_n.jpg?stp=dst-jpg_s150x150_tt6&_nc_cat=108&efg=eyJ2ZW5jb2RlX3RhZyI6InByb2ZpbGVfcGljLnd3dy42MjUuQzMifQ%3D%3D&oh=00_AQFVWV8n91K3M9I6duD-rpyw6s69LHNdXNcLy7l3rOmayA&oe=6A824646","is_private":false,"is_unpublished":false,"latest_reel_media":1786476076,"biography":"-online store\nWe ship happiness all over Syria","full_name":"Brands Brands","is_verified":false,"account_badges":[],"bio_links":[],"is_memorialized":false,"pronouns":[],"follower_count":10939,"following_count":44,"all_media_count":null,"id":"17841454122337874","is_coppa_enforced":false,"has_any_clips":true}},"extensions":{"is_final":true}},"sequence_number":0}}]]]}</script>
</body></html>"""

LOGIN_WALL = """<html><head>
<meta property="og:title" content="Instagram" />
<meta property="og:description" content="Create an account or log in to Instagram." />
</head><body>Log in</body></html>"""


def test_real_capture_uses_the_payload_not_the_meta_tag() -> None:
    parsed = parse_public_profile(REAL_PAGE, "65xim")
    expect("the real capture parses", parsed is not None)
    assert parsed
    expect("following is the payload's 577, NOT og:description's 677",
           parsed.get("following_count") == 577, repr(parsed.get("following_count")))
    expect("followers matches ground truth", parsed.get("followers_count") == 118,
           repr(parsed.get("followers_count")))
    expect("the numeric id is pk, not the app-scoped id",
           parsed.get("instagram_id") == "7880052534", repr(parsed.get("instagram_id")))
    expect("full name", parsed.get("full_name") == "Mohamad", repr(parsed.get("full_name")))
    expect("username", parsed.get("username") == "65xim", repr(parsed.get("username")))
    expect("the bio comes through in full, not truncated",
           "Cyber security engineer" in (parsed.get("biography") or ""),
           repr(parsed.get("biography")))
    expect("the avatar URL is unescaped",
           (parsed.get("profile_pic_url") or "").startswith("https://scontent"),
           repr(parsed.get("profile_pic_url")))


def test_privacy_flag_is_read_not_guessed() -> None:
    """The bug this closes: an ABSENT flag became bool(None) -> False, and a
    private account was called public and swept for stories."""
    parsed = parse_public_profile(REAL_PAGE, "65xim")
    assert parsed
    expect("private accounts are reported private", parsed.get("is_private") is True)
    expect("verification flag is read", parsed.get("is_verified") is False)


def test_null_count_is_absent_never_zero() -> None:
    """all_media_count is null for this private account. Zero would read as
    'they deleted every post'."""
    parsed = parse_public_profile(REAL_PAGE, "65xim")
    assert parsed
    expect("a null post count is absent, not 0", "posts_count" not in parsed,
           repr(parsed.get("posts_count")))


def test_public_capture_uses_the_payload_too() -> None:
    """The public account settles that og: staleness isn't a private-account
    quirk, and that the post count is missing from this door for everyone."""
    parsed = parse_public_profile(REAL_PAGE_PUBLIC, "b_rand_s")
    expect("the public capture parses", parsed is not None)
    assert parsed
    expect("following is the payload's 44, NOT og:description's 47",
           parsed.get("following_count") == 44, repr(parsed.get("following_count")))
    expect("followers is the exact 10939, not og's rounded '11K'",
           parsed.get("followers_count") == 10939, repr(parsed.get("followers_count")))
    expect("a public account is reported public", parsed.get("is_private") is False)
    expect("the numeric id is pk, not the app-scoped id",
           parsed.get("instagram_id") == "54222996077", repr(parsed.get("instagram_id")))
    # og: claims 1,006 posts; the payload says null. Absent beats a wrong 0 —
    # and beats trusting the tag that is demonstrably stale on the same page.
    expect("the post count is ABSENT even for a public account with 1,006 posts",
           "posts_count" not in parsed, repr(parsed.get("posts_count")))


def test_the_pages_avatar_is_the_small_variant() -> None:
    """Pins what this door can and cannot do for profile pictures: it serves
    the 150x150 variant, which is smaller than the API's profile_pic_url_hd
    (~320px). Measured 2026-08-12: stripping the size out of the URL to get a
    bigger one returns 403 'URL signature mismatch' — the signature covers the
    transform, so there is no upgrade path here."""
    parsed = parse_public_profile(REAL_PAGE_PUBLIC, "b_rand_s")
    assert parsed
    url = parsed.get("profile_pic_url") or ""
    expect("the avatar URL is unescaped", url.startswith("https://scontent"), repr(url[:40]))
    expect("and is the 150x150 variant", "s150x150" in url, repr(url))


def test_login_wall_and_junk_yield_nothing() -> None:
    expect("a login wall parses to None",
           parse_public_profile(LOGIN_WALL, "someone") is None)
    expect("an empty page parses to None", parse_public_profile("", "x") is None)
    expect("junk parses to None", parse_public_profile("<html>no</html>", "x") is None)
    expect("a truncated payload parses to None",
           parse_public_profile('..."xig_user_by_username":{"pk":"1","foo', "x") is None)
    expect("a payload with no counts is refused",
           parse_public_profile('"xig_user_by_username":{"username":"x"}', "x") is None)


def test_bidi_marks_are_stripped() -> None:
    """og:title wrapped RTL names in invisible marks, so a name diffed as
    changing to a character-for-character identical value."""
    wrapped = "‎رالا‎"
    expect("bidi marks are removed", strip_bidi(wrapped) == "رالا", repr(strip_bidi(wrapped)))
    expect("visible text is untouched", strip_bidi("Mohamad") == "Mohamad")


def test_large_page_is_bounded() -> None:
    """The parser only ever runs when something is already broken, so a huge or
    malformed document must not become unbounded work (a previous version
    pinned the CPU until Render killed the instance)."""
    page = REAL_PAGE + ("<div>" + "x" * 200 + "</div>") * 20_000
    size_mb = len(page) / 1_000_000
    expect("the fixture is realistically large", size_mb > 3, f"{size_mb:.1f} MB")
    start = time.monotonic()
    parsed = parse_public_profile(page, "65xim")
    elapsed = time.monotonic() - start
    expect("a multi-MB page still parses", parsed is not None)
    expect("and stays well under a second", elapsed < 1.0,
           f"{elapsed:.2f}s for {size_mb:.1f} MB")

    # Unbalanced braces must terminate, not scan forever.
    start = time.monotonic()
    runaway = '"xig_user_by_username":{' + '{"a":1,' * 100_000
    expect("unbalanced braces yield None",
           parse_public_profile(runaway, "x") is None)
    expect("and return immediately", time.monotonic() - start < 1.0)


def main() -> int:
    test_real_capture_uses_the_payload_not_the_meta_tag()
    test_privacy_flag_is_read_not_guessed()
    test_null_count_is_absent_never_zero()
    test_public_capture_uses_the_payload_too()
    test_the_pages_avatar_is_the_small_variant()
    test_login_wall_and_junk_yield_nothing()
    test_bidi_marks_are_stripped()
    test_large_page_is_bounded()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All page-payload tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
