"""End-to-end check: run InstagramClient.fetch_profile() and assert we get a
200 OK with the same fields the Burp capture showed for @65xim."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Windows console defaults to cp1252; force UTF-8 so we can print Arabic bios etc.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Inject env vars before importing settings — they're required by the pydantic model.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "x")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")

from app.monitor.instagram import InstagramClient


EXPECTED = {
    "username": "65xim",
    "full_name": "Mohamad",
    "instagram_id": "7880052534",
    "followers_count": 113,
    "following_count": 997,
    "is_private": True,
    "is_verified": False,
}


async def main() -> int:
    username = sys.argv[1] if len(sys.argv) > 1 else "65xim"
    async with InstagramClient(max_retries=8) as client:
        result = await client.fetch_profile(username)

    print(f"http_status={result.http_status} success={result.success}")
    if result.error:
        print(f"error={result.error}")
    if result.parsed:
        print(json.dumps(result.parsed, indent=2, ensure_ascii=False))

    if not result.success:
        return 1

    if username == "65xim":
        bad = []
        for k, v in EXPECTED.items():
            actual = result.parsed.get(k) if result.parsed else None
            if actual != v:
                bad.append(f"  {k}: expected {v!r} got {actual!r}")
        if bad:
            print("MISMATCH:")
            print("\n".join(bad))
            return 1
        print("All expected fields match Burp capture.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
