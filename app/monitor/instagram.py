"""Instagram web_profile_info client.

Uses curl_cffi with Chrome TLS impersonation so the JA3/JA4 handshake matches
a real browser. Instagram's anti-bot compares the TLS fingerprint against the
declared User-Agent — httpx (Python OpenSSL stack) gets 401s where Chrome gets
200s on the same IP.

    GET /api/v1/users/web_profile_info/?username=<u> HTTP/2
    Host: www.instagram.com
    x-ig-app-id: 936619743392459
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException, Timeout

from app.config import settings
from app.utils.logger import logger

INSTAGRAM_HOST = "www.instagram.com"
PROFILE_PATH = "/api/v1/users/web_profile_info/"
PROFILE_URL = f"https://{INSTAGRAM_HOST}{PROFILE_PATH}"
FORCED_IG_APP_ID = "936619743392459"
# Pin to a known Chrome fingerprint shipped with curl_cffi. Bump alongside the
# curl_cffi version when newer chromeNNN literals become available.
CHROME_IMPERSONATE = "chrome146"


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


def _build_headers() -> dict[str, str]:
    # Chrome impersonation already injects accept, accept-language, sec-ch-ua*,
    # sec-fetch-*, and a Chrome user-agent. Only the IG-specific app id needs
    # to be added on top — matches the minimal Burp-confirmed request shape.
    return {"x-ig-app-id": FORCED_IG_APP_ID}


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


class _SessionLike(Protocol):
    async def get(self, url: str, *, params: Any = ..., headers: Any = ...) -> Any: ...
    async def close(self) -> None: ...


class InstagramClient:
    """Async client for the web_profile_info endpoint."""

    def __init__(
        self,
        max_retries: int = 8,
        session: _SessionLike | None = None,
    ):
        self.max_retries = max_retries
        if session is not None:
            self._session: _SessionLike = session
            self._own_session = False
        else:
            session_kwargs: dict[str, Any] = {
                "impersonate": CHROME_IMPERSONATE,
                "timeout": (10.0, float(settings.request_timeout)),
                "allow_redirects": True,
            }
            if settings.proxy:
                session_kwargs["proxy"] = settings.proxy
            self._session = AsyncSession(**session_kwargs)
            self._own_session = True

    async def close(self) -> None:
        if self._own_session:
            await self._session.close()

    async def __aenter__(self) -> "InstagramClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def fetch_profile(self, username: str) -> ProfileFetchResult:
        """Fetch a profile with intelligent retry/backoff."""
        username = username.strip().lstrip("@")
        headers = _build_headers()
        last_status = 0
        last_error: Optional[str] = None

        for attempt in range(1, self.max_retries + 1):
            jitter = random.uniform(0.0, 1.5)
            try:
                response = await self._session.get(
                    PROFILE_URL,
                    params={"username": username},
                    headers=headers,
                )
                last_status = response.status_code

                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except Exception:
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

                # 401/403 and other blocks — keep retrying with backoff in
                # case it's a transient IP/UA challenge.
                logger.warning(
                    "HTTP {} on @{} (attempt {}/{})",
                    response.status_code, username, attempt, self.max_retries,
                )
                last_error = f"HTTP {response.status_code}"
                if response.status_code in (401, 403):
                    if attempt < self.max_retries:
                        await asyncio.sleep(min(15.0, (2 ** attempt) + jitter))
                        continue

            except Timeout as exc:
                last_status = 0
                last_error = f"timeout: {exc!r}"
                logger.warning(
                    "Timeout fetching @{} (attempt {}/{}): {}",
                    username, attempt, self.max_retries, exc,
                )
                await asyncio.sleep(min(15.0, (2 ** attempt) + jitter))
            except RequestException as exc:
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
