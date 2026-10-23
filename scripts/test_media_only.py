"""Media-only accounts alert on their stories, posts and avatar alone.

The 🎞 Media only button on the account card keeps an account's alerts to its
new posts/reels, highlights count and profile picture (stories go out from the
story phase as before). Followers, following, bio, name, link and the flags are
still read and stored — just not sent or logged — and 📋 Track all turns them
back on.

Covers: the stored flag, the alert filter in _dispatch_changes (including the
follower-anomaly alert and the manual-recheck "no changes" line), that the
caller's change set is left whole, the card line and buttons, and the toggle
callback. Hand-rolled (no pytest), runs offline on sqlite with fakes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_media_only.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.bot import handlers, keyboards  # noqa: E402
from app.database import crud  # noqa: E402
from app.database.models import Base, MonitoredAccount, NotificationLog  # noqa: E402
from app.database.session import dispose_engine, engine, get_session  # noqa: E402
from app.monitor.change_detector import Change, ChangeSet  # noqa: E402
from app.monitor.service import MonitorService  # noqa: E402
from sqlalchemy import select  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def make_service() -> MonitorService:
    notifier = AsyncMock()
    notifier.send_text = AsyncMock(return_value=True)
    notifier.send_document = AsyncMock(return_value=True)
    notifier.create_forum_topic = AsyncMock(return_value=None)
    return MonitorService(
        instagram=AsyncMock(), hasher=AsyncMock(), notifier=notifier,
        stories=AsyncMock(),
    )


def profile_changes(username: str, *, followers_new: int = 1010) -> ChangeSet:
    return ChangeSet(
        username=username,
        changes=[
            Change("followers_count", 1000, followers_new, "followers"),
            Change("biography", "old bio", "new bio", "bio"),
            Change("posts_count", 41, 42, "posts"),
        ],
        profile_pic_changed=True,
        old_pic_hash="old",
        new_pic_hash="new",
    )


def texts(service: MonitorService) -> list[str]:
    return [c.args[0] for c in service.notifier.send_text.call_args_list]


async def logged_types(account_id: int) -> list[str]:
    async with get_session() as session:
        rows = (await session.execute(
            select(NotificationLog.change_type)
            .where(NotificationLog.account_id == account_id)
        )).scalars().all()
    return sorted(rows)


async def add_account(account_id: int, username: str) -> None:
    async with get_session() as session:
        session.add(MonitoredAccount(id=account_id, username=username, active=True))


async def test_flag_round_trip() -> None:
    await add_account(1, "flagged")
    async with get_session() as session:
        before = await crud.is_media_only(session, 1)
        await crud.set_media_only(session, 1, True)
    async with get_session() as session:
        on = await crud.is_media_only(session, 1)
        await crud.set_media_only(session, 1, False)
    async with get_session() as session:
        off = await crud.is_media_only(session, 1)
    expect("an account alerts on everything by default", before is False)
    expect("media only is stored", on is True)
    expect("track all clears it", off is False)


async def test_track_all_sends_every_change() -> None:
    await add_account(2, "everything")
    service = make_service()
    await service._dispatch_changes(
        2, "everything", profile_changes("everything"),
        previous_snapshot_id=1, new_pic_path=Path("pic.jpg"), notify_unchanged=False,
    )
    sent = "\n".join(texts(service))
    expect("followers are reported", "followers" in sent, sent)
    expect("the bio is reported", "Bio changed" in sent, sent)
    expect("the post is reported", "posted a new post" in sent, sent)
    expect("the picture is sent", service.notifier.send_document.await_count == 1)
    expect(
        "every change is logged",
        await logged_types(2)
        == ["biography", "followers_count", "posts_count", "profile_picture"],
        str(await logged_types(2)),
    )


async def test_media_only_keeps_posts_and_avatar() -> None:
    await add_account(3, "mediaacct")
    async with get_session() as session:
        await crud.set_media_only(session, 3, True)
    service = make_service()
    changeset = profile_changes("mediaacct")
    await service._dispatch_changes(
        3, "mediaacct", changeset,
        previous_snapshot_id=1, new_pic_path=Path("pic.jpg"), notify_unchanged=False,
    )
    sent = "\n".join(texts(service))
    expect("the post is still reported", "posted a new post" in sent, sent)
    expect("followers are not sent", "followers" not in sent, sent)
    expect("the bio is not sent", "Bio" not in sent, sent)
    expect("the picture is still sent", service.notifier.send_document.await_count == 1)
    expect(
        "only the post and picture are logged",
        await logged_types(3) == ["posts_count", "profile_picture"],
        str(await logged_types(3)),
    )
    expect(
        "the caller's change set is left whole",
        [c.field for c in changeset.changes]
        == ["followers_count", "biography", "posts_count"],
    )


async def test_media_only_mutes_the_follower_anomaly() -> None:
    await add_account(4, "bigjump")
    async with get_session() as session:
        await crud.set_media_only(session, 4, True)
    service = make_service()
    changeset = ChangeSet(
        username="bigjump",
        changes=[Change("followers_count", 1000, 50000, "followers")],
    )
    await service._dispatch_changes(
        4, "bigjump", changeset,
        previous_snapshot_id=1, new_pic_path=None, notify_unchanged=False,
    )
    expect(
        "a muted follower jump sends nothing, anomaly included",
        service.notifier.send_text.await_count == 0,
        str(texts(service)),
    )
    expect("and logs nothing", await logged_types(4) == [], str(await logged_types(4)))


async def test_media_only_recheck_says_what_was_muted() -> None:
    await add_account(5, "rechecked")
    async with get_session() as session:
        await crud.set_media_only(session, 5, True)
    service = make_service()
    changeset = ChangeSet(
        username="rechecked",
        changes=[Change("biography", "a", "b", "bio")],
    )
    await service._dispatch_changes(
        5, "rechecked", changeset,
        previous_snapshot_id=1, new_pic_path=None, notify_unchanged=True,
    )
    sent = texts(service)
    expect(
        "a recheck with only muted changes says so",
        len(sent) == 1 and "1 profile change muted: media only" in sent[0]
        and "No changes detected" not in sent[0],
        str(sent),
    )


def test_keyboard_button() -> None:
    def buttons(markup) -> list[str]:
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    off = buttons(keyboards.account_actions("u"))
    on = buttons(keyboards.account_actions("u", media_only=True))
    expect("the card offers Media only", "acc:mediaonly:u" in off, str(off))
    expect("a media-only card offers Track all", "acc:trackall:u" in on, str(on))


async def test_toggle_from_the_card() -> None:
    await add_account(6, "toggled")
    query = SimpleNamespace(
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(message_id=9, chat_id=7),
    )
    update = SimpleNamespace(
        callback_query=query, message=None,
        effective_chat=SimpleNamespace(id=7), effective_user=SimpleNamespace(id=99),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(edit_message_text=AsyncMock()),
        user_data={},
        application=SimpleNamespace(bot_data={"monitor": make_service()}),
    )

    await handlers._handle_account(update, context, ["mediaonly", "toggled"])
    async with get_session() as session:
        on = await crud.is_media_only(session, 6)
    card = query.edit_message_text.call_args.kwargs
    expect("Media only turns the mode on", on is True)
    expect("the card says so", "media only" in card.get("text", ""), card.get("text"))
    expect(
        "the card now offers Track all",
        "acc:trackall:toggled"
        in [b.callback_data for row in card["reply_markup"].inline_keyboard for b in row],
    )

    await handlers._handle_account(update, context, ["trackall", "toggled"])
    async with get_session() as session:
        off = await crud.is_media_only(session, 6)
    card = query.edit_message_text.call_args.kwargs
    expect("Track all turns it off", off is False)
    expect("the card line is gone", "media only" not in card.get("text", ""), card.get("text"))


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await test_flag_round_trip()
    await test_track_all_sends_every_change()
    await test_media_only_keeps_posts_and_avatar()
    await test_media_only_mutes_the_follower_anomaly()
    await test_media_only_recheck_says_what_was_muted()
    test_keyboard_button()
    await test_toggle_from_the_card()

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
