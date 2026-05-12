"""Profile picture downloader + SHA256 hasher."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings
from app.utils.logger import logger
from app.utils.user_agents import random_user_agent


@dataclass
class HashedMedia:
    sha256: str
    byte_size: int
    content_type: Optional[str]
    local_path: Path
    source_url: str


class MediaHasher:
    """Downloads images, computes SHA256, and stores them on disk."""

    def __init__(self) -> None:
        timeout = httpx.Timeout(settings.request_timeout, connect=10.0)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            proxy=settings.proxy,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "MediaHasher":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def hash_url(self, url: str, username: str) -> Optional[HashedMedia]:
        """Download an image URL and persist it. Returns None on failure."""
        if not url:
            return None
        try:
            response = await self._client.get(
                url,
                headers={
                    "User-Agent": random_user_agent(),
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    "Referer": "https://www.instagram.com/",
                },
            )
        except httpx.HTTPError as exc:
            logger.warning("Failed to download profile picture for @{}: {}", username, exc)
            return None

        if response.status_code != 200 or not response.content:
            logger.warning(
                "Bad image response for @{}: status={}, len={}",
                username, response.status_code, len(response.content or b""),
            )
            return None

        digest = hashlib.sha256(response.content).hexdigest()
        ext = _ext_from_content_type(response.headers.get("Content-Type", "")) or ".jpg"

        account_dir = settings.media_path / username
        account_dir.mkdir(parents=True, exist_ok=True)
        path = account_dir / f"{digest}{ext}"
        if not path.exists():
            path.write_bytes(response.content)

        return HashedMedia(
            sha256=digest,
            byte_size=len(response.content),
            content_type=response.headers.get("Content-Type"),
            local_path=path,
            source_url=url,
        )


def _ext_from_content_type(ct: str) -> Optional[str]:
    ct = (ct or "").split(";")[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(ct)
