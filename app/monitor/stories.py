"""Instagram stories and highlights downloader via storiesig.info API."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from curl_cffi.requests import AsyncSession

from app.config import settings
from app.utils.logger import logger

_API = "https://api-ig.storiesig.info/api"
_CHROME = "chrome120"


@dataclass
class StoryItem:
    pk: str
    taken_at: int           # unix timestamp
    media_type: str         # "image" or "video"
    url: str
    source: str             # "story" or "highlight"
    highlight_id: Optional[str] = None
    highlight_title: Optional[str] = None


class StoriesClient:
    """Async client wrapping the storiesig.info public API."""

    def __init__(self) -> None:
        kwargs: dict = {
            "impersonate": _CHROME,
            "timeout": 30,
            "allow_redirects": True,
        }
        if settings.proxy:
            kwargs["proxy"] = settings.proxy
        self._session = AsyncSession(**kwargs)

    async def close(self) -> None:
        await self._session.close()

    async def fetch_stories(self, username: str) -> list[StoryItem]:
        """Return all active story items for a public account."""
        url = f"{_API}/story?url=https://www.instagram.com/stories/{username}"
        try:
            resp = await self._session.get(url)
            if resp.status_code != 200:
                logger.debug("Stories API {} for @{}", resp.status_code, username)
                return []
            return self._parse_content(resp.json(), source="story")
        except Exception as exc:
            logger.warning("Failed to fetch stories for @{}: {}", username, exc)
            return []

    async def fetch_highlight_catalog(self, username: str) -> dict[str, str]:
        """Return highlight reel id -> title without downloading every item."""
        pk = await self._get_user_pk(username)
        if not pk:
            return {}
        try:
            resp = await self._session.get(f"{_API}/highlights/{pk}")
            if resp.status_code != 200:
                return {}
            highlights = resp.json().get("result", [])
        except Exception as exc:
            logger.warning("Failed to fetch highlight catalog for @{}: {}", username, exc)
            return {}
        catalog: dict[str, str] = {}
        for h in highlights:
            hid = h.get("id")
            if hid:
                catalog[str(hid)] = str(h.get("title") or "")
        return catalog

    async def fetch_highlight_items(self, username: str, highlight_id: str, title: str) -> list[StoryItem]:
        """Download story items for one highlight reel."""
        items: list[StoryItem] = []
        try:
            resp = await self._session.get(f"{_API}/highlightStories/{highlight_id}")
            if resp.status_code != 200:
                return items
            for item in self._parse_content(resp.json(), source="highlight"):
                item.highlight_id = highlight_id
                item.highlight_title = title
                items.append(item)
        except Exception as exc:
            logger.warning(
                "Failed to fetch highlight {} for @{}: {}", highlight_id, username, exc
            )
        return items

    async def fetch_highlights(self, username: str) -> list[StoryItem]:
        """Return all story items across every highlight reel for a public account."""
        pk = await self._get_user_pk(username)
        if not pk:
            return []

        try:
            resp = await self._session.get(f"{_API}/highlights/{pk}")
            if resp.status_code != 200:
                return []
            highlights = resp.json().get("result", [])
        except Exception as exc:
            logger.warning("Failed to fetch highlight list for @{}: {}", username, exc)
            return []

        items: list[StoryItem] = []
        for h in highlights:
            hid = str(h.get("id", ""))
            title = str(h.get("title") or "")
            if not hid:
                continue
            items.extend(await self.fetch_highlight_items(username, hid, title))
        return items

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
        """Return Instagram numeric user id (pk) for a username, if the account is public."""
        return await self._get_user_pk(username)

    async def _get_user_pk(self, username: str) -> Optional[str]:
        try:
            resp = await self._session.get(f"{_API}/userInfoByUsername/{username}")
            if resp.status_code != 200:
                return None
            return resp.json().get("result", {}).get("user", {}).get("pk")
        except Exception as exc:
            logger.warning("Failed to get PK for @{}: {}", username, exc)
            return None

    def _parse_content(self, data: dict, *, source: str) -> list[StoryItem]:
        items: list[StoryItem] = []
        for raw in data.get("result", []):
            pk = str(raw.get("pk", ""))
            if not pk:
                continue
            taken_at = int(raw.get("taken_at", 0))
            videos = raw.get("video_versions", [])
            if videos:
                items.append(StoryItem(
                    pk=pk, taken_at=taken_at, media_type="video",
                    url=videos[0].get("url", ""), source=source,
                ))
            else:
                candidates = raw.get("image_versions2", {}).get("candidates", [])
                if candidates:
                    items.append(StoryItem(
                        pk=pk, taken_at=taken_at, media_type="image",
                        url=candidates[0].get("url", ""), source=source,
                    ))
        return items
