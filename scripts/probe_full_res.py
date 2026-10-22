"""Measure what each anonymous source serves for one post. Run from home.

    python scripts/probe_full_res.py https://www.instagram.com/p/<code>/
    python scripts/probe_full_res.py @username

For a post it lists, image by image, the measured pixel size from:
  * saveinsta: what the bot delivers today (the decoded CDN url's `stp`
    transform and the downloaded file), and
  * Instagram's own logged-out post route, the query yt-dlp 2026.10.21 uses
    (PolarisLoggedOutDesktopWWWPostRootContentQuery). Its
    image_versions2.candidates hold every rendition Instagram stores, the
    largest included, and carousel_media holds every slide. When the query
    answers nothing, the same payload is read from the logged-out
    /p/<code>/ page.

For @username it reports whether the logged-out profile page (the one the
home fetcher already sends the bot) carries shortcodes and full-size
renditions in its posts-grid block (polaris_ordered_timeline_connection).

Needs a residential IP: Render's and Cloudflare's get the login page.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from curl_cffi.requests import AsyncSession  # noqa: E402
from PIL import Image  # noqa: E402

from app.monitor.stories import StoriesClient  # noqa: E402

IG = "https://www.instagram.com"
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_DOC_ID = "27130156389949648"
_FRIENDLY = "PolarisLoggedOutDesktopWWWPostRootContentQuery"
_API_HEADERS = {
    "X-IG-App-ID": "936619743392459",
    "X-ASBD-ID": "359341",
    "X-IG-WWW-Claim": "0",
    "Origin": IG,
    "Accept": "*/*",
}
_LSD_RES = (
    re.compile(r'\["LSD",\[\],\{"token":"([^"]+)"'),
    re.compile(r'id="__eqmc"[^>]*>\{[^<]*?"l":"([^"]+)"'),
)
_POST_RE = re.compile(r"instagram\.com/(?:[^/?#]+/)?(?:p|reels?|tv)/([A-Za-z0-9_-]+)")
_DECODER = json.JSONDecoder()


def media_id(shortcode: str) -> str:
    """A post's numeric id from its shortcode (private posts carry 28 extra
    characters, which are not part of the id)."""
    if len(shortcode) > 28:
        shortcode = shortcode[:-28]
    value = 0
    for char in shortcode:
        value = value * 64 + _ALPHABET.index(char)
    return str(value)


def stp_of(url: str) -> str:
    return parse_qs(urlparse(url).query).get("stp", ["-"])[0]


def _area(candidate: dict[str, Any]) -> int:
    return int(candidate.get("width") or 0) * int(candidate.get("height") or 0)


def _objects_after(text: str, key: str) -> list[Any]:
    """Every JSON value that follows `key` in the page, at any size."""
    found, start = [], 0
    while (at := text.find(key, start)) >= 0:
        start = at + len(key)
        try:
            value, _end = _DECODER.raw_decode(text, start)
        except ValueError:
            continue
        found.append(value)
    return found


async def measure(session: AsyncSession, url: str) -> str:
    try:
        resp = await session.get(url, headers={"Referer": f"{IG}/"})
    except Exception as exc:
        return f"download failed: {exc}"
    if resp.status_code != 200:
        return f"HTTP {resp.status_code}"
    try:
        with Image.open(io.BytesIO(resp.content)) as img:
            width, height = img.size
    except Exception:
        return f"{len(resp.content) // 1024} KB, not an image"
    return f"{width}×{height} ({len(resp.content) // 1024} KB)"


async def probe_saveinsta(post_url: str) -> None:
    print("\n== saveinsta (what the bot sends today) ==")
    client = StoriesClient()
    try:
        items = client._parse_items(
            await client._fetch_media_html(post_url), source="post"
        )
        if not items:
            print("  no items: blocked, private, or its markup changed")
        for number, item in enumerate(items, 1):
            cdn = client._decode_jwt_url(item.url) or ""
            print(
                f"  #{number} {item.media_type}: stp={stp_of(cdn)}"
                f"  ->  {await measure(client._session, item.url)}"
            )
    finally:
        await client.close()


def _post_from_page(page: str) -> Optional[dict[str, Any]]:
    for media in _objects_after(page, '"xig_polaris_media":'):
        if isinstance(media, dict) and media.get("if_not_gated_logged_out"):
            return media["if_not_gated_logged_out"]
    return None


async def probe_instagram(session: AsyncSession, code: str) -> None:
    print("\n== Instagram's logged-out post route ==")
    target = media_id(code)
    home = await session.get(f"{IG}/")
    lsd = next((m.group(1) for rx in _LSD_RES if (m := rx.search(home.text))), None)
    ruling = await session.get(
        f"{IG}/api/v1/web/get_ruling_for_content/",
        params={"content_type": "MEDIA", "target_id": target},
        headers=_API_HEADERS,
    )
    try:
        ruling_ok = ruling.json().get("status") == "ok"
    except ValueError:
        ruling_ok = False
    csrf = session.cookies.get("csrftoken")
    print(
        f"  home HTTP {home.status_code}, LSD token {'found' if lsd else 'MISSING'}; "
        f"ruling HTTP {ruling.status_code} ({'ok' if ruling_ok else 'not ok'}); "
        f"csrftoken {'set' if csrf else 'MISSING'}"
    )

    headers = {
        **_API_HEADERS,
        "X-FB-Friendly-Name": _FRIENDLY,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{IG}/p/{code}/",
    }
    if lsd:
        headers["X-FB-LSD"] = lsd
    if csrf and ruling_ok:
        headers["X-CSRFToken"] = csrf
    resp = await session.post(
        f"{IG}/api/graphql",
        headers=headers,
        data={
            "lsd": lsd or "",
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": _FRIENDLY,
            "server_timestamps": "true",
            "variables": json.dumps({"media_id": target}, separators=(",", ":")),
            "doc_id": _DOC_ID,
        },
    )
    product = None
    try:
        media = (resp.json().get("data") or {}).get("xig_polaris_media") or {}
        product = media.get("if_not_gated_logged_out")
    except (ValueError, AttributeError):
        pass
    print(f"  graphql HTTP {resp.status_code}: {'payload' if product else 'no payload'}")

    if not product:
        page = await session.get(f"{IG}/p/{code}/")
        product = _post_from_page(page.text)
        print(
            f"  /p/{code}/ page HTTP {page.status_code}, {len(page.content) // 1024} KB,"
            f" landed on {urlparse(str(page.url)).path}: "
            f"{'payload' if product else 'no payload'}"
        )
    if not product:
        return

    slides = [s for s in (product.get("carousel_media") or [product]) if isinstance(s, dict)]
    print(f"  {len(slides)} slide(s)")
    for number, slide in enumerate(slides, 1):
        candidates = (slide.get("image_versions2") or {}).get("candidates") or []
        if not candidates:
            print(f"  #{number}: no image candidates")
            continue
        best = max(candidates, key=_area)
        kind = "video" if slide.get("video_versions") else "image"
        print(
            f"  #{number} {kind}: original {slide.get('original_width')}×"
            f"{slide.get('original_height')}, {len(candidates)} renditions, largest "
            f"{best.get('width')}×{best.get('height')} stp={stp_of(best.get('url', ''))}"
            f"  ->  {await measure(session, best['url'])}"
        )


async def probe_grid(session: AsyncSession, username: str) -> None:
    print(f"\n== logged-out profile page of @{username}: posts-grid block ==")
    page = await session.get(f"{IG}/{username}/")
    print(
        f"  HTTP {page.status_code}, {len(page.content) // 1024} KB,"
        f" landed on {urlparse(str(page.url)).path}"
    )
    grids = [
        block["polaris_ordered_timeline_connection"]
        for block in _objects_after(page.text, '"xig_user_by_username":')
        if isinstance(block, dict) and block.get("polaris_ordered_timeline_connection")
    ]
    if not grids:
        print("  no posts-grid block on this page")
        return
    edges = grids[0].get("edges") or []
    nodes = [e.get("node") or {} for e in edges if isinstance(e, dict)]
    print(f"  {len(nodes)} post(s); node keys: {sorted(nodes[0]) if nodes else '-'}")
    for node in nodes:
        candidates = (node.get("image_versions2") or {}).get("candidates") or []
        best = max(candidates, key=_area, default={})
        slides = len(node.get("carousel_media") or []) or node.get("carousel_media_count") or 1
        print(
            f"   {node.get('code') or node.get('shortcode') or '?'}:"
            f" media_type={node.get('media_type')} slides={slides}"
            f" largest={best.get('width')}×{best.get('height')}"
        )


async def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    arg = sys.argv[1].strip()
    async with AsyncSession(impersonate="chrome120", timeout=30) as session:
        if arg.startswith("@"):
            await probe_grid(session, arg[1:])
            return 0
        found = _POST_RE.search(arg)
        if not found:
            print("Give a post/reel URL or @username.")
            return 2
        code = found.group(1)
        await probe_saveinsta(f"{IG}/p/{code}/")
        await probe_instagram(session, code)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
