"""The Cloudflare Worker's key goes to the Worker, and only to the Worker.

The Worker's URL is public in this repository and it proxied Instagram for
anyone who called it. It now checks a shared secret (its WATCHER_KEY) in the
X-Watcher-Key header; the bot sends it from IG_PROXY_KEY.

What must hold:
- with IG_PROXY_KEY unset, nothing changes: no header anywhere;
- with it set, every Worker route (username, user_id, hd_user_id) carries
  the key, and no request to Instagram itself ever does;
- a Worker that refuses the key (403 "bad watcher key") is said to have done
  so, once — it would otherwise read as Instagram blocking every route — and
  Instagram's own 403 is not mistaken for that;
- the key is never logged, never shown, and a pasted one is trimmed.

Runs fully offline.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_worker_key.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.bot import handlers  # noqa: E402
from app.config import Settings, settings  # noqa: E402
from app.monitor.instagram import (  # noqa: E402
    WORKER_KEY_HEADER,
    WORKER_KEY_REFUSED,
    InstagramClient,
)
from app.utils.logger import logger  # noqa: E402

FAILURES: list[str] = []
WORKER = "https://ig-proxy.example.workers.dev"
KEY = "test-key-zq81-not-real"


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


class Resp:
    def __init__(self, status: int, body: Any = None, text: str = "") -> None:
        self.status_code = status
        self._body = body
        self.text = text
        self.url = ""

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


INSTAGRAM_WALL = {"message": "Please wait a few minutes before you try again.",
                  "require_login": True, "status": "fail"}
REEL = {"data": {"user": {"has_public_story": False, "is_live": False,
                          "reel": {"id": "123456789", "user": {"id": "123456789",
                                                               "username": "someone"}},
                          "edge_highlight_reels": {"edges": []}}}}


class Session:
    """The Worker answers whatever `worker` says; Instagram refuses everything
    (its username API, reel query and user-info route 401, the page 429)."""

    def __init__(self, worker: Callable[[dict], Resp]) -> None:
        self.worker = worker
        self.requests: list[tuple[str, dict, dict]] = []

    async def get(self, url: str, *, params: Any = None, headers: Any = None, **kw: Any) -> Resp:
        params, headers = dict(params or {}), dict(headers or {})
        self.requests.append((url, params, headers))
        if url == WORKER:
            return self.worker(params)
        if url.startswith("https://www.instagram.com/") and "/api/" not in url and "graphql" not in url:
            return Resp(429, text="")
        return Resp(401, INSTAGRAM_WALL)

    async def close(self) -> None:
        pass

    def worker_requests(self) -> list[tuple[str, dict, dict]]:
        return [r for r in self.requests if r[0] == WORKER]

    def instagram_requests(self) -> list[tuple[str, dict, dict]]:
        return [r for r in self.requests if r[0] != WORKER]


def has_key_header(headers: dict) -> bool:
    return any(k.lower() == WORKER_KEY_HEADER.lower() for k in headers)


async def every_worker_route(client: InstagramClient) -> None:
    """One call down each Worker route: username, user_id, hd_user_id."""
    await client.fetch_profile("someone", auth_attempts=1)
    client._reel_cache.clear()
    await client.probe_by_id("123456789", user_info=True)


class Logs:
    """Every log line, all levels, while in use."""

    def __enter__(self) -> "Logs":
        self.lines: list[str] = []
        self.warnings: list[str] = []
        self._sink = logger.add(self._take, level="DEBUG")
        return self

    def _take(self, message: Any) -> None:
        self.lines.append(str(message))
        if message.record["level"].name == "WARNING":
            self.warnings.append(message.record["message"])

    def __exit__(self, *exc: Any) -> None:
        logger.remove(self._sink)

    def key_warnings(self) -> list[str]:
        return [w for w in self.warnings if WORKER_KEY_REFUSED in w]


# ---------- the header ---------------------------------------------------------

async def test_no_key_changes_nothing() -> None:
    settings.ig_proxy_key = None
    session = Session(lambda p: Resp(401, {"error": "upstream blocked after retries"}))
    async with InstagramClient(max_retries=1, session=session) as client:
        await every_worker_route(client)
    routes = {k for _, params, _ in session.worker_requests() for k in params}
    expect("(every Worker route was exercised)",
           {"username", "user_id", "hd_user_id"} <= routes, repr(routes))
    expect("with IG_PROXY_KEY unset, no request carries the header",
           not any(has_key_header(h) for _, _, h in session.requests), repr(session.requests))


async def test_the_key_goes_to_the_worker_only() -> None:
    settings.ig_proxy_key = KEY
    try:
        session = Session(lambda p: Resp(401, {"error": "upstream blocked after retries"}))
        async with InstagramClient(max_retries=1, session=session) as client:
            await every_worker_route(client)
        worker = session.worker_requests()
        expect("every Worker request carries the key",
               worker and all(h.get(WORKER_KEY_HEADER) == KEY for _, _, h in worker),
               repr([(p, h) for _, p, h in worker]))
        expect("on every Worker route",
               {k for _, p, _ in worker for k in p} >= {"username", "user_id", "hd_user_id"},
               repr([p for _, p, _ in worker]))
        direct = session.instagram_requests()
        expect("(the direct fallbacks ran too)",
               {u.split("?")[0] for u, _, _ in direct} >= {
                   "https://www.instagram.com/graphql/query/",
                   "https://i.instagram.com/api/v1/users/123456789/info/",
                   "https://www.instagram.com/someone/"},
               repr([u for u, _, _ in direct]))
        expect("and none of them carries the key — Instagram never sees it",
               not any(has_key_header(h) or KEY in str(h) or KEY in str(p)
                       for _, p, h in direct), repr(direct))
    finally:
        settings.ig_proxy_key = None


# ---------- a refused key ------------------------------------------------------

async def test_a_refused_key_is_named_once() -> None:
    settings.ig_proxy_key = KEY
    answers: dict[str, Callable[[dict], Resp]] = {
        "default": lambda p: Resp(403, {"error": WORKER_KEY_REFUSED}),
    }
    session = Session(lambda p: answers.get(next(iter(p)), answers["default"])(p))
    try:
        with Logs() as logs:
            async with InstagramClient(max_retries=1, session=session) as client:
                await every_worker_route(client)
                await every_worker_route(client)
                first = logs.key_warnings()
                expect("a Worker refusing the key is said to, once — not per request",
                       len(first) == 1, repr(logs.warnings))
                expect("naming both settings that must match",
                       first and "IG_PROXY_KEY" in first[0] and "WATCHER_KEY" in first[0],
                       repr(first))
                expect("(the refusal still reads as no answer to the callers)",
                       len(session.worker_requests()) >= 6)

                # The Worker answers once (the key was fixed), then refuses again.
                answers["user_id"] = lambda p: Resp(200, REEL)
                client._reel_cache.clear()
                await client.probe_by_id("123456789")
                answers.pop("user_id")
                client._reel_cache.clear()
                await client.probe_by_id("123456789")
                expect("after an answer, a new refusal is said again",
                       len(logs.key_warnings()) == 2, repr(logs.warnings))
        expect("the key never appears in any log line, at any level",
               not any(KEY in line for line in logs.lines),
               repr([line for line in logs.lines if KEY in line][:2]))
    finally:
        settings.ig_proxy_key = None

    # The same refusal with no key configured says that instead.
    session = Session(lambda p: Resp(403, {"error": WORKER_KEY_REFUSED}))
    with Logs() as logs:
        async with InstagramClient(max_retries=1, session=session) as client:
            await client.fetch_profile("someone", auth_attempts=1)
    expect("with no key set, it says the Worker wants one and IG_PROXY_KEY is unset",
           len(logs.key_warnings()) == 1 and "not set" in logs.key_warnings()[0],
           repr(logs.warnings))


async def test_instagrams_own_403_is_not_a_key_refusal() -> None:
    settings.ig_proxy_key = KEY
    try:
        session = Session(lambda p: Resp(403, {"message": "We're sorry, but something went wrong.",
                                                "status": "fail"}))
        with Logs() as logs:
            async with InstagramClient(max_retries=1, session=session) as client:
                await every_worker_route(client)
        expect("an upstream 403 from Instagram is not reported as the key",
               logs.key_warnings() == [], repr(logs.warnings))
    finally:
        settings.ig_proxy_key = None


# ---------- the setting and what is shown ----------------------------------------

def test_the_key_is_trimmed() -> None:
    base = dict(TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHAT_ID="1",
                DATABASE_URL=f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")
    expect("a pasted key loses its stray whitespace",
           Settings(**base, IG_PROXY_KEY="  abc-123 \n").ig_proxy_key == "abc-123")
    expect("a blank one is no key", Settings(**base, IG_PROXY_KEY="   ").ig_proxy_key is None)
    expect("an absent one is no key", Settings(**base).ig_proxy_key is None)


def test_the_route_line_says_keyed_and_never_the_key() -> None:
    old_url = settings.ig_proxy_url
    settings.ig_proxy_url = WORKER
    try:
        settings.ig_proxy_key = KEY
        keyed = handlers.instagram_route()
        settings.ig_proxy_key = None
        open_ = handlers.instagram_route()
    finally:
        settings.ig_proxy_url = old_url
        settings.ig_proxy_key = None
    expect("/probe's route line says the Worker is keyed", ", keyed)" in keyed, keyed)
    expect("without showing the key", KEY not in keyed, keyed)
    expect("and says so when it is not", ", no key)" in open_, open_)


async def main() -> int:
    settings.ig_proxy_url = WORKER
    settings.ig_session_cookie = None
    settings.home_fetch_token = None

    await test_no_key_changes_nothing()
    await test_the_key_goes_to_the_worker_only()
    await test_a_refused_key_is_named_once()
    await test_instagrams_own_403_is_not_a_key_refusal()
    test_the_key_is_trimmed()
    test_the_route_line_says_keyed_and_never_the_key()

    if DB_FILE.exists():
        DB_FILE.unlink()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("All worker-key tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
