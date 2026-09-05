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

Built for a slow link (a phone on a VPN):

- one poll brings back a whole batch of jobs, not one;
- only the page's few-kilobyte payload is sent back, not the 700 KB page;
- uploads run in the background, so the next fetch never waits on the last
  upload, and the bot never waits on the phone — it hands the whole sweep's
  list over up front and picks each page up when it needs it.

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
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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
# How many jobs to ask for per poll (the bot caps this too).
POLL_BATCH = 8
IG_TIMEOUT_SECONDS = 25
# Be a polite neighbour to your own IP: at most one Instagram request every
# this many seconds, whatever the bot asks for.
MIN_GAP_SECONDS = 1.0
# Uploads run here so a slow link never holds up the next fetch or poll.
UPLOAD_THREADS = 2
UPLOAD_TIMEOUT = 90
# When the page carries no payload (login wall, 404, block page) the bot only
# needs enough of it to say what it was.
NO_PAYLOAD_BODY_LIMIT = 64 * 1024

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
_log_lock = threading.Lock()


def _log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _log_lock:
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


# ----------------------------------------------------------- the payload

_PAYLOAD_KEY = '"xig_user_by_username":'
_OBJECT_LIMIT = 200_000


def _extract_object(text: str, start: int) -> Optional[str]:
    """The JSON object beginning at `start`, by brace matching — the same
    walk the bot's parser does (app/monitor/public_page.py), tracking string
    and escape state, bounded by _OBJECT_LIMIT."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, min(len(text), start + _OBJECT_LIMIT)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def extract_payload(body: bytes) -> Optional[bytes]:
    """The profile payload out of the page, wrapped so the bot's parser reads
    it exactly as it reads the page — a few KB instead of 700 KB. None when
    the page carries none (login wall, 404, block page)."""
    text = body.decode("utf-8", "replace")
    at = text.find(_PAYLOAD_KEY)
    if at < 0:
        return None
    raw = _extract_object(text, at + len(_PAYLOAD_KEY))
    if raw is None:
        return None
    return ('{"data":{' + _PAYLOAD_KEY + raw + "}}").encode("utf-8")


# ------------------------------------------------------------- the device

def read_battery() -> tuple[Optional[int], Optional[bool]]:
    """Battery percent and whether it is charging, or (None, None) when the
    device does not say. Android exposes both in sysfs, readable without root
    on some phones; Termux:API's `termux-battery-status` is the supported
    route (MIUI forbids sysfs). A desktop PC reports nothing, which is fine.
    The bot turns a low reading into a Telegram alert."""
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
                 body: bytes = b"", headers: Optional[dict] = None,
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


def deliver(url: str, token: str, worker: str, job_id: str, username: str,
            ig_status: int, body: bytes, final_url: str, fetch_seconds: float) -> None:
    """Upload one answer (runs in a background thread). The payload alone
    when the page has one; otherwise the head of whatever came back, so the
    bot can log what it was."""
    payload = extract_payload(body) if ig_status == 200 else None
    data = payload if payload is not None else body[:NO_PAYLOAD_BODY_LIMIT]
    started = time.monotonic()
    try:
        code, _ = _bot_request(
            "POST", f"{url}/home-fetch/jobs/{job_id}", token, worker,
            body=gzip.compress(data, compresslevel=6),
            headers={
                "Content-Type": "text/html; charset=utf-8",
                "Content-Encoding": "gzip",
                "X-IG-Status": str(ig_status),
                "X-IG-Final-Url": final_url,
                "X-IG-Payload": "1" if payload is not None else "0",
            },
            timeout=UPLOAD_TIMEOUT,
        )
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        _log(f"@{username}: could not deliver to the bot ({exc})")
        return
    _log(
        f"@{username}: Instagram HTTP {ig_status} in {fetch_seconds:.1f}s, "
        f"payload={'yes' if payload is not None else 'no'}, "
        f"delivered {len(data) / 1024:.0f} KB in {time.monotonic() - started:.1f}s"
        + ("" if code == 200 else f" (bot answered HTTP {code})")
    )


def run(url: str, token: str, worker: str) -> int:
    _log(f"polling {url} as '{worker}'  (engine: {ENGINE})")
    percent, charging = read_battery()
    if percent is not None:
        state = "charging" if charging else "not charging" if charging is not None else "unknown"
        _log(f"battery {percent}% ({state}) - reported to the bot with every poll")
    uploads = ThreadPoolExecutor(max_workers=UPLOAD_THREADS, thread_name_prefix="upload")
    backoff = 5.0
    while True:
        poll_started = time.monotonic()
        try:
            status, raw = _bot_request(
                "GET",
                f"{url}/home-fetch/jobs?wait={POLL_WAIT_SECONDS}&batch={POLL_BATCH}",
                token, worker, headers=_device_headers(),
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
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            data = {}
        jobs = data.get("jobs")
        if not jobs and data.get("job"):
            jobs = [data["job"]]  # an older bot build hands out one at a time
        if not jobs:
            continue  # nothing to do this round — poll again at once

        _log(f"{len(jobs)} job(s) received after a {time.monotonic() - poll_started:.1f}s poll")
        for job in jobs:
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
            uploads.submit(
                deliver, url, token, worker, job_id, username,
                ig_status, body, final_url, time.monotonic() - started,
            )


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
