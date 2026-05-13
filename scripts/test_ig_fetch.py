"""Probe the Instagram web_profile_info endpoint with the cookie-free Chrome
148 header set used by the production client. Retries until HTTP 200.

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

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
_SEC_CH_UA = '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"'
_SEC_CH_UA_FULL = (
    '"Chromium";v="148.0.7778.167", "Google Chrome";v="148.0.7778.167", '
    '"Not/A)Brand";v="99.0.0.0"'
)


def headers(username: str) -> dict[str, str]:
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9,ar;q=0.8,de;q=0.7,nl;q=0.6,zh-CN;q=0.5,zh;q=0.4",
        "host": "www.instagram.com",
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
        "user-agent": _UA,
        "x-asbd-id": "359341",
        "x-ig-app-id": "936619743392459",
        "x-ig-max-touch-points": "0",
        "x-ig-www-claim": "0",
        "x-requested-with": "XMLHttpRequest",
    }


MAX_ATTEMPTS = 20


def main() -> int:
    with httpx.Client(http2=True, timeout=20.0) as client:
        last_status = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                r = client.get(URL, params={"username": USERNAME}, headers=headers(USERNAME))
                last_status = r.status_code
                print(
                    f"[attempt {attempt}] {r.http_version} {r.status_code} "
                    f"bytes={len(r.content)} ct={r.headers.get('content-type')}"
                )
                if r.http_version != "HTTP/2":
                    print("  ! expected HTTP/2")
                    return 1
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
                time.sleep(delay)
            except httpx.HTTPError as exc:
                print(f"[attempt {attempt}] error {exc!r}")
                time.sleep(min(15, 2 ** attempt))

    print(f"failed after {MAX_ATTEMPTS} attempts, last_status={last_status}")
    return 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    raise SystemExit(main())
