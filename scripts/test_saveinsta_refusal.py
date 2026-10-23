"""A download saveinsta refuses says why, instead of guessing at a rate limit.

saveinsta answers a request it won't serve with status "ok", no media, and its
reason in "mess". On 2026-10-23 that was "Error: Video is private." for every
highlight of every public account tried (@apple, @natgeo, @nasa) while their
stories still downloaded, and the bot reported "the anonymous source may be
rate-limited or temporarily down" — wrong, and it hid that highlights had
stopped working altogether. A rate-limited token step (HTTP 429) used to surface
as "The authentication token is invalid"; it now says it is a rate limit.

Covers: the reason's clean-up, the refusal record on the client (mess, 429 on
either step, and nothing for a plain empty answer), and the message the
download paths return. Hand-rolled (no pytest), offline with fakes.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_saveinsta_refusal.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.database.models import Base  # noqa: E402
from app.database.session import dispose_engine, engine  # noqa: E402
from app.monitor import service as service_mod  # noqa: E402
from app.monitor.service import MonitorService  # noqa: E402
from app.monitor.stories import StoriesClient  # noqa: E402

FAILURES: list[str] = []

# saveinsta's own words, as it sent them.
PRIVATE_MESS = (
    'Error: Video is private. Please use the tool <a href="/en1/instagram-'
    'private-downloader"><b>Instagram Private Downloader</b></a>.'
)
TOKEN_MESS = "The authentication token is invalid. Please refresh the page and try again."


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Answers userverify and ajaxSearch with canned responses."""

    def __init__(self, verify: FakeResponse, search: FakeResponse) -> None:
        self.verify, self.search = verify, search
        self.searched = 0

    async def post(self, url, data=None, headers=None):
        if url.endswith("/api/userverify"):
            return self.verify
        self.searched += 1
        return self.search

    async def close(self) -> None:
        pass


def make_client(verify: FakeResponse, search: FakeResponse) -> StoriesClient:
    client = StoriesClient()
    client._session = FakeSession(verify, search)  # type: ignore[assignment]
    client._tokens = ("exp", "tok")  # skip the token page
    client._tokens_until = time.monotonic() + 600
    return client


HIGHLIGHT = "https://www.instagram.com/stories/highlights/18338083249250055/"
VERIFIED = FakeResponse(200, {"token": "cf"})


def test_clean_reason() -> None:
    clean = StoriesClient._clean_reason
    expect("the private refusal reads as one sentence",
           clean(PRIVATE_MESS) == "Video is private.", repr(clean(PRIVATE_MESS)))
    expect("the token refusal keeps its first sentence",
           clean(TOKEN_MESS) == "The authentication token is invalid.",
           repr(clean(TOKEN_MESS)))
    long = clean("x" * 300)
    expect("a long reason is capped", len(long) <= 100 and long.endswith("…"), repr(long))


async def test_refusal_is_recorded() -> None:
    client = make_client(
        VERIFIED,
        FakeResponse(200, {"status": "ok", "p": "instagram", "mess": PRIVATE_MESS}),
    )
    since = time.monotonic()
    data = await client._fetch_media_html(HIGHLIGHT)
    expect("a refused request returns no media", data == "")
    expect("its reason is kept", client.refusal_since(since) == "Video is private.",
           repr(client.refusal_since(since)))
    expect("but not for a download that starts after it",
           client.refusal_since(time.monotonic() + 1) is None)
    items = await client.fetch_highlight_items("apple", "18338083249250055", "Pro")
    expect("the highlight comes back empty", items == [])
    await client.close()


async def test_rate_limited_token_step() -> None:
    client = make_client(
        FakeResponse(429, None, "<!doctype html>"),
        FakeResponse(200, {"status": "ok", "mess": TOKEN_MESS}),
    )
    since = time.monotonic()
    await client._fetch_media_html(HIGHLIGHT)
    expect("a 429 on the token step reads as a rate limit",
           client.refusal_since(since) == "Too many requests (HTTP 429).",
           repr(client.refusal_since(since)))
    expect("and the tokenless search is not sent", client._session.searched == 0)
    await client.close()


async def test_rate_limited_search() -> None:
    client = make_client(VERIFIED, FakeResponse(429, None, "slow down"))
    since = time.monotonic()
    await client._fetch_media_html(HIGHLIGHT)
    expect("a 429 on the search reads as a rate limit",
           client.refusal_since(since) == "Too many requests (HTTP 429).")
    await client.close()


async def test_plain_empty_answer_is_not_a_refusal() -> None:
    client = make_client(VERIFIED, FakeResponse(200, {"status": "ok", "data": ""}))
    since = time.monotonic()
    await client._fetch_media_html(HIGHLIGHT)
    expect("an empty answer with no reason records nothing",
           client.refusal_since(since) is None)
    await client.close()


def make_service(stories) -> MonitorService:
    notifier = AsyncMock()
    notifier.create_forum_topic = AsyncMock(return_value=None)
    return MonitorService(
        instagram=AsyncMock(), hasher=AsyncMock(), notifier=notifier, stories=stories,
    )


async def test_download_says_why() -> None:
    refused = make_client(
        VERIFIED,
        FakeResponse(200, {"status": "ok", "mess": PRIVATE_MESS}),
    )
    service = make_service(refused)
    result = await service.download_highlights_from_catalog(
        "apple", {"18338083249250055": "Pro Controls", "17909078682557669": "Aperture"}
    )
    error = str(result.get("error"))
    expect("the bulk highlights download fails", result.get("ok") is False)
    expect("and passes saveinsta's reason on",
           "saveinsta" in error and "“Video is private.”" in error, error)
    expect("instead of guessing at a rate limit", "rate-" not in error, error)
    await refused.close()

    silent = make_client(VERIFIED, FakeResponse(200, {"status": "ok", "data": ""}))
    service = make_service(silent)
    result = await service.download_highlights_from_catalog("apple", {"1": "One"})
    expect("with no reason given, the old message stands",
           result.get("error") == service_mod._DOWNLOAD_UNAVAILABLE_MSG,
           str(result.get("error")))
    await silent.close()


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    test_clean_reason()
    await test_refusal_is_recorded()
    await test_rate_limited_token_step()
    await test_rate_limited_search()
    await test_plain_empty_answer_is_not_a_refusal()
    await test_download_says_why()

    await dispose_engine()
    if DB_FILE.exists():
        DB_FILE.unlink()

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {FAILURES}")
        return 1
    print("\nall good")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    sys.exit(asyncio.run(main()))
