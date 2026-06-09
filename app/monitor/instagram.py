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
import time
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException, Timeout

from app.config import settings
from app.utils.logger import logger

INSTAGRAM_HOST = "www.instagram.com"
PROFILE_PATH = "/api/v1/users/web_profile_info/"
PROFILE_URL = f"https://{INSTAGRAM_HOST}{PROFILE_PATH}"
PROFILE_REEL_QUERY_ID = "9957820854288654"
PROFILE_REEL_QUERY_URL = f"https://{INSTAGRAM_HOST}/graphql/query/"
MOBILE_HOST = "i.instagram.com"
MOBILE_USER_INFO_PATH = "/api/v1/users/{user_id}/info/"
FORCED_IG_APP_ID = "936619743392459"
CHROME_IMPERSONATE = "chrome120"
# Android Instagram UA — used for the mobile API endpoint to retrieve hd_profile_pic_url_info
_ANDROID_UA = (
    "Instagram 275.0.0.27.98 Android (33/13; 420dpi; 1080x2400; "
    "samsung; SM-G998B; p3s; exynos2100; en_US; 458229258)"
)


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
    # sec-fetch-*, and a Chrome user-agent. Only the IG-specific app id and the
    # optional session cookie need to be added on top — matches the minimal
    # Burp-confirmed request shape for both anonymous and logged-in fetches.
    headers = {"x-ig-app-id": FORCED_IG_APP_ID}
    if settings.ig_session_cookie:
        headers["cookie"] = settings.ig_session_cookie
    return headers


def extract_instagram_id(payload: Optional[dict[str, Any]]) -> Optional[str]:
    """Read a numeric user id from web_profile_info or graphql reel query JSON."""
    if not isinstance(payload, dict):
        return None
    try:
        user = payload["data"]["user"]
    except (KeyError, TypeError):
        return None
    if not isinstance(user, dict):
        return None

    direct = user.get("id")
    if direct:
        return str(direct)

    reel = user.get("reel")
    if not isinstance(reel, dict):
        return None
    if reel.get("id"):
        return str(reel["id"])
    for key in ("user", "owner"):
        node = reel.get(key)
        if isinstance(node, dict) and node.get("id"):
            return str(node["id"])
    return None


def parse_highlight_catalog(payload: dict[str, Any]) -> dict[str, str]:
    """Parse highlight reel id -> title from graphql reel query JSON."""
    try:
        user = payload["data"]["user"]
    except (KeyError, TypeError):
        return {}
    if not isinstance(user, dict):
        return {}
    edges = user.get("edge_highlight_reels", {}).get("edges")
    if not isinstance(edges, list):
        return {}
    catalog: dict[str, str] = {}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        node = edge.get("node")
        if not isinstance(node, dict):
            continue
        highlight_id = node.get("id")
        if highlight_id:
            catalog[str(highlight_id)] = str(node.get("title") or "")
    return catalog


