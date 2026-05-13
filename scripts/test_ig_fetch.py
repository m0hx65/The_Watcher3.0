"""Probe the Instagram web_profile_info endpoint with the exact minimal request
that survives anti-bot:

    GET /api/v1/users/web_profile_info/?username=<u> HTTP/2
    Host: www.instagram.com
    X-Ig-App-Id: 936619743392459

Retries until HTTP 200 (or until MAX_ATTEMPTS is hit).

Usage:
    python scripts/test_ig_fetch.py 65xim
"""

from __future__ import annotations

import json
import sys
import time

import httpx

USERNAME = sys.argv[1] if len(sys.argv) > 1 else "65xim"
URL = "https://www.instagram.com/api/v1/users/web_profile_info/"
HEADERS = {"X-Ig-App-Id": "936619743392459"}
MAX_ATTEMPTS = 20


def main() -> int:
    with httpx.Client(http2=True, timeout=20.0) as client:
        last_status = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                r = client.get(URL, params={"username": USERNAME}, headers=HEADERS)
                last_status = r.status_code
                print(
                    f"[attempt {attempt}] {r.http_version} {r.status_code} "
                    f"bytes={len(r.content)} ct={r.headers.get('content-type')}"
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
                        print(json.dumps(data, indent=2)[:1200])
                        return 0
                    print("  ! 200 but no user in payload")
                    return 2
                if r.status_code == 404:
                    print("  user not found")
                    return 2
                if r.status_code == 429:
                    delay = min(60, 2 ** attempt)
                else:
                    delay = min(30, 2 ** attempt)
                print(f"  sleeping {delay}s")
                time.sleep(delay)
            except httpx.HTTPError as exc:
                print(f"[attempt {attempt}] error {exc!r}")
                time.sleep(min(15, 2 ** attempt))

    print(f"failed after {MAX_ATTEMPTS} attempts, last_status={last_status}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
