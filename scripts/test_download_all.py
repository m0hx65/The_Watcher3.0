"""Exercise the bulk-download ("📦 Download all") flow end to end with fakes.

Hand-rolled smoke test (no pytest), in the style of test_callback_cleanup.py:
unittest.mock.AsyncMock fakes stand in for Telegram and the network clients so
the whole keyboard -> callback -> service pipeline runs without a live bot.
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

DB_FILE = ROOT / "test_download_all.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from app.bot import handlers, keyboards  # noqa: E402
from app.database.models import MonitoredAccount  # noqa: E402
from app.database.session import dispose_engine, engine  # noqa: E402
from app.monitor.service import MonitorService  # noqa: E402
from app.monitor.stories import StoryItem  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def flatten(markup) -> list:
    return [btn for row in markup.inline_keyboard for btn in row]


def make_update(*, callback_data: str = "", msg_id: int = 42) -> SimpleNamespace:
    query = SimpleNamespace(
        data=callback_data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(message_id=msg_id, delete=AsyncMock()),
    )
    return SimpleNamespace(
        callback_query=query,
        message=None,
        effective_chat=SimpleNamespace(id=7),
        effective_user=SimpleNamespace(id=99),
    )


def make_message_update(text: str) -> SimpleNamespace:
    reply_msg = SimpleNamespace(message_id=500, edit_text=AsyncMock())
    message = SimpleNamespace(
        text=text,
        reply_text=AsyncMock(return_value=reply_msg),
    )
    update = SimpleNamespace(
        callback_query=None,
        message=message,
        effective_chat=SimpleNamespace(id=7),
        effective_user=SimpleNamespace(id=99),
    )
    return update


def make_context(service=None) -> SimpleNamespace:
    bot = SimpleNamespace(delete_message=AsyncMock(), send_message=AsyncMock())
    return SimpleNamespace(
        bot=bot,
        user_data={},
        args=[],
        application=SimpleNamespace(bot_data={"monitor": service}),
    )


def make_service_mock(items=None) -> SimpleNamespace:
    items = items if items is not None else [("1", "Trips"), ("2", "Food"), ("3", "Cats")]
    return SimpleNamespace(
        get_download_overview=AsyncMock(
            return_value={
                "ok": True,
                "items": items,
                "monitored": False,
                "is_private": False,
                "posts_count": 12,
                "error": None,
            }
        ),
        fetch_and_send_profile_picture=AsyncMock(return_value={"ok": True, "hd": True, "error": None}),
        fetch_and_send_stories=AsyncMock(return_value={"ok": True, "count": 2, "error": None}),
        download_posts=AsyncMock(
            return_value={"ok": True, "count": 5, "photos": 3, "videos": 2, "error": None}
        ),
        download_all_highlights=AsyncMock(
            return_value={"ok": True, "count": 9, "reels": 3, "error": None}
        ),
        download_highlights_by_indexes=AsyncMock(
            return_value={"ok": True, "count": 4, "reels": 2, "error": None}
        ),
        instagram=SimpleNamespace(fetch_username_by_id=AsyncMock(return_value="someuser")),
    )


# ---------- keyboards ----------

def test_keyboard_callback_lengths() -> None:
    long_user = "a" * 30
    items = [(str(i), f"Highlight {i}") for i in range(99)]
    selected = {"story", "pic", "ph", "rl"} | {f"h{i}" for i in range(99)}
    markups = [
        keyboards.main_menu(),
        keyboards.download_entry(True),
        keyboards.download_entry(False),
        keyboards.download_panel(long_user, items, selected),
        keyboards.download_result(long_user),
    ]
    worst = 0
    for m in markups:
        for btn in flatten(m):
            worst = max(worst, len(btn.callback_data.encode("utf-8")))
    expect(
        "all download callback_data within Telegram's 64-byte cap",
        worst <= 64,
        f"worst={worst}",
    )


def test_main_menu_has_download_button() -> None:
    data = [b.callback_data for b in flatten(keyboards.main_menu())]
    expect("main menu carries dl:menu", "dl:menu" in data, f"data={data}")


def test_panel_marks_reflect_selection() -> None:
    items = [("1", "Trips"), ("2", "Food")]
    markup = keyboards.download_panel("user", items, {"story", "h1"})
    labels = {b.callback_data: b.text for b in flatten(markup)}
    expect("selected story shows checked box", labels["dl:t:story:user"].startswith("✅"))
    expect("unselected pic shows empty box", labels["dl:t:pic:user"].startswith("⬜"))
    expect("selected highlight 1 shows checked box", labels["dl:t:h1:user"].startswith("✅"))
    expect("unselected highlight 0 shows empty box", labels["dl:t:h0:user"].startswith("⬜"))
    expect(
        "download-selected shows count",
        "(2)" in labels["dl:go:user"],
        labels["dl:go:user"],
    )
    expect(
        "select-all label offered when not all selected",
        "Select all" in labels["dl:hall:user"],
    )
    markup_all = keyboards.download_panel("user", items, {"h0", "h1"})
    labels_all = {b.callback_data: b.text for b in flatten(markup_all)}
    expect(
        "clear label offered when all highlights selected",
        "Clear" in labels_all["dl:hall:user"],
    )


# ---------- handler flow ----------

async def test_dl_menu_shows_entry() -> None:
    update = make_update(callback_data="dl:menu")
    context = make_context(make_service_mock())
    await handlers.on_callback(update, context)
    call = update.callback_query.edit_message_text.call_args
    expect("dl:menu edits the message", call is not None)
    if call:
        markup = call.kwargs["reply_markup"]
        data = [b.callback_data for b in flatten(markup)]
        expect("dl:menu offers manual entry", "dl:manual" in data, f"data={data}")


async def test_dl_manual_sets_prompt_state() -> None:
    update = make_update(callback_data="dl:manual")
    context = make_context(make_service_mock())
    await handlers.on_callback(update, context)
    expect(
        "dl:manual arms the awaiting flag",
        context.user_data.get(handlers._AWAITING_DLALL_USERNAME) is True,
    )
    expect(
        "dl:manual stores the prompt message id",
        context.user_data.get(handlers._PROMPT_MSG_ID) == 42,
    )


async def test_typed_username_opens_panel() -> None:
    service = make_service_mock()
    update = make_message_update("https://instagram.com/SomeUser/")
    context = make_context(service)
    context.user_data[handlers._AWAITING_DLALL_USERNAME] = True
    await handlers.on_plain_text(update, context)
    expect(
        "typed URL resolves and fetches the overview",
        service.get_download_overview.await_count == 1
        and service.get_download_overview.call_args.args[0] == "someuser",
    )
    state = context.user_data.get(handlers._DL_STATE)
    expect(
        "panel state stored for the typed account",
        isinstance(state, dict) and state.get("username") == "someuser",
    )
    reply_msg = update.message.reply_text.return_value
    expect("status message edited into the panel", reply_msg.edit_text.await_count == 1)


async def test_typed_numeric_id_resolves() -> None:
    service = make_service_mock()
    update = make_message_update("12345")
    context = make_context(service)
    context.user_data[handlers._AWAITING_DLALL_USERNAME] = True
    await handlers.on_plain_text(update, context)
    expect(
        "numeric id resolved through fetch_username_by_id",
        service.instagram.fetch_username_by_id.await_count == 1,
    )
    expect(
        "resolved id opens the overview",
        service.get_download_overview.await_count == 1,
    )


async def test_typed_garbage_rejected() -> None:
    service = make_service_mock()
    update = make_message_update("not a user!!")
    context = make_context(service)
    context.user_data[handlers._AWAITING_DLALL_USERNAME] = True
    await handlers.on_plain_text(update, context)
    expect(
        "invalid input never hits the service",
        service.get_download_overview.await_count == 0,
    )
    expect(
        "invalid input gets an error reply",
        update.message.reply_text.await_count == 1,
    )


async def test_toggle_flow() -> None:
    service = make_service_mock()
    context = make_context(service)
    context.user_data[handlers._DL_STATE] = {
        "username": "user",
        "items": [("1", "Trips"), ("2", "Food"), ("3", "Cats")],
        "selected": set(),
        "is_private": False,
        "posts_count": 12,
    }

    update = make_update(callback_data="dl:t:story:user")
    await handlers.on_callback(update, context)
    expect(
        "toggle adds the story token",
        context.user_data[handlers._DL_STATE]["selected"] == {"story"},
    )
    expect(
        "toggle never refetches the overview",
        service.get_download_overview.await_count == 0,
    )

    update = make_update(callback_data="dl:t:story:user")
    await handlers.on_callback(update, context)
    expect(
        "second toggle removes the story token",
        context.user_data[handlers._DL_STATE]["selected"] == set(),
    )

    update = make_update(callback_data="dl:t:h2:user")
    await handlers.on_callback(update, context)
    expect(
        "highlight toggle adds its index token",
        context.user_data[handlers._DL_STATE]["selected"] == {"h2"},
    )

    update = make_update(callback_data="dl:t:h9:user")
    await handlers.on_callback(update, context)
    expect(
        "out-of-range highlight token triggers a panel rebuild",
        service.get_download_overview.await_count == 1,
    )


async def test_toggle_with_stale_state_rebuilds() -> None:
    service = make_service_mock()
    context = make_context(service)  # no _DL_STATE at all
    update = make_update(callback_data="dl:t:story:user")
    await handlers.on_callback(update, context)
    expect(
        "toggle without stored state rebuilds the panel",
        service.get_download_overview.await_count == 1,
    )


async def test_hall_selects_and_clears() -> None:
    service = make_service_mock()
    context = make_context(service)
    context.user_data[handlers._DL_STATE] = {
        "username": "user",
        "items": [("1", "Trips"), ("2", "Food"), ("3", "Cats")],
        "selected": {"story"},
        "is_private": False,
        "posts_count": 12,
    }
    update = make_update(callback_data="dl:hall:user")
    await handlers.on_callback(update, context)
    expect(
        "hall selects all highlight tokens (keeps story)",
        context.user_data[handlers._DL_STATE]["selected"]
        == {"story", "h0", "h1", "h2"},
    )
    update = make_update(callback_data="dl:hall:user")
    await handlers.on_callback(update, context)
    expect(
        "hall again clears only the highlight tokens",
        context.user_data[handlers._DL_STATE]["selected"] == {"story"},
    )


async def test_go_with_empty_selection_alerts() -> None:
    service = make_service_mock()
    context = make_context(service)
    context.user_data[handlers._DL_STATE] = {
        "username": "user",
        "items": [],
        "selected": set(),
        "is_private": False,
        "posts_count": 0,
    }
    update = make_update(callback_data="dl:go:user")
    await handlers.on_callback(update, context)
    answered = update.callback_query.answer.call_args_list
    alerted = any(c.kwargs.get("show_alert") for c in answered)
    expect("empty selection raises an alert", alerted, f"calls={answered}")
    expect(
        "empty selection downloads nothing",
        service.fetch_and_send_stories.await_count == 0
        and service.download_posts.await_count == 0
        and service.download_all_highlights.await_count == 0
        and service.download_highlights_by_indexes.await_count == 0
        and service.fetch_and_send_profile_picture.await_count == 0,
    )


async def test_go_routes_selection_to_services() -> None:
    service = make_service_mock()
    context = make_context(service)
    context.user_data[handlers._DL_STATE] = {
        "username": "user",
        "items": [("1", "Trips"), ("2", "Food"), ("3", "Cats")],
        "selected": {"story", "ph", "h0", "h2"},
        "is_private": False,
        "posts_count": 12,
    }
    update = make_update(callback_data="dl:go:user")
    await handlers.on_callback(update, context)
    expect("go downloads the story", service.fetch_and_send_stories.await_count == 1)
    call = service.download_posts.call_args
    expect(
        "go downloads photos only (reels not ticked)",
        call is not None
        and call.kwargs.get("photos") is True
        and call.kwargs.get("videos") is False,
        f"call={call}",
    )
    hl_call = service.download_highlights_by_indexes.call_args
    expect(
        "go downloads exactly the two ticked highlights",
        hl_call is not None and hl_call.args[1] == [0, 2],
        f"call={hl_call}",
    )
    expect(
        "go skips the not-ticked profile pic and all-highlights path",
        service.fetch_and_send_profile_picture.await_count == 0
        and service.download_all_highlights.await_count == 0,
    )


async def test_all_downloads_everything() -> None:
    service = make_service_mock()
    context = make_context(service)
    update = make_update(callback_data="dl:all:user")
    await handlers.on_callback(update, context)
    call = service.download_posts.call_args
    expect(
        "EVERYTHING sends profile pic + story + posts(photos&videos) + all highlights",
        service.fetch_and_send_profile_picture.await_count == 1
        and service.fetch_and_send_stories.await_count == 1
        and call is not None
        and call.kwargs.get("photos") is True
        and call.kwargs.get("videos") is True
        and service.download_all_highlights.await_count == 1,
    )
    expect(
        "EVERYTHING needs no stored panel state",
        service.download_highlights_by_indexes.await_count == 0,
    )


async def test_overview_failure_shows_error_panel() -> None:
    service = make_service_mock()
    service.get_download_overview = AsyncMock(
        return_value={
            "ok": False, "items": [], "monitored": False,
            "is_private": None, "posts_count": None,
            "error": "@ghost doesn't exist (HTTP 404).",
        }
    )
    context = make_context(service)
    update = make_update(callback_data="dl:open:ghost")
    await handlers.on_callback(update, context)
    expect(
        "failed overview clears any panel state",
        handlers._DL_STATE not in context.user_data,
    )
    last_call = update.callback_query.edit_message_text.call_args
    expect(
        "failed overview reports the error",
        last_call is not None and "ghost" in last_call.kwargs.get("text", ""),
        f"call={last_call}",
    )


# ---------- service layer ----------

def make_real_service(*, posts=None, highlights=None, reel_user=None):
    instagram = SimpleNamespace(
        fetch_profile=AsyncMock(
            return_value=SimpleNamespace(
                success=True,
                http_status=200,
                parsed={
                    "instagram_id": "777",
                    "is_private": False,
                    "posts_count": 7,
                },
                raw_response={},
                error=None,
            )
        ),
        fetch_reel_user=AsyncMock(
            return_value=reel_user
            if reel_user is not None
            else {"highlights": highlights or {}}
        ),
        fetch_username_by_id=AsyncMock(return_value=None),
        fetch_hd_pic_url=AsyncMock(return_value=None),
    )
    stories = SimpleNamespace(
        fetch_posts=AsyncMock(return_value=posts or []),
        fetch_stories=AsyncMock(return_value=[]),
        fetch_highlight_items=AsyncMock(return_value=[]),
        fetch_profile_pic_url=AsyncMock(return_value=None),
        download=AsyncMock(return_value=Path("dummy.jpg")),
    )
    notifier = SimpleNamespace(
        send_text=AsyncMock(return_value=True),
        send_photo=AsyncMock(return_value=True),
        send_video=AsyncMock(return_value=True),
        send_document=AsyncMock(return_value=True),
    )
    hasher = SimpleNamespace(hash_url=AsyncMock(return_value=None))
    service = MonitorService(instagram, hasher, notifier, stories)
    return service, instagram, stories, notifier


def post(pk: str, media_type: str) -> StoryItem:
    return StoryItem(
        pk=pk, taken_at=0, media_type=media_type,
        url=f"https://dl.snapcdn.app/{pk}", source="post",
    )


async def test_download_posts_filters_photos_only() -> None:
    posts = [post("1", "image"), post("2", "video"), post("3", "image")]
    service, _ig, _stories, notifier = make_real_service(posts=posts)
    result = await service.download_posts("someuser", photos=True, videos=False)
    expect(
        "download_posts photos-only sends just the two images",
        result["ok"] and result["photos"] == 2 and result["videos"] == 0,
        f"result={result}",
    )
    expect(
        "photos go out via send_photo, no videos sent",
        notifier.send_photo.await_count == 2 and notifier.send_video.await_count == 0,
    )


async def test_download_posts_both_kinds() -> None:
    posts = [post("1", "image"), post("2", "video")]
    service, _ig, _stories, notifier = make_real_service(posts=posts)
    result = await service.download_posts("someuser", photos=True, videos=True)
    expect(
        "download_posts sends photos and videos",
        result["ok"] and result["count"] == 2
        and notifier.send_photo.await_count == 1
        and notifier.send_video.await_count == 1,
        f"result={result}",
    )


async def test_download_posts_empty_grid_reports_error() -> None:
    service, _ig, _stories, _notifier = make_real_service(posts=[])
    result = await service.download_posts("someuser")
    expect(
        "empty grid is reported as a failure with a reason",
        not result["ok"] and bool(result["error"]),
        f"result={result}",
    )


async def test_download_highlights_by_indexes_picks_right_reels() -> None:
    highlights = {"1": "Trips", "2": "Food", "3": "Cats"}
    service, _ig, stories, notifier = make_real_service(highlights=highlights)

    async def fake_items(username, hid, title):
        return [
            StoryItem(
                pk=f"pk-{hid}", taken_at=0, media_type="image",
                url=f"https://dl.snapcdn.app/{hid}", source="highlight",
                highlight_id=hid, highlight_title=title,
            )
        ]

    stories.fetch_highlight_items = AsyncMock(side_effect=fake_items)
    result = await service.download_highlights_by_indexes("someuser", [0, 2])
    fetched = sorted(c.args[1] for c in stories.fetch_highlight_items.call_args_list)
    expect(
        "indexes 0 and 2 map to highlight ids 1 and 3 (sorted ordering)",
        fetched == ["1", "3"],
        f"fetched={fetched}",
    )
    expect(
        "two reels delivered, one item each",
        result["ok"] and result["reels"] == 2 and result["count"] == 2,
        f"result={result}",
    )
    expect("items sent as photos", notifier.send_photo.await_count == 2)


async def test_download_highlights_bad_indexes() -> None:
    service, _ig, _stories, _notifier = make_real_service(highlights={"1": "Trips"})
    result = await service.download_highlights_by_indexes("someuser", [5])
    expect(
        "out-of-range indexes fail gracefully",
        not result["ok"] and "refresh" in (result["error"] or "").lower(),
        f"result={result}",
    )


async def test_overview_includes_catalog_and_privacy() -> None:
    service, _ig, _stories, _notifier = make_real_service(
        highlights={"2": "B", "1": "A"}
    )
    overview = await service.get_download_overview("@SomeUser")
    expect(
        "overview returns sorted highlight items + profile basics",
        overview["ok"]
        and overview["items"] == [("1", "A"), ("2", "B")]
        and overview["is_private"] is False
        and overview["posts_count"] == 7,
        f"overview={overview}",
    )


async def test_overview_404() -> None:
    service, ig, _stories, _notifier = make_real_service()
    ig.fetch_profile = AsyncMock(
        return_value=SimpleNamespace(
            success=False, http_status=404, parsed=None,
            raw_response=None, error="HTTP 404",
        )
    )
    overview = await service.get_download_overview("ghost")
    expect(
        "404 profile is a hard failure",
        not overview["ok"] and "404" in (overview["error"] or ""),
        f"overview={overview}",
    )


async def test_fetch_and_send_profile_picture_sends_document() -> None:
    service, _ig, stories, notifier = make_real_service()
    pic = SimpleNamespace(
        sha256="ab" * 32, local_path=Path("pic.jpg"), byte_size=1000,
        content_type="image/jpeg", source_url="u",
    )
    service.hasher.hash_url = AsyncMock(return_value=pic)
    stories.fetch_profile_pic_url = AsyncMock(return_value="https://dl.snapcdn.app/x")
    result = await service.fetch_and_send_profile_picture("someuser")
    expect(
        "profile picture fetched HD and sent as a document",
        result["ok"] and result["hd"] and notifier.send_document.await_count == 1,
        f"result={result}",
    )


async def main() -> int:
    # JSONB columns elsewhere don't compile on sqlite; these flows only touch
    # monitored_accounts (get_account / list_accounts).
    async with engine.begin() as conn:
        await conn.run_sync(MonitoredAccount.__table__.create)

    test_keyboard_callback_lengths()
    test_main_menu_has_download_button()
    test_panel_marks_reflect_selection()

    await test_dl_menu_shows_entry()
    await test_dl_manual_sets_prompt_state()
    await test_typed_username_opens_panel()
    await test_typed_numeric_id_resolves()
    await test_typed_garbage_rejected()
    await test_toggle_flow()
    await test_toggle_with_stale_state_rebuilds()
    await test_hall_selects_and_clears()
    await test_go_with_empty_selection_alerts()
    await test_go_routes_selection_to_services()
    await test_all_downloads_everything()
    await test_overview_failure_shows_error_panel()

    await test_download_posts_filters_photos_only()
    await test_download_posts_both_kinds()
    await test_download_posts_empty_grid_reports_error()
    await test_download_highlights_by_indexes_picks_right_reels()
    await test_download_highlights_bad_indexes()
    await test_overview_includes_catalog_and_privacy()
    await test_overview_404()
    await test_fetch_and_send_profile_picture_sends_document()

    await dispose_engine()

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {FAILURES}")
        return 1
    print("\nall good")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