def _parse_reel_query_user(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Parse graphql reel query (query_id=9957820854288654&user_id=…)."""
    try:
        user = payload["data"]["user"]
    except (KeyError, TypeError):
        return None
    if not isinstance(user, dict):
        return None

    instagram_id = extract_instagram_id(payload)
    username: Optional[str] = None
    reel = user.get("reel")
    if isinstance(reel, dict):
        for key in ("user", "owner"):
            node = reel.get(key)
            if isinstance(node, dict):
                candidate = node.get("username")
                if isinstance(candidate, str) and candidate.strip():
                    username = candidate.strip().lstrip("@").lower()
                    break
    if username is None:
        raw = user.get("username")
        if isinstance(raw, str) and raw.strip():
            username = raw.strip().lstrip("@").lower()

    if not instagram_id and not username:
        return None
    return {
        "instagram_id": instagram_id,
        "username": username,
        "highlights": parse_highlight_catalog(payload),
        "has_public_story": bool(user.get("has_public_story")),
        "is_live": bool(user.get("is_live")),
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


class _SessionLike(Protocol):
    async def get(self, url: str, *, params: Any = ..., headers: Any = ...) -> Any: ...
    async def close(self) -> None: ...


class InstagramClient:
    """Async client for the web_profile_info endpoint."""

    def __init__(
        self,
        max_retries: int = 5,
        session: _SessionLike | None = None,
    ):
        self.max_retries = max_retries
        # Circuit breaker for the graphql reel query: datacenter IPs (Render) get
        # a hard 401 on /graphql/query, and retrying it wastes seconds on every
        # call. Once we see a hard block we skip the endpoint for a short while
        # and let callers use their fallback (saveinsta / stored data).
        self._reel_blocked_until: float = 0.0
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

    async def fetch_hd_pic_url(self, user_id: str) -> Optional[str]:
        """Return the highest-resolution profile picture URL via the mobile API.

        Instagram's mobile endpoint returns hd_profile_pic_url_info which holds
        the full-size image (up to ~1440px) rather than the ~320px thumbnail that
        web_profile_info exposes via profile_pic_url_hd.  Falls back gracefully.
        """
        if not user_id:
            return None
        url = f"https://{MOBILE_HOST}/api/v1/users/{user_id}/info/"
        headers: dict[str, str] = {
            "User-Agent": _ANDROID_UA,
            "x-ig-app-id": FORCED_IG_APP_ID,
            "Accept-Language": "en-US,en;q=0.9",
        }
        if settings.ig_session_cookie:
            headers["cookie"] = settings.ig_session_cookie
        try:
            response = await self._session.get(url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                user = data.get("user") or {}
                hd_info = user.get("hd_profile_pic_url_info") or {}
                if hd_info.get("url"):
                    logger.debug("HD pic URL obtained for user_id={}", user_id)
                    return hd_info["url"]
                # hd_profile_pic_url_info absent (no session / private account).
                # Do NOT fall back to the mobile API's profile_pic_url — that is
                # the 150px thumbnail, smaller than what web_profile_info already
                # gave us. Return None so the caller keeps the web API URL.
            logger.debug(
                "Mobile API returned HTTP {} or no hd info for user_id={}",
                response.status_code, user_id,
            )
        except Exception as exc:
            logger.debug("fetch_hd_pic_url failed for user_id={}: {}", user_id, exc)
        return None

    # How long to skip the reel query after a hard block (401/403) before probing
    # again. Keeps card opens and sweeps fast where the graphql endpoint is
    # IP-blocked, while still recovering automatically if access returns.
    _REEL_BLOCK_TTL = 180.0

    async def fetch_reel_user(self, user_id: str) -> Optional[dict[str, Any]]:
        """Fetch reel/highlight metadata for a user id (graphql query_id=9957820854288654).

        Fast-fails: a hard 401/403 (typical on datacenter IPs) trips a short-lived
        circuit breaker so we don't burn seconds retrying a blocked endpoint on
        every call — callers fall back to saveinsta / stored data. Transient
        429/5xx still get one quick retry.
        """
        if not user_id:
            return None
        if time.monotonic() < self._reel_blocked_until:
            return None  # endpoint recently hard-blocked — skip, use fallback
        headers = _build_headers()
        params = {
            "query_id": PROFILE_REEL_QUERY_ID,
            "user_id": str(user_id),
            "include_chaining": "false",
            "include_reel": "true",
            "include_suggested_users": "false",
            "include_logged_out_extras": "true",
            "include_live_status": "true",
            "include_highlight_reels": "true",
        }
        for attempt in range(1, 3):  # at most 2 attempts — fail fast
            try:
                response = await self._session.get(
                    PROFILE_REEL_QUERY_URL,
                    params=params,
                    headers=headers,
                )
                if response.status_code == 200:
                    self._reel_blocked_until = 0.0  # access works — clear breaker
                    try:
                        payload = response.json()
                    except Exception:
                        logger.debug("Reel query id={} returned non-JSON 200", user_id)
                        return None
                    return _parse_reel_query_user(payload)
                if response.status_code in (401, 403):
                    # Hard block (IP/auth) — don't retry, trip the breaker.
                    self._reel_blocked_until = time.monotonic() + self._REEL_BLOCK_TTL
                    logger.debug(
                        "Reel query id={} HTTP {} — blocking endpoint for {:.0f}s",
                        user_id, response.status_code, self._REEL_BLOCK_TTL,
                    )
                    return None
                # 429/5xx — transient, one quick retry.
                if (response.status_code == 429 or 500 <= response.status_code < 600) and attempt < 2:
                    await asyncio.sleep(random.uniform(0.3, 0.7))
                    continue
                logger.debug("Reel query id={} HTTP {} — giving up", user_id, response.status_code)
                return None
            except Exception as exc:
                if attempt < 2:
                    await asyncio.sleep(random.uniform(0.3, 0.7))
                    continue
                logger.debug("Reel query id={} failed: {}", user_id, exc)
                return None
        return None

    async def fetch_username_by_id(self, user_id: str) -> Optional[str]:
        """Resolve the current username for a stable Instagram numeric user ID."""
        parsed = await self.fetch_reel_user(user_id)
        if parsed is None:
            return None
        return parsed.get("username")

    async def fetch_profile(self, username: str) -> ProfileFetchResult:
        """Fetch a profile with intelligent retry/backoff."""
        username = username.strip().lstrip("@")
        headers = _build_headers()
        last_status = 0
        last_error: Optional[str] = None

        if settings.ig_proxy_url:
            fetch_url = settings.ig_proxy_url
            fetch_params: dict[str, str] = {"username": username}
            fetch_headers: dict[str, str] = {}
        else:
            fetch_url = PROFILE_URL
            fetch_params = {"username": username}
            fetch_headers = headers

        for attempt in range(1, self.max_retries + 1):
            jitter = random.uniform(0.0, 1.5)
            try:
                response = await self._session.get(
                    fetch_url,
                    params=fetch_params,
                    headers=fetch_headers,
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

                # 401/403 — retry immediately, IG usually returns 200 within a few tries.
                logger.warning(
                    "HTTP {} on @{} (attempt {}/{})",
                    response.status_code, username, attempt, self.max_retries,
                )
                last_error = f"HTTP {response.status_code}"
                if response.status_code in (401, 403):
                    if attempt < self.max_retries:
                        await asyncio.sleep(random.uniform(1.0, 3.0))
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
