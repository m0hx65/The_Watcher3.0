"""Regression tests: the bio is on the card, and a new post is announced as one.

Three things the owner asked for, all about a PRIVATE account — the case where
the bot can see the metadata and never the media:

- the account card lists the bio (public even on a private account, and the
  field that changes most often with no other visible sign);
- a "—" post count is explained rather than left looking like a lost number.
  It happens when the last successful reading came from the public page, which
  Instagram serves with `all_media_count: null` to a logged-out viewer;
- a rising post count is announced as "posted a new post", not buried in a
  stat line. For a private account that count IS the event: the media is
  unreachable, so nothing else will ever say they posted.

Runs offline on sqlite.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_post_bio.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.bot.handlers import _render_account_card  # noqa: E402
from app.bot.notifications import render_changes_message  # noqa: E402
from app.database.models import (  # noqa: E402
    AccountSnapshot,
    Base,
    MonitoredAccount,
)
from app.database.session import engine, get_session  # noqa: E402
from app.monitor.change_detector import detect_changes  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


BIO = "فـلـس palestine\nCyber security engineer"


async def _account_with(username: str, **snapshot_kwargs) -> None:
    async with get_session() as session:
        account = MonitoredAccount(username=username, active=True, instagram_id="1")
        session.add(account)
        await session.flush()
        session.add(
            AccountSnapshot(
                account_id=account.id,
                username=username,
                http_status=200,
                **snapshot_kwargs,
            )
        )


# ---------- 1. The card ----------

async def test_card_lists_the_bio() -> None:
    await _account_with(
        "biocard",
        full_name="تاليا",
        biography=BIO,
        followers_count=831,
        following_count=1095,
        posts_count=42,
        is_private=True,
        raw_response={"data": {"user": {"id": "1"}}},
    )
    card = await _render_account_card("biocard")
    assert card
    expect("the card lists the bio", "Bio:" in card, card)
    expect("with the real text", "Cyber security engineer" in card, card)
    expect("a private account still shows it", "🔒 private" in card, card)
    expect("and the post count is there when it is known",
           "Posts: <b>42</b>" in card, card)
    expect("no page-source note when the API supplied the reading",
           "public page" not in card, card)


async def test_an_unknown_post_count_is_explained() -> None:
    """The screenshot that started this: 'Posts: —' on a card whose last
    successful check came from the public page."""
    await _account_with(
        "pagecard",
        full_name="تاليا",
        biography=BIO,
        followers_count=831,
        following_count=1095,
        posts_count=None,          # all_media_count was null
        is_private=True,
        raw_response={"source": "public_page"},
    )
    card = await _render_account_card("pagecard")
    assert card
    expect("the dash is still shown, not a fake zero",
           "Posts: <b>—</b>" in card, card)
    expect("and the card says where the reading came from",
           "public page" in card, card)
    expect("and that this door has no post count",
           "no post count" in card, card)
    expect("the bio survives a page reading", "Cyber security engineer" in card, card)


# ---------- 2. The alert ----------

def _posts_change(old: int, new: int) -> str:
    previous = AccountSnapshot(username="poster", http_status=200, posts_count=old)
    current = AccountSnapshot(username="poster", http_status=200, posts_count=new)
    return render_changes_message(detect_changes(previous, current))


def test_a_new_post_is_announced_as_one() -> None:
    text = _posts_change(41, 42)
    expect("a rising post count says they posted",
           "posted a new post" in text, text)
    expect("the numbers are still there", "41" in text and "42" in text, text)
    expect("it is not buried in a bare stat line",
           "Posts: 41 → 42" not in text, text)

    many = _posts_change(41, 44)
    expect("several at once is pluralized", "posted 3 new posts" in many, many)

    gone = _posts_change(42, 41)
    expect("a deletion is not called a post", "removed a post" in gone, gone)
    expect("and never says 'posted'", "posted" not in gone, gone)


def test_an_unknown_count_never_invents_a_post() -> None:
    """A private account whose API is blocked has no post count at all. The
    first reading that HAS one must not read as 'they just posted 42 times'."""
    previous = AccountSnapshot(username="poster", http_status=200, posts_count=None)
    current = AccountSnapshot(username="poster", http_status=200, posts_count=42)
    changeset = detect_changes(previous, current)
    expect("None -> 42 is a silent baseline, not a post",
           changeset.find("posts_count") is None,
           repr([c.field for c in changeset.changes]))


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await test_card_lists_the_bio()
    await test_an_unknown_post_count_is_explained()
    test_a_new_post_is_announced_as_one()
    test_an_unknown_count_never_invents_a_post()

    await engine.dispose()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All post/bio reporting tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
