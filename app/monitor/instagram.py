"""Instagram web_profile_info client with retry behavior.

This client is locked to the single public profile request shape below.
No alternate Instagram endpoints, cookies, or profile-media downloads are used
by this client. HTTP/2 is required.

    GET /api/v1/users/web_profile_info/?username=<u> HTTP/2
    Host: www.instagram.com
    x-ig-app-id: 936619743392459

See `scripts/test_ig_fetch.py` for a stand-alone repro.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.config import settings
from app.utils.logger import logger

INSTAGRAM_HOST = "www.instagram.com"
PROFILE_PATH = "/api/v1/users/web_profile_info/"
PROFILE_URL = f"https://{INSTAGRAM_HOST}{PROFILE_PATH}"
FORCED_IG_APP_ID = "936619743392459"
REQUIRED_HTTP_VERSION = "HTTP/2"


class InstagramError(Exception):
    """Base exception for Instagram fetcher problems."""


class RateLimited(InstagramError):
    pass


class UserNotFound(InstagramError):
    pass


@dataclass
class ProfileFetchResult:
    """Outcome of a single profile fetch attempt."""

    username: str
    http_status: int
    parsed: Optional[dict[str, Any]] = None
    raw_response: Optional[dict[str, Any]] = None
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.http_status == 200 and self.parsed is not None


_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
_SEC_CH_UA = (
    '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"'
)
_SEC_CH_UA_FULL = (
    '"Chromium";v="148.0.7778.167", "Google Chrome";v="148.0.7778.167", '
    '"Not/A)Brand";v="99.0.0.0"'
)


def _build_headers(username: str) -> dict[str, str]:
    """Headers for the single allowed public profile endpoint. No cookies."""
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9,ar;q=0.8,de;q=0.7,nl;q=0.6,zh-CN;q=0.5,zh;q=0.4",
        "host": INSTAGRAM_HOST,
        "priority": "u=1, i",
        "referer": f"https://www.instagram.com/{username}",
        "sec-ch-prefers-color-scheme": "dark",
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-full-version-list": _SEC_CH_UA_FULL,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-platform-version": '"19.0.0"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": _CHROME_UA,
        "x-asbd-id": "359341",
        "x-ig-app-id": FORCED_IG_APP_ID,
        "x-ig-max-touch-points": "0",
        "x-ig-www-claim": "0",
        "x-requested-with": "XMLHttpRequest",
    }


def _parse_user(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Normalize Instagram payload into a flat dict matching our snapshot fields."""
    try:
        user = payload["data"]["user"]
    except (KeyError, TypeError):
        return None
    if not user:
        return None

    def deep(*path: str) -> Any:
        node: Any = user
        for key in path:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node

    highlights = deep("highlight_reel_count")
    reels = deep("edge_felix_video_timeline", "count")

    return {
        "username": user.get("username"),
        "full_name": user.get("full_name"),
        "biography": user.get("biography"),
        "followers_count": deep("edge_followed_by", "count"),
        "following_count": deep("edge_follow", "count"),
        "posts_count": deep("edge_owner_to_timeline_media", "count"),
        "reels_count": reels,
        "story_count": highlights,
        "is_private": user.get("is_private"),
        "is_verified": user.get("is_verified"),
        "is_business": user.get("is_business_account"),
        "profile_pic_url": user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
        "external_url": user.get("external_url"),
        "instagram_id": user.get("id"),
    }


class InstagramClient:
    """Async client for the web_profile_info endpoint."""

    def __init__(
        self,
        max_retries: int = 8,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.max_retries = max_retries
        timeout = httpx.Timeout(
            settings.request_timeout, connect=10.0, read=settings.request_timeout
        )
        # HTTP/2 is required; HTTP/1.1 responses are rejected below.
        client_kwargs: dict[str, Any] = {
            "http2": True,
            "timeout": timeout,
            "follow_redirects": True,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        else:
            client_kwargs["proxy"] = settings.proxy
        self._client = httpx.AsyncClient(**client_kwargs)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "InstagramClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def fetch_profile(self, username: str) -> ProfileFetchResult:
        """Fetch a profile with intelligent retry/backoff."""
        username = username.strip().lstrip("@")
        last_status = 0
        last_error: Optional[str] = None

        for attempt in range(1, self.max_retries + 1):
            jitter = random.uniform(0.0, 1.5)
            try:
                response = await self._client.get(
                    PROFILE_URL,
                    params={"username": username},
                    headers=_build_headers(username),
                )
                last_status = response.status_code

                if response.http_version != REQUIRED_HTTP_VERSION:
                    last_error = (
                        f"Unexpected HTTP version {response.http_version}; "
                        f"{REQUIRED_HTTP_VERSION} is required"
                    )
                    logger.warning(
                        "{} for @{} on attempt {}/{}",
                        last_error,
                        username,
                        attempt,
                        self.max_retries,
                    )
                    return ProfileFetchResult(
                        username=username,
                        http_status=response.status_code,
                        error=last_error,
                    )

                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except ValueError:
                        last_error = "Invalid JSON in response"
                        logger.warning(
                            "Non-JSON 200 for {} on attempt {}", username, attempt
                        )
                    else:
                        parsed = _parse_user(payload)
                        if parsed is None:
                            return ProfileFetchResult(
                                username=username,
                                http_status=404,
                                raw_response=payload,
                                error="User not found in response",
                            )
                        return ProfileFetchResult(
                            username=username,
                            http_status=200,
                            parsed=parsed,
                            raw_response=payload,
                        )

                if response.status_code == 404:
                    return ProfileFetchResult(
                        username=username,
                        http_status=404,
                        error="User not found",
                    )

                if response.status_code == 429:
                    delay = min(60.0, (2 ** attempt) * 4.0 + jitter)
                    logger.warning(
                        "Rate limited on @{} (attempt {}/{}). Sleeping {:.1f}s",
                        username, attempt, self.max_retries, delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                if 500 <= response.status_code < 600:
                    delay = min(30.0, (2 ** attempt) + jitter)
                    logger.warning(
                        "Server error {} on @{} (attempt {}/{}). Sleeping {:.1f}s",
                        response.status_code, username, attempt, self.max_retries, delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                # 401/403 etc — Instagram blocking or auth required
                logger.warning(
                    "HTTP {} on @{} (attempt {}/{})",
                    response.status_code, username, attempt, self.max_retries,
                )
                last_error = f"HTTP {response.status_code}"
                if response.status_code in (401, 403):
                    # No point hammering — short exponential then return.
                    if attempt < self.max_retries:
                        await asyncio.sleep(min(15.0, (2 ** attempt) + jitter))
                        continue

            except httpx.TimeoutException as exc:
                last_status = 0
                last_error = f"timeout: {exc!r}"
                logger.warning(
                    "Timeout fetching @{} (attempt {}/{}): {}",
                    username, attempt, self.max_retries, exc,
                )
                await asyncio.sleep(min(15.0, (2 ** attempt) + jitter))
            except httpx.HTTPError as exc:
                last_status = 0
                last_error = f"http error: {exc!r}"
                logger.warning(
                    "HTTP error fetching @{} (attempt {}/{}): {}",
                    username, attempt, self.max_retries, exc,
                )
                await asyncio.sleep(min(15.0, (2 ** attempt) + jitter))

        return ProfileFetchResult(
            username=username,
            http_status=last_status,
            error=last_error or f"failed after {self.max_retries} attempts",
        )
