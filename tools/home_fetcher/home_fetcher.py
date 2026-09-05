"""The Watcher -home page fetcher.

Runs on a machine whose internet connection Instagram trusts (your PC, on your
home line or behind your VPN) and fetches ONE thing on the bot's behalf: the
public profile page ``instagram.com/<username>/``. Its embedded payload carries
the follower/following counts, the bio and the privacy flag that Instagram no
longer hands to datacenter IPs -Render's and Cloudflare's alike, as measured
on 2026-09-05. The bot asks this service only after its own attempt was
refused, and only for that page. Nothing is stored here, nothing else is
fetched, and no Instagram login is involved.

Run it:

    python home_fetcher.py

It listens on 127.0.0.1:8787 and prints its access token. Expose it with a
free, stable HTTPS URL (Tailscale Funnel, one command, survives reboots):

    tailscale funnel --bg 8787

Then set two environment variables on the bot (Render → Environment):

    HOME_FETCH_URL   = https://<machine>.<tailnet>.ts.net
    HOME_FETCH_TOKEN = <the token this script printed>

Endpoints:

    GET /health            -> {"ok": true, "requests": N}
    GET /page/<username>   -> Instagram's own status code and HTML, plus an
                              X-IG-Final-Url header. Requires the header
                              X-Watcher-Token: <token>.

Only Python 3.9+ and curl_cffi are needed (``pip install curl_cffi``). The
Chrome TLS impersonation matters: Instagram only embeds the payload for a
request that looks like a real browser navigation.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - startup guidance, not logic
    sys.exit("curl_cffi is required:  pip install curl_cffi")

BIND = os.environ.get("HOME_FETCH_BIND", "127.0.0.1:8787")
TOKEN_FILE = Path(__file__).with_name("home_fetcher.token")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
# Be a polite neighbour to your own IP: at most one Instagram request every
# this many seconds, whatever the bot asks for. The bot already paces itself.
MIN_GAP_SECONDS = 2.0
IG_TIMEOUT_SECONDS = 25

_lock = threading.Lock()
_last_request_at = 0.0
_request_count = 0
_session = curl_requests.Session(impersonate="chrome120", timeout=IG_TIMEOUT_SECONDS)


def _log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} | {message}", flush=True)


def load_token() -> str:
    """The shared secret the bot must present. From the environment when set,
    otherwise from a file next to this script -created on first run."""
    env = os.environ.get("HOME_FETCH_TOKEN", "").strip()
    if env:
        return env
    if TOKEN_FILE.exists():
        stored = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
    return token


TOKEN = load_token()


def fetch_profile_page(username: str) -> tuple[int, bytes, str]:
    """One Instagram request, paced. Returns (status, body, final_url)."""
    global _last_request_at, _request_count
    with _lock:
        wait = MIN_GAP_SECONDS - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()
        _request_count += 1
    response = _session.get(
        f"https://www.instagram.com/{username}/",
        headers={"Accept-Language": "en-US,en;q=0.9"},
        allow_redirects=True,
    )
    final_url = getattr(response, "url", "") or ""
    return response.status_code, response.content or b"", final_url


class Handler(BaseHTTPRequestHandler):
    server_version = "WatcherHomeFetcher/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # we log ourselves, once per request, with the outcome

    def _send(self, status: int, body: bytes, content_type: str,
              extra: Optional[dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._json(200, {"ok": True, "requests": _request_count})
            return
        if not path.startswith("/page/"):
            self._json(404, {"error": "unknown path"})
            return
        if self.headers.get("X-Watcher-Token", "") != TOKEN:
            _log(f"refused {self.client_address[0]} -bad or missing token")
            self._json(401, {"error": "bad token"})
            return
        username = path[len("/page/"):].strip("/").lstrip("@")
        if not USERNAME_RE.match(username):
            self._json(400, {"error": "bad username"})
            return
        started = time.monotonic()
        try:
            status, body, final_url = fetch_profile_page(username)
        except Exception as exc:  # network failure on our side
            _log(f"@{username}: Instagram request failed -{exc!r}")
            self._json(502, {"error": f"instagram request failed: {exc!r}"})
            return
        has_payload = b"xig_user_by_username" in body
        _log(
            f"@{username}: Instagram answered HTTP {status}, {len(body)} bytes, "
            f"payload={'yes' if has_payload else 'no'} "
            f"({time.monotonic() - started:.1f}s)"
        )
        self._send(
            status, body, "text/html; charset=utf-8",
            {"X-IG-Final-Url": final_url, "X-IG-Payload": "1" if has_payload else "0"},
        )


def main() -> int:
    if "--print-token" in sys.argv:
        print(TOKEN)
        return 0
    host, _, port = BIND.rpartition(":")
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port or 8787)), Handler)
    _log(f"listening on http://{host or '127.0.0.1'}:{port or 8787}")
    _log(f"access token: {TOKEN}")
    _log("set HOME_FETCH_TOKEN to that value on the bot, and HOME_FETCH_URL to "
         "the public URL you expose this on (e.g. `tailscale funnel --bg 8787`)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("stopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
