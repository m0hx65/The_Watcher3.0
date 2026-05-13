"""Offline smoke test for the locked Instagram request shape."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "x")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")

from app.monitor.instagram import (  # noqa: E402
    FORCED_IG_APP_ID,
    INSTAGRAM_HOST,
    PROFILE_PATH,
    InstagramClient,
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


async def test_profile_request_shape() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=_payload(),
            extensions={"http_version": b"HTTP/2"},
        )

    async with InstagramClient(
        max_retries=1,
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await client.fetch_profile("65xim")

    assert result.success, result.error
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "GET"
    assert request.url.host == INSTAGRAM_HOST
    assert request.url.path == PROFILE_PATH
    assert request.url.params.get("username") == "65xim"
    assert request.headers.get("host") == INSTAGRAM_HOST
    assert request.headers.get("x-ig-app-id") == FORCED_IG_APP_ID


async def test_http1_response_rejected() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_payload(),
            extensions={"http_version": b"HTTP/1.1"},
        )

    async with InstagramClient(
        max_retries=1,
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await client.fetch_profile("65xim")

    assert not result.success
    assert result.error and "HTTP/2 is required" in result.error


async def main() -> int:
    await test_profile_request_shape()
    await test_http1_response_rejected()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
