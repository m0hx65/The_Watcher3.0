"""LIVE end-to-end check of the proxied Instagram client (as deployed on Render).

Exercises the exact production configuration: InstagramClient with IG_PROXY_URL
pointing at the Cloudflare Worker, covering the two reported failures:
  1. /add <numeric id>  -> fetch_username_by_id
  2. highlight catalog  -> fetch_reel_user

Usage:  python scripts/test_proxy_live.py [user_id] [add_id]
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./proxy_live_unused.db")
os.environ.setdefault("IG_PROXY_URL", "https://ig-proxy.m-asaad2005-ma.workers.dev")

from app.config import settings  # noqa: E402
from app.monitor.instagram import InstagramClient  # noqa: E402

REEL_USER_ID = sys.argv[1] if len(sys.argv) > 1 else "40427049386"  # opscn1
ADD_ID = sys.argv[2] if len(sys.argv) > 2 else "62790675311"        # /add target

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    print(f"{status}: {name}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


async def main() -> int:
    print(f"proxy: {settings.ig_proxy_url}")
    async with InstagramClient() as client:
        # 1. The /add-by-id path that returned "Could not resolve" in prod.
        username = await client.fetch_username_by_id(ADD_ID)
        expect(f"/add {ADD_ID} resolves", bool(username), "got None")
        print(f"   -> id {ADD_ID} = @{username}")

        # 2. The highlight catalog that showed "has no highlights" in prod.
        reel = await client.fetch_reel_user(REEL_USER_ID)
        expect("reel user fetched", reel is not None)
        if reel:
            print(
                f"   -> @{reel.get('username')} story={reel.get('has_public_story')} "
                f"live={reel.get('is_live')} highlights={len(reel.get('highlights') or {})} "
                f"{list((reel.get('highlights') or {}).values())}"
            )

        # 3. Cache: an immediate repeat must not hit the network.
        t0 = time.perf_counter()
        again = await client.fetch_reel_user(REEL_USER_ID)
        dt = time.perf_counter() - t0
        expect("repeat served from cache (<50ms)", again is not None and dt < 0.05, f"{dt*1000:.0f}ms")

        # 4. Profile fetch through the worker (regression check).
        if reel and reel.get("username"):
            profile = await client.fetch_profile(reel["username"])
            expect(
                "profile fetch via proxy",
                profile.success and bool(profile.parsed),
                f"status={profile.http_status} err={profile.error}",
            )
            if profile.parsed:
                print(
                    f"   -> @{profile.parsed.get('username')} followers="
                    f"{profile.parsed.get('followers_count')} id={profile.parsed.get('instagram_id')}"
                )

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {FAILURES}")
        return 1
    print("\nall good")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
