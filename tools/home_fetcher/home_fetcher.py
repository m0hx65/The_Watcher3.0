"""The Watcher — home page fetcher (worker).

Runs on a device whose internet connection Instagram trusts — an old phone in
Termux, or your PC — and does ONE job for the bot: it polls the bot for
profile pages to fetch, fetches ``instagram.com/<username>/`` from here, and
posts Instagram's answer back. That page's embedded payload carries the
follower/following counts, the bio and the privacy flag that Instagram no
longer hands to datacenter IPs (Render's and Cloudflare's alike, measured
2026-09-05), but still hands to a home line or a VPN.

Nothing dials into your network: this script only makes outbound HTTPS
requests, so it works behind carrier-grade NAT, on a phone, without root, and
without any tunnel. Nothing is stored, nothing else is fetched, no Instagram
login is involved.

Setup — two values, in a file named ``home_fetcher.env`` next to this script
(or as environment variables):

    WATCHER_URL=https://<your-bot>.onrender.com
    HOME_FETCH_TOKEN=<any long random string — the same one you set on Render>

    python home_fetcher.py --new-token     prints a fresh random token to use
    python home_fetcher.py                 runs the worker

Only Python 3.9+ is needed. curl_cffi (``pip install curl_cffi``) is used
when installed — it impersonates Chrome down to the TLS handshake — but the
standard library works too: measured 2026-09-05, Instagram embeds the payload
for any request carrying Chrome's navigation headers from a trusted IP. The
header set is what it reads; the TLS fingerprint is not. That is what lets
this run in Termux, where curl_cffi does not install.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None  # the standard-library engine below is used instead
if os.environ.get("HOME_FETCH_ENGINE", "").lower() == "stdlib":
    curl_requests = None  # force the engine a phone will use, e.g. for a test on a PC

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / "home_fetcher.env"
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
# How long one poll waits at the bot for a job before asking again. The bot
# caps this at 25 s; keep the read timeout comfortably above it.
POLL_WAIT_SECONDS = 25
POLL_READ_TIMEOUT = 45
IG_TIMEOUT_SECONDS = 25
# Be a polite neighbour to your own IP: at most one Instagram request every
# this many seconds, whatever the bot asks for. The bot already paces itself.
MIN_GAP_SECONDS = 2.0

_session = (
    curl_requests.Session(impersonate="chrome120", timeout=IG_TIMEOUT_SECONDS)
    if curl_requests is not None else None
)
ENGINE = "curl_cffi (Chrome impersonation)" if _session is not None else "stdlib urllib"

# A top-level page navigation, as Chrome sends it. Only the stdlib engine
# needs these spelled out; curl_cffi's impersonation adds them itself.
_NAV_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip",
    "Sec-Ch-Ua": '"Chromium";v="120", "Google Chrome";v="120", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_last_request_at = 0.0


def _log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} | {message}", flush=True)


def load_settings() -> tuple[str, str, str]:
    """WATCHER_URL, HOME_FETCH_TOKEN and a worker name — from the environment,
    else from home_fetcher.env next to this script (KEY=VALUE lines)."""
    values: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    url = (os.environ.get("WATCHER_URL") or values.get("WATCHER_URL") or "").rstrip("/")
    token = os.environ.get("HOME_FETCH_TOKEN") or values.get("HOME_FETCH_TOKEN") or ""
    worker = (
        os.environ.get("HOME_FETCH_WORKER") or values.get("HOME_FETCH_WORKER")
        or socket.gethostname() or "worker"
    )
    return url, token, worker


# ------------------------------------------------------------ Instagram

def fetch_profile_page(username: str) -> tuple[int, bytes, str]:
    """One Instagram request, paced. Returns (status, body, final_url)."""
    global _last_request_at
    wait = MIN_GAP_SECONDS - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()
    url = f"https://www.instagram.com/{username}/"
    if _session is not None:
        response = _session.get(
            url, headers={"Accept-Language": "en-US,en;q=0.9"}, allow_redirects=True,
        )
        final_url = getattr(response, "url", "") or ""
        return response.status_code, response.content or b"", final_url
    return _fetch_with_stdlib(url)


def _fetch_with_stdlib(url: str) -> tuple[int, bytes, str]:
    """The same navigation, through urllib. Redirects are followed; a 4xx/5xx
    is returned as Instagram sent it rather than raised."""
    request = urllib.request.Request(url, headers=_NAV_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=IG_TIMEOUT_SECONDS) as response:
            status = response.status
            raw = response.read()
            encoding = response.headers.get("Content-Encoding", "")
            final_url = response.geturl()
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read()
        encoding = error.headers.get("Content-Encoding", "") if error.headers else ""
        final_url = error.geturl() or url
    body = gzip.decompress(raw) if encoding == "gzip" and raw[:2] == b"\x1f\x8b" else raw
    return status, body, final_url


# ------------------------------------------------------------- the device

def read_battery() -> tuple[Optional[int], Optional[bool]]:
    """Battery percent and whether it is charging, or (None, None) when the
    device does not say. Android exposes both in sysfs, readable without root
    on most phones; Termux:API's `termux-battery-status` is the fallback. A
    desktop PC reports nothing, which is fine. The bot turns a low reading
    into a Telegram alert, so a phone that fell off its charger is noticed
    before it dies and the page door closes."""
    # sysfs first. On many phones (MIUI among them) SELinux refuses Termux
    # even a stat() here — Python then raises PermissionError from exists(),
    # so every touch is guarded and any refusal means "ask elsewhere".
    base = Path("/sys/class/power_supply")
    for name in ("battery", "Battery", "BAT0", "BAT1"):
        try:
            percent = int((base / name / "capacity").read_text().strip())
        except (OSError, ValueError):
            continue
        charging: Optional[bool] = None
        try:
            status = (base / name / "status").read_text().strip().lower()
            charging = status in ("charging", "full")
        except (OSError, ValueError):
            pass
        return percent, charging
    # Termux:API (the add-on app plus `pkg install termux-api`) exposes the
    # battery to Termux the supported way.
    try:
        if shutil.which("termux-battery-status"):
            out = subprocess.run(
                ["termux-battery-status"], capture_output=True, text=True, timeout=10,
            ).stdout
            data = json.loads(out)
            percent = int(data.get("percentage"))
            charging = str(data.get("status", "")).upper() in ("CHARGING", "FULL")
            return percent, charging
    except Exception:
        pass
    return None, None


def _device_headers() -> dict[str, str]:
    percent, charging = read_battery()
    headers: dict[str, str] = {}
    if percent is not None:
        headers["X-Watcher-Battery"] = str(percent)
    if charging is not None:
        headers["X-Watcher-Charging"] = "yes" if charging else "no"
    return headers


# --------------------------------------------------------------- the bot

def _bot_request(method: str, url: str, token: str, worker: str, *,
                 body: bytes = b"", headers: dict | None = None,
                 timeout: float = POLL_READ_TIMEOUT) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body if method == "POST" else None, method=method)
    request.add_header("X-Watcher-Token", token)
    request.add_header("X-Watcher-Worker", worker)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def run(url: str, token: str, worker: str) -> int:
    _log(f"polling {url} as '{worker}'  (engine: {ENGINE})")
    percent, charging = read_battery()
    if percent is not None:
        state = "charging" if charging else "not charging" if charging is not None else "unknown"
        _log(f"battery {percent}% ({state}) - reported to the bot with every poll")
    backoff = 5.0
    while True:
        try:
            status, raw = _bot_request(
                "GET", f"{url}/home-fetch/jobs?wait={POLL_WAIT_SECONDS}", token, worker,
                headers=_device_headers(),
            )
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            _log(f"bot unreachable ({exc}) - retrying in {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(60.0, backoff * 2)
            continue
        if status == 401:
            _log("the bot rejected the token - check HOME_FETCH_TOKEN on both sides; retrying in 60s")
            time.sleep(60)
            continue
        if status == 404:
            _log("the bot says the home fetcher is disabled (HOME_FETCH_TOKEN not set on Render) - retrying in 60s")
            time.sleep(60)
            continue
        if status != 200:
            _log(f"bot answered HTTP {status} - retrying in {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(60.0, backoff * 2)
            continue
        backoff = 5.0
        try:
            job = json.loads(raw.decode("utf-8")).get("job")
        except (ValueError, UnicodeDecodeError):
            job = None
        if not job:
            continue  # nothing to do this round — poll again at once

        username = str(job.get("username", "")).lstrip("@")
        job_id = str(job.get("id", ""))
        if not USERNAME_RE.match(username) or not job_id:
            _log(f"ignoring a malformed job: {job!r}")
            continue

        started = time.monotonic()
        try:
            ig_status, body, final_url = fetch_profile_page(username)
        except Exception as exc:  # network failure on our side
            _log(f"@{username}: Instagram request failed - {exc!r}")
            ig_status, body, final_url = 0, b"", ""
        has_payload = b"xig_user_by_username" in body
        _log(
            f"@{username}: Instagram answered HTTP {ig_status}, {len(body)} bytes, "
            f"payload={'yes' if has_payload else 'no'} ({time.monotonic() - started:.1f}s)"
        )
        try:
            code, _ = _bot_request(
                "POST", f"{url}/home-fetch/jobs/{job_id}", token, worker,
                body=gzip.compress(body),
                headers={
                    "Content-Type": "text/html; charset=utf-8",
                    "Content-Encoding": "gzip",
                    "X-IG-Status": str(ig_status),
                    "X-IG-Final-Url": final_url,
                },
                timeout=60,
            )
            if code != 200:
                _log(f"@{username}: the bot answered HTTP {code} to the delivery")
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            _log(f"@{username}: could not deliver to the bot ({exc})")


def main() -> int:
    if "--new-token" in sys.argv:
        print(secrets.token_urlsafe(32))
        return 0
    url, token, worker = load_settings()
    if not url or not token:
        _log(
            "WATCHER_URL and HOME_FETCH_TOKEN are required - put them in "
            f"{ENV_FILE.name} next to this script or in the environment"
        )
        return 2
    try:
        return run(url, token, worker)
    except KeyboardInterrupt:
        _log("stopping")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
