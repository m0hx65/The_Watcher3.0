"""Profile picture downloader + SHA256 hasher."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

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


def _strip_cdn_size(url: str) -> Optional[str]:
    """Return a size-constraint-free CDN URL, or None if no modification was made.

    Instagram/Facebook CDN URLs encode a target resolution in two ways:
      - Path segment:  /v/t51.2885-19/s320x320/HASH_n.jpg   (older format)
      - Query param:   ?stp=dst-jpg_s320x320_e35             (newer format)

    Removing the constraint asks the CDN for the original stored image rather
    than a downscaled thumbnail.  The technique is the same one used by
    InstaRaider (github.com/akurtovic/InstaRaider).
    """
    if "fbcdn.net" not in url and "cdninstagram.com" not in url:
        return None

    modified = False

    # 1. Strip /s{W}x{H}/ from the URL path  (e.g. /s150x150/, /s320x320/)
    new_url = re.sub(r"/s\d+x\d+/", "/", url)
    if new_url != url:
        modified = True
        url = new_url

    # 2. Strip size specs from the `stp` query parameter
    #    e.g. stp=dst-jpg_s320x320_e35  →  stp=dst-jpg_e35
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    if "stp" in params:
        new_stp = []
        for stp in params["stp"]:
            stripped = re.sub(r"_?[sp]\d+x\d+", "", stp).strip("_")
            if stripped != stp:
                modified = True
            new_stp.append(stripped or stp)
        if modified:
            params["stp"] = new_stp
            url = urlunparse(parsed._replace(query=urlencode(params, doseq=True)))

    return url if modified else None


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
        """Download an image URL and persist it. Returns None on failure.

        Tries to fetch a size-constraint-free (full-resolution) version of the
        URL first.  Falls back to the original URL if that attempt fails.
        """
        if not url:
            return None

        # Try the full-resolution version of the CDN URL first.
        hd_url = _strip_cdn_size(url)
        if hd_url:
            result = await self._fetch_and_store(hd_url, username)
            if result is not None:
                logger.debug(
                    "HD image downloaded for @{}: {} bytes ({})",
                    username, result.byte_size, hd_url,
                )
                return result
            logger.debug("HD URL attempt failed for @{}, falling back to original", username)

        return await self._fetch_and_store(url, username)

    async def _fetch_and_store(self, url: str, username: str) -> Optional[HashedMedia]:
        """Download one URL, hash it, and persist to disk."""
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
