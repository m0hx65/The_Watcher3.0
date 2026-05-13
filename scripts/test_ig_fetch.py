"""Probe the Instagram web_profile_info endpoint using curl_cffi with Chrome
TLS impersonation, mirroring the production client. Retries until HTTP 200.

Usage:
    python scripts/test_ig_fetch.py 65xim
"""

from __future__ import annotations

import asyncio
import json
import sys

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

USERNAME = sys.argv[1] if len(sys.argv) > 1 else "65xim"
URL = "https://www.instagram.com/api/v1/users/web_profile_info/"
HEADERS = {"x-ig-app-id": "936619743392459"}
IMPERSONATE = "chrome146"
MAX_ATTEMPTS = 20


async def main() -> int:
    async with AsyncSession(impersonate=IMPERSONATE, timeout=20.0) as client:
        last_status = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                r = await client.get(URL, params={"username": USERNAME}, headers=HEADERS)
                last_status = r.status_code
                print(
                    f"[attempt {attempt}] http_version={r.http_version} "
                    f"{r.status_code} bytes={len(r.content)} "
                    f"ct={r.headers.get('content-type')}"
                )
                if r.status_code == 200:
                    data = r.json()
                    user = data.get("data", {}).get("user")
                    if user:
                        print(
                            "  ok username={u} full_name={fn} followers={fo} "
                            "following={fl} private={p}".format(
                                u=user.get("username"),
                                fn=user.get("full_name"),
                                fo=user.get("edge_followed_by", {}).get("count"),
                                fl=user.get("edge_follow", {}).get("count"),
                                p=user.get("is_private"),
                            )
                        )
                        print(json.dumps(data, indent=2, ensure_ascii=False)[:1200])
                        return 0
                    print("  ! 200 but no user in payload")
                    return 2
                if r.status_code == 404:
                    print("  user not found")
                    return 2
                delay = min(60, 2 ** attempt)
                print(f"  sleeping {delay}s")
                await asyncio.sleep(delay)
            except RequestException as exc:
                print(f"[attempt {attempt}] error {exc!r}")
                await asyncio.sleep(min(15, 2 ** attempt))

    print(f"failed after {MAX_ATTEMPTS} attempts, last_status={last_status}")
    return 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    raise SystemExit(asyncio.run(main()))
