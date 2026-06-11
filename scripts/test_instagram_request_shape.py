"""Offline smoke test for the locked Instagram request shape."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "x")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")

from app.monitor.instagram import (  # noqa: E402
    FORCED_IG_APP_ID,
    PROFILE_REEL_QUERY_ID,
    PROFILE_REEL_QUERY_URL,
    PROFILE_URL,
    InstagramClient,
    extract_instagram_id,
)


def _payload() -> dict:
    return {
        "data": {
            "user": {
                "username": "65xim",
                "full_name": "Mohamad",
                "biography": "",
                "edge_followed_by": {"count": 1},
                "edge_follow": {"count": 2},
                "edge_owner_to_timeline_media": {"count": 3},
                "is_private": True,
                "is_verified": False,
                "is_business_account": False,
                "id": "7880052534",
            }
        }
    }


def _reel_payload() -> dict:
    return {
        "data": {
            "viewer": None,
            "user": {
                "reel": {
                    "user": {
                        "id": "7880052534",
                        "username": "65xim_new",
                    },
                    "owner": {
                        "id": "7880052534",
                        "username": "65xim_new",
                    },
                }
            },
        },
        "status": "ok",
    }


class _MockResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return self._body


class _MockSession:
    def __init__(self, handler: Callable[[str, dict, dict], _MockResponse]):
        self.handler = handler
        self.requests: list[dict[str, Any]] = []

    async def get(self, url: str, *, params: Any = None, headers: Any = None) -> _MockResponse:
        self.requests.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        return self.handler(url, dict(params or {}), dict(headers or {}))

    async def close(self) -> None:
        pass


async def test_profile_request_shape() -> None:
    session = _MockSession(lambda url, params, headers: _MockResponse(200, _payload()))

    async with InstagramClient(max_retries=1, session=session) as client:
        result = await client.fetch_profile("65xim")

    assert result.success, result.error
    assert len(session.requests) == 1
    req = session.requests[0]
    assert req["url"] == PROFILE_URL
    assert req["params"].get("username") == "65xim"
    assert req["headers"].get("x-ig-app-id") == FORCED_IG_APP_ID


async def test_401_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(url: str, params: dict, headers: dict) -> _MockResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            return _MockResponse(401, {})
        return _MockResponse(200, _payload())

    session = _MockSession(handler)
    async with InstagramClient(max_retries=3, session=session) as client:
        result = await client.fetch_profile("65xim")

    assert result.success, result.error
    assert calls["n"] == 2


async def test_extract_instagram_id_from_reel_query() -> None:
    assert extract_instagram_id(_reel_payload()) == "7880052534"
    assert extract_instagram_id(_payload()) == "7880052534"


async def test_fetch_reel_user_parses_id_and_username() -> None:
    session = _MockSession(lambda url, params, headers: _MockResponse(200, _reel_payload()))

    async with InstagramClient(max_retries=1, session=session) as client:
        parsed = await client.fetch_reel_user("7880052534")

    assert parsed is not None
    assert parsed["instagram_id"] == "7880052534"
    assert parsed["username"] == "65xim_new"
    assert parsed["highlights"] == {}


async def test_username_lookup_by_id_request_shape() -> None:
    session = _MockSession(lambda url, params, headers: _MockResponse(200, _reel_payload()))

    async with InstagramClient(max_retries=1, session=session) as client:
        username = await client.fetch_username_by_id("7880052534")

    assert username == "65xim_new"
    assert len(session.requests) == 1
    req = session.requests[0]
    assert req["url"] == PROFILE_REEL_QUERY_URL
    assert req["params"].get("query_id") == PROFILE_REEL_QUERY_ID
    assert req["params"].get("user_id") == "7880052534"
    assert req["params"].get("include_reel") == "true"
    assert req["params"].get("include_highlight_reels") == "true"
    assert req["headers"].get("x-ig-app-id") == FORCED_IG_APP_ID


async def test_reel_user_routes_via_proxy_when_configured() -> None:
    from app.config import settings

    session = _MockSession(lambda url, params, headers: _MockResponse(200, _reel_payload()))
    old = settings.ig_proxy_url
    settings.ig_proxy_url = "https://ig-proxy.example.workers.dev"
    try:
        async with InstagramClient(max_retries=1, session=session) as client:
            parsed = await client.fetch_reel_user("7880052534")
    finally:
        settings.ig_proxy_url = old

    assert parsed is not None and parsed["username"] == "65xim_new"
    assert len(session.requests) == 1
    req = session.requests[0]
    assert req["url"] == "https://ig-proxy.example.workers.dev"
    assert req["params"] == {"user_id": "7880052534"}


async def test_reel_user_falls_back_to_direct_on_proxy_400() -> None:
    """An old worker build answers 400 for ?user_id= — direct must still run."""
    from app.config import settings

    def handler(url: str, params: dict, headers: dict) -> _MockResponse:
        if url.startswith("https://ig-proxy"):
            return _MockResponse(400, {})
        return _MockResponse(200, _reel_payload())

    session = _MockSession(handler)
    old = settings.ig_proxy_url
    settings.ig_proxy_url = "https://ig-proxy.example.workers.dev"
    try:
        async with InstagramClient(max_retries=1, session=session) as client:
            parsed = await client.fetch_reel_user("7880052534")
    finally:
        settings.ig_proxy_url = old

    assert parsed is not None and parsed["username"] == "65xim_new"
    assert [r["url"] for r in session.requests] == [
        "https://ig-proxy.example.workers.dev",
        PROFILE_REEL_QUERY_URL,
    ]


async def test_reel_user_proxy_404_is_authoritative() -> None:
    """Proxy 404 = Instagram says the id is gone; no direct retry, no crash."""
    from app.config import settings

    session = _MockSession(lambda url, params, headers: _MockResponse(404, {}))
    old = settings.ig_proxy_url
    settings.ig_proxy_url = "https://ig-proxy.example.workers.dev"
    try:
        async with InstagramClient(max_retries=1, session=session) as client:
            parsed = await client.fetch_reel_user("999")
    finally:
        settings.ig_proxy_url = old

    assert parsed is None
    assert len(session.requests) == 1  # proxy only — direct never attempted


async def test_reel_user_cache_serves_repeats() -> None:
    session = _MockSession(lambda url, params, headers: _MockResponse(200, _reel_payload()))

    async with InstagramClient(max_retries=1, session=session) as client:
        first = await client.fetch_reel_user("7880052534")
        second = await client.fetch_reel_user("7880052534")

    assert first is not None and second is not None
    assert len(session.requests) == 1  # second call came from the TTL cache


async def main() -> int:
    await test_profile_request_shape()
    await test_401_retries_then_succeeds()
    await test_extract_instagram_id_from_reel_query()
    await test_fetch_reel_user_parses_id_and_username()
    await test_username_lookup_by_id_request_shape()
    await test_reel_user_routes_via_proxy_when_configured()
    await test_reel_user_falls_back_to_direct_on_proxy_400()
    await test_reel_user_proxy_404_is_authoritative()
    await test_reel_user_cache_serves_repeats()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
