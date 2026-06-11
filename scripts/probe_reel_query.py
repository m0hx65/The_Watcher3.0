"""Probe the graphql reel query with different request shapes.

Run locally (residential IP) to confirm which header shapes Instagram accepts
for the anonymous reel/highlight query, and test the deployed ig-proxy worker.
"""
import asyncio
import json
import sys

from curl_cffi.requests import AsyncSession

USER_ID = sys.argv[1] if len(sys.argv) > 1 else "40427049386"
URL = "https://www.instagram.com/graphql/query/"
PARAMS = {
    "query_id": "9957820854288654",
    "user_id": USER_ID,
    "include_chaining": "false",
    "include_reel": "true",
    "include_suggested_users": "false",
    "include_logged_out_extras": "true",
    "include_live_status": "true",
    "include_highlight_reels": "true",
}


def summarize(label, status, text):
    out = f"{label}: HTTP {status}"
    try:
        payload = json.loads(text)
        user = (payload.get("data") or {}).get("user") or {}
        reel = user.get("reel") or {}
        edges = (user.get("edge_highlight_reels") or {}).get("edges") or []
        uname = ((reel.get("user") or {}).get("username")) or "?"
        out += f" username={uname} has_story={user.get('has_public_story')} highlights={len(edges)}"
        for e in edges:
            out += f" [{(e.get('node') or {}).get('title')}]"
    except Exception as exc:
        out += f" (parse fail: {exc}; body[:120]={text[:120]!r})"
    print(out)


async def main():
    async with AsyncSession(impersonate="chrome120", timeout=20) as s:
        # 1. Bot's current shape: chrome120 + x-ig-app-id
        r = await s.get(URL, params=PARAMS, headers={"x-ig-app-id": "936619743392459"})
        summarize("bot-shape (x-ig-app-id)", r.status_code, r.text)

        # 2. Bare browser shape: no extra headers at all (matches the Burp capture)
        r = await s.get(URL, params=PARAMS)
        summarize("bare (no extra headers)", r.status_code, r.text)


if __name__ == "__main__":
    asyncio.run(main())
