"""Regression tests for the home fetcher worker itself (tools/home_fetcher).

Two faults found in one live sweep log (2026-10-18), both on the phone's side:

- A profile page carries the key ``"xig_user_by_username"`` TWICE — the
  profile query and the posts-grid query — in either order. The worker sent
  the bot whichever came first, so for some accounts it sent the grid block
  (pk, id, timeline; no counts) every sweep, and the bot could only log
  "no profile payload".
- The upload threads keep their connection to the bot open between sweeps.
  A phone's VPN / carrier NAT forgets an idle connection without telling
  either end, so the first upload of a sweep went out into nothing and sat
  out the whole 90 s timeout: pages fetched in ~2 s reached the bot 111 s
  after pickup, all at once.

Runs fully offline (a throwaway HTTP server on 127.0.0.1 stands in for the bot).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "home_fetcher"))
os.environ["HOME_FETCH_ENGINE"] = "stdlib"

import home_fetcher as hf  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


PROFILE = (
    '{"pk":"42","username":"pageuser","is_private":true,"biography":"bio",'
    '"full_name":"Page User","is_verified":false,"follower_count":833,'
    '"following_count":1061,"all_media_count":null,"id":"17841400000000000"}'
)
GRID = (
    '{"pk":"42","polaris_ordered_timeline_connection":{"edges":[],'
    '"page_info":{"end_cursor":null,"has_next_page":false}},"id":"17841400000000000"}'
)


def _page(*blocks: str) -> bytes:
    """A trimmed page with each block in its own preloader, in this order."""
    scripts = "".join(
        '<script type="application/json" data-sjs>{"require":[["RelayPrefetchedStreamCache",'
        f'"next",[],["adp_Q{n}",{{"__bbox":{{"result":{{"data":{{"xig_user_by_username":{block}'
        "}}}}}}]]]}</script>"
        for n, block in enumerate(blocks)
    )
    return f"<!DOCTYPE html><html><body>{scripts}</body></html>".encode("utf-8")


def _payload_user(payload: bytes) -> dict:
    return json.loads(payload)["data"]["xig_user_by_username"]


def test_the_profile_block_is_picked_in_either_order() -> None:
    for name, page in (
        ("profile first", _page(PROFILE, GRID)),
        ("grid first", _page(GRID, PROFILE)),
    ):
        payload = hf.extract_payload(page)
        user = _payload_user(payload) if payload else {}
        expect(f"{name}: the profile block is sent",
               user.get("follower_count") == 833 and user.get("username") == "pageuser",
               repr(payload))
    expect("a page with only the grid block sends nothing (the bot logs its head)",
           hf.extract_payload(_page(GRID)) is None)
    expect("a page with no payload at all sends nothing",
           hf.extract_payload(b"<html>Log in</html>") is None)
    broken = _page('{"pk":"42", "oops', PROFILE)
    expect("a malformed block before the profile does not hide it",
           (_payload_user(hf.extract_payload(broken) or b'{"data":{"xig_user_by_username":{}}}')
            .get("following_count")) == 1061)


class _Bot(BaseHTTPRequestHandler):
    """Counts connections and records each request's headers."""

    protocol_version = "HTTP/1.1"
    connections: set = set()
    seen_headers: list = []

    def setup(self) -> None:
        super().setup()
        _Bot.connections.add(id(self.connection))

    def _answer(self) -> None:
        _Bot.seen_headers.append(dict(self.headers))
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _answer
    do_POST = _answer

    def log_message(self, *args) -> None:  # keep the test output clean
        pass


def test_an_idle_connection_is_reopened_and_a_busy_one_kept() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Bot)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    old_idle = hf.IDLE_RECONNECT_SECONDS
    hf.IDLE_RECONNECT_SECONDS = 0.3
    try:
        link = hf.BotLink(f"http://127.0.0.1:{server.server_port}", "tok", "tester")
        for _ in range(3):
            status, _ = link.request("POST", "/home-fetch/jobs/x", body=b"payload", timeout=5)
        expect("back-to-back uploads share one connection",
               status == 200 and link.connects == 1, repr((status, link.connects)))
        time.sleep(0.5)  # longer than the idle limit: a NAT may have forgotten it
        status, _ = link.request("POST", "/home-fetch/jobs/y", body=b"payload", timeout=5)
        expect("after sitting idle the link opens a fresh connection first",
               status == 200 and link.connects == 2, repr((status, link.connects)))
        expect("the server saw exactly two connections", len(_Bot.connections) == 2,
               repr(len(_Bot.connections)))
        expect("every request names the worker's version",
               all(h.get("X-Watcher-Version") == str(hf.WORKER_VERSION) for h in _Bot.seen_headers),
               repr([h.get("X-Watcher-Version") for h in _Bot.seen_headers]))
        expect("the version that picks the profile block is 2 or later", hf.WORKER_VERSION >= 2)
    finally:
        hf.IDLE_RECONNECT_SECONDS = old_idle
        server.shutdown()
        server.server_close()


def main() -> int:
    test_the_profile_block_is_picked_in_either_order()
    test_an_idle_connection_is_reopened_and_a_busy_one_kept()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All home fetcher worker tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
