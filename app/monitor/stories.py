"""Login-free Instagram story & highlight media downloader (saveinsta.to).

The bot is intentionally 100% anonymous — no Instagram login, cookie, or session.
Instagram's own endpoints return `login_required` for story media, and the old
storiesig.info proxy this used to rely on was shut down. saveinsta.to is a
third-party anonymous downloader (it runs its own session server-side, we never
log in) that still serves public story/highlight media, so we drive its public
token flow the same way its web UI does:

    1. GET  https://saveinsta.to/en/highlights        -> page carries k_exp / k_token
    2. POST https://saveinsta.to/api/userverify        -> issues a per-request cftoken
    3. POST https://saveinsta.to/api/ajaxSearch         -> returns media HTML for the URL

The HTML lists each item as a <li> with a dl.snapcdn.app download link (whose JWT
encodes the real scontent.cdninstagram.com URL). curl_cffi's Chrome TLS
impersonation is required — a plain Python TLS stack gets blocked.

Like any third-party source this can break or rate-limit; every method degrades
gracefully (returns [] / None) so a dead source never breaks the sweep, and the
graphql story/live status + highlight-name detection stays the reliable signal.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from curl_cffi.requests import AsyncSession

from app.config import settings
from app.utils.logger import logger

_BASE = "https://saveinsta.to"
_TOKEN_PAGE = f"{_BASE}/en/highlights"
_VERIFY_URL = f"{_BASE}/api/userverify"
_SEARCH_URL = f"{_BASE}/api/ajaxSearch"
_DL_HOST = "https://dl.snapcdn.app"
_CHROME = "chrome120"

# Page carries `k_exp = "..."` / `k_token = "..."` inline; ajaxSearch needs both.
_K_EXP_RE = re.compile(r'k_exp\s*=\s*"([^"]+)"')
_K_TOKEN_RE = re.compile(r'k_token\s*=\s*"([^"]+)"')
# Each media item is one <li>…</li>; the video/image icon class tells the type.
_LI_RE = re.compile(r"<li\b.*?</li>", re.S)
_ANCHOR_RE = re.compile(
    r'<a\b[^>]*href="(https://dl\.snapcdn\.app[^"]+)"[^>]*title="([^"]*)"', re.S
)
_FALLBACK_DL_RE = re.compile(r'href="(https://dl\.snapcdn\.app[^"]+)"')
# scontent filenames embed a stable numeric media id: /<mediaid>_<ownerid>_…
_MEDIA_ID_RE = re.compile(r"/(\d{6,})_\d{6,}_")
# Instagram serves profile pictures from the t51.*-19 CDN namespace (feed media
# is -15). Used to pick the avatar out of a profile's media listing.
_PROFILE_PIC_RE = re.compile(r"t51\.\d+-19")


@dataclass
class StoryItem:
    pk: str
    taken_at: int           # unix timestamp (0 when the source omits it)
    media_type: str         # "image" or "video"
    url: str                # dl.snapcdn.app download link
    source: str             # "story" or "highlight"
    highlight_id: Optional[str] = None
    highlight_title: Optional[str] = None


class StoriesClient:
    """Async client wrapping the saveinsta.to anonymous downloader."""

    def __init__(self) -> None:
        kwargs: dict = {
            "impersonate": _CHROME,
            "timeout": 30,
            "allow_redirects": True,
        }
        if settings.proxy:
            kwargs["proxy"] = settings.proxy
        self._session = AsyncSession(**kwargs)
        # Cached (k_exp, k_token) from the token page + the monotonic time they
        # stop being reused. Lets repeat fetches skip one of three round-trips.
        self._tokens: Optional[tuple[str, str]] = None
        self._tokens_until: float = 0.0

    async def close(self) -> None:
        await self._session.close()

    # ---------------------------------------------------------------- public

    async def fetch_stories(self, username: str) -> list[StoryItem]:
        """Return all active story items for a public account (login-free)."""
        url = f"https://www.instagram.com/stories/{username}/"
        data = await self._fetch_media_html(url)
        return self._parse_items(data, source="story")

    async def fetch_highlight_items(
        self, username: str, highlight_id: str, title: str
    ) -> list[StoryItem]:
        """Return the story items for one highlight reel (login-free).

        `highlight_id` is the numeric id from Instagram's graphql reel query; the
        saveinsta endpoint expects it as /stories/highlights/<id>/.
        """
        numeric = str(highlight_id).split(":")[-1]
        url = f"https://www.instagram.com/stories/highlights/{numeric}/"
        data = await self._fetch_media_html(url)
        items = self._parse_items(data, source="highlight")
        for item in items:
            item.highlight_id = highlight_id
            item.highlight_title = title
        return items

    async def fetch_posts(self, username: str, limit: int = 12) -> list[StoryItem]:
        """Return recent feed posts/reels for a public account (newest first).

        saveinsta's profile listing is the post grid at full resolution. The
        avatar (t51.*-19 namespace) is skipped; each remaining item is one post's
        main media. Returned as StoryItems with source="post".
        """
        data = await self._fetch_media_html(f"https://www.instagram.com/{username}/")
        items: list[StoryItem] = []
        seen: set[str] = set()
        if not data:
            return items
        for li in _LI_RE.findall(data):
            is_video = "icon-dlvideo" in li
            href = self._pick_download_href(li, is_video)
            if not href:
                continue
            href = href.replace("&amp;", "&")
            cdn_url = self._decode_jwt_url(href)
            if cdn_url and _PROFILE_PIC_RE.search(cdn_url):
                continue  # skip the profile avatar
            pk = self._derive_pk(cdn_url, href)
            if pk in seen:
                continue
            seen.add(pk)
            items.append(
                StoryItem(
                    pk=pk,
                    taken_at=0,
                    media_type="video" if is_video else "image",
                    url=href,
                    source="post",
                )
            )
            if len(items) >= limit:
                break
        return items

    async def fetch_profile_pic_url(self, username: str) -> Optional[str]:
        """Return a login-free HD (up to 1080px) profile-picture download URL.

        saveinsta's profile listing includes the avatar from the t51.*-19 CDN
        namespace at full resolution. Works for public accounts; private accounts
        yield nothing here (their HD avatar needs login), so the caller falls back
        to the web profile_pic_url_hd (320px), which is the anonymous ceiling.
        """
        data = await self._fetch_media_html(f"https://www.instagram.com/{username}/")
        if not data:
            return None
        for li in _LI_RE.findall(data):
            href = self._pick_download_href(li, "icon-dlvideo" in li)
            if not href:
                continue
            href = href.replace("&amp;", "&")
            cdn_url = self._decode_jwt_url(href)
            if cdn_url and _PROFILE_PIC_RE.search(cdn_url):
                return href
        return None

    async def download(self, item: StoryItem, username: str) -> Optional[Path]:
        """Download a story item to disk. Returns the local path on success."""
        ext = ".mp4" if item.media_type == "video" else ".jpg"
        dest_dir = settings.media_path / username / "stories"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{item.pk}{ext}"

        if dest.exists():
            return dest

        try:
            resp = await self._session.get(item.url)
            if resp.status_code != 200 or not resp.content:
                logger.warning(
                    "Bad story download for @{} pk={}: status={}",
                    username, item.pk, resp.status_code,
                )
                return None
            dest.write_bytes(resp.content)
            return dest
        except Exception as exc:
            logger.warning(
                "Failed to download story {} for @{}: {}", item.pk, username, exc
            )
            return None

    async def resolve_user_id(self, username: str) -> Optional[str]:
        """Deprecated PK lookup. saveinsta works off URLs, not numeric ids, and
        the old storiesig PK API is gone, so this always returns None. The id
        backfill path falls back to web_profile_info, which is the reliable
        anonymous source for the numeric id."""
        return None

    # --------------------------------------------------------------- internal

    async def _get_tokens(self) -> Optional[tuple[str, str]]:
        """Return cached (k_exp, k_token), refreshing from the token page when
        the cache has expired. Cached for up to 5 minutes (or k_exp, whichever is
        sooner) — saves one HTTP round-trip on every fetch after the first."""
        if self._tokens and time.monotonic() < self._tokens_until:
            return self._tokens
        page = await self._session.get(_TOKEN_PAGE)
        if page.status_code != 200:
            logger.debug("saveinsta token page HTTP {}", page.status_code)
            return None
        ke = _K_EXP_RE.search(page.text)
        kt = _K_TOKEN_RE.search(page.text)
        if not ke or not kt:
            logger.debug("saveinsta token block not found")
            return None
        self._tokens = (ke.group(1), kt.group(1))
        self._tokens_until = time.monotonic() + 300.0
        return self._tokens

    async def _fetch_media_html(self, target_url: str) -> str:
        """Run the saveinsta token flow for an Instagram URL; return media HTML.

        Returns "" on any failure so callers degrade to an empty result set.
        """
        try:
            tokens = await self._get_tokens()
            if tokens is None:
                return ""
            k_exp, k_token = tokens

            verify = await self._session.post(
                _VERIFY_URL,
                data={"url": target_url},
                headers={
                    "Origin": _BASE,
                    "Referer": f"{_BASE}/en/video",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            cftoken = ""
            if verify.status_code == 200:
                try:
                    cftoken = verify.json().get("token", "") or ""
                except Exception:
                    cftoken = ""

            search = await self._session.post(
                _SEARCH_URL,
                data={
                    "k_exp": k_exp,
                    "k_token": k_token,
                    "q": target_url,
                    "t": "media",
                    "lang": "en",
                    "v": "v2",
                    "cftoken": cftoken,
                },
                headers={
                    "Origin": _BASE,
                    "Referer": _TOKEN_PAGE,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            if search.status_code != 200:
                logger.debug("saveinsta ajaxSearch HTTP {}", search.status_code)
                self._tokens = None  # tokens may be stale — force refresh next time
                return ""
            payload = search.json()
            if payload.get("status") != "ok":
                logger.debug("saveinsta ajaxSearch status={}", payload.get("status"))
                return ""
            return payload.get("data", "") or ""
        except Exception as exc:
            logger.warning("saveinsta fetch failed for {}: {}", target_url, exc)
            return ""

    def _parse_items(self, data: str, *, source: str) -> list[StoryItem]:
        """Parse the media HTML into StoryItems, de-duplicated by media id."""
        items: list[StoryItem] = []
        seen: set[str] = set()
        if not data:
            return items
        for li in _LI_RE.findall(data):
            is_video = "icon-dlvideo" in li
            href = self._pick_download_href(li, is_video)
            if not href:
                continue
            href = href.replace("&amp;", "&")
            cdn_url = self._decode_jwt_url(href)
            pk = self._derive_pk(cdn_url, href)
            if pk in seen:
                continue
            seen.add(pk)
            items.append(
                StoryItem(
                    pk=pk,
                    taken_at=0,
                    media_type="video" if is_video else "image",
                    url=href,
                    source=source,
                )
            )
        return items

    @staticmethod
    def _pick_download_href(li: str, is_video: bool) -> Optional[str]:
        """Choose the right download link inside a <li>.

        Video items expose two links — "Download Thumbnail" (poster) and
        "Download Video" (the mp4); pick the video. Image items have a single
        download link. Falls back to the last dl.snapcdn link found.
        """
        anchors = _ANCHOR_RE.findall(li)
        if is_video:
            for url, title in anchors:
                if "video" in title.lower():
                    return url
        else:
            for url, title in anchors:
                if "thumbnail" not in title.lower():
                    return url
        all_links = _FALLBACK_DL_RE.findall(li)
        return all_links[-1] if all_links else None

    @staticmethod
    def _decode_jwt_url(href: str) -> Optional[str]:
        """Decode the embedded JWT in a snapcdn link to the real cdn URL."""
        token = href.split("token=")[-1]
        for part in token.split("."):
            padded = part + "=" * (-len(part) % 4)
            try:
                decoded = json.loads(base64.urlsafe_b64decode(padded))
            except (binascii.Error, ValueError, json.JSONDecodeError):
                continue
            if isinstance(decoded, dict) and decoded.get("url"):
                return str(decoded["url"])
        return None

    @staticmethod
    def _derive_pk(cdn_url: Optional[str], href: str) -> str:
        """Stable per-item id for dedup: the numeric media id when present,
        otherwise a hash of the media path (query strings carry volatile signing
        params, so they're stripped first)."""
        base = cdn_url or href
        path = base.split("?", 1)[0]
        media_id = _MEDIA_ID_RE.search(path)
        if media_id:
            return media_id.group(1)
        return hashlib.sha1(path.encode("utf-8")).hexdigest()[:24]
