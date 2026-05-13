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
    PROFILE_URL,
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


async def main() -> int:
    await test_profile_request_shape()
    await test_401_retries_then_succeeds()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
