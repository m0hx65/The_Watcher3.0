"""Telegram bot command handlers and inline-button callback routing."""

from __future__ import annotations

import asyncio
import csv
import io
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from telegram import BotCommand, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.bot import keyboards
from app.config import settings
from app.database import crud
from app.database.session import get_session
from app.monitor.service import MonitorService
from app.utils.formatting import esc, fmt_number, fmt_timestamp, truncate
from app.utils.logger import logger
from app.workers.scheduler import MAX_INTERVAL, MIN_INTERVAL, WatcherScheduler


# ---------- Static text & bot menu ----------

WELCOME_TEXT = (
    "<b>👁 The Watcher</b>\n"
    "<i>Silent Instagram profile monitoring</i>\n\n"
    "Use the buttons below, or type <code>/add @username</code>, <code>/add https://instagram.com/username</code>, or <code>/add 1234567890</code> to start."
)

HELP_TEXT = (
    "<b>👁 The Watcher</b>\n\n"
    "<b>Navigation</b>\n"
    "Tap any account in the list to open its card. From there: "
    "Recheck · History · Photo · Story · Highlights · Remove. "
    "✨ Highlights lists every highlight by name; story/live status shows on the card. "
    "🏠 Home always returns here.\n\n"
    "<b>Commands</b>\n"
    "<code>/add @user</code>, <code>/add https://instagram.com/user</code>, or <code>/add 1234567890</code> — start monitoring\n"
    "<code>/remove @user</code> — stop monitoring\n"
    "<code>/list</code> — all accounts\n"
    "<code>/recheck @user</code> — force a check now\n"
    "<code>/status</code> — monitoring stats\n"
    "<code>/interval [value]</code> — get or set interval (e.g. <code>30m</code>)\n"
    "<code>/history @user</code> — recent changes\n"
    "<code>/photo @user</code> — stored profile picture\n"
    "<code>/fetchphoto @user</code> — download current profile picture on demand\n"
    "<code>/story @user</code> — download any user's current story (no monitoring needed)\n"
    "<code>/highlights @user</code> — list any user's highlights to download\n"
    "<code>/export</code> — download CSV\n\n"
    "<b>🔎 Any user</b> on the menu does the same as /story and /highlights for "
    "any public account, without adding it to monitoring."
)

BOT_COMMANDS: list[BotCommand] = [
    BotCommand("menu", "Open the main menu"),
    BotCommand("add", "Start monitoring an account"),
    BotCommand("remove", "Stop monitoring an account"),
    BotCommand("list", "List monitored accounts"),
    BotCommand("status", "Show monitoring statistics"),
    BotCommand("interval", "Show or change the recheck interval"),
    BotCommand("recheck", "Force a check for a username"),
    BotCommand("history", "Recent changes for a username"),
    BotCommand("photo", "Current profile picture"),
    BotCommand("fetchphoto", "Download current profile picture on demand"),
    BotCommand("story", "Download any user's current story"),
    BotCommand("highlights", "List any user's highlights to download"),
    BotCommand("export", "Export change history as CSV"),
    BotCommand("help", "Show help"),
]

_AWAITING_USERNAME = "awaiting_username"
_AWAITING_FETCH_USERNAME = "awaiting_fetch_username"
_AWAITING_INTERVAL = "awaiting_interval"
# Message id of the bot's most recent prompt (the message that displays
# a Cancel button while we wait for typed input). Used so we can clean it
# up once the user has actually responded.
_PROMPT_MSG_ID = "prompt_msg_id"
# Keys for tracking the active panel (main-menu message) so it can be
# moved to the bottom of the chat after automated notifications arrive.
PANEL_MSG_ID = "panel_msg_id"
PANEL_CHAT_ID = "panel_chat_id"
# Instagram usernames: 1–30 chars, ASCII letters/digits/dots/underscores.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_INSTAGRAM_ID_RE = re.compile(r"^\d{1,64}$")
_INSTAGRAM_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})(?:[/?#].*)?$",
    re.IGNORECASE,
)
# "30m", "1h", "1800s", "1h30m", or a bare integer (seconds).
_INTERVAL_RE = re.compile(
    r"^\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?\s*$",
    re.IGNORECASE,
)


# ---------- Authorization ----------

def _is_authorized(update: Update) -> bool:
    admins = settings.admin_ids
    if not admins:
        return True
    user = update.effective_user
    chat = update.effective_chat
    if user and user.id in admins:
        return True
    if chat and chat.id in admins:
        return True
    return False


async def _reject_if_unauthorized(update: Update) -> bool:
    if _is_authorized(update):
        return False
    if update.callback_query:
        await update.callback_query.answer("Unauthorized.", show_alert=True)
    elif update.message:
        await update.message.reply_text("Unauthorized.")
    return True


# ---------- Small helpers ----------

def _username_arg(context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    if not context.args:
        return None
    return _normalize_username(context.args[0])


def _normalize_username(raw: str) -> Optional[str]:
    raw = raw.strip().lstrip("@")
    if not raw or not _USERNAME_RE.match(raw):
        return None
    return raw.lower()


def _parse_add_target(raw: str) -> tuple[Optional[str], Optional[str]]:
    raw = raw.strip()
    if not raw:
        return None, None
    if raw.lower().startswith("http://") or raw.lower().startswith("https://"):
        match = _INSTAGRAM_URL_RE.match(raw)
        if not match:
            return None, None
        path = match.group(1)
        if _INSTAGRAM_ID_RE.match(path):
            return None, path
        return _normalize_username(path), None
    if "instagram.com/" in raw.lower():
        match = _INSTAGRAM_URL_RE.match(raw)
        if not match:
            return None, None
        path = match.group(1)
        if _INSTAGRAM_ID_RE.match(path):
            return None, path
        return _normalize_username(path), None
    raw = raw.lstrip("@")
    if not raw:
        return None, None
    if _INSTAGRAM_ID_RE.match(raw):
        return None, raw
    return _normalize_username(raw), None


def _parse_interval(raw: str) -> Optional[int]:
    """Accept '30m', '1h', '1800s', '1h30m', or a bare integer (seconds)."""
    text = raw.strip().lower()
    if not text:
        return None
    if text.isdigit():
        n = int(text)
        return n or None
    m = _INTERVAL_RE.match(text)
    if not m or not any(m.groups()):
        return None
    h, mm, s = (int(x) if x else 0 for x in m.groups())
    total = h * 3600 + mm * 60 + s
    return total or None


def _format_interval(seconds: int) -> str:
    """Render seconds as the shortest 'XhYmZs' form, dropping zeros."""
    seconds = int(seconds)
    parts: list[str] = []
    if seconds >= 3600:
        h, seconds = divmod(seconds, 3600)
        parts.append(f"{h}h")
    if seconds >= 60:
        m, seconds = divmod(seconds, 60)
        parts.append(f"{m}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return "".join(parts)


def _chat_id(update: Update) -> Optional[int]:
    chat = update.effective_chat
    return chat.id if chat else None


_EDIT_IGNORED_ERRORS = (
    "not modified",
    "message to edit not found",
    "message can't be edited",
    "message_id_invalid",
    "message identifier is not specified",
    "there is no text in the message to edit",
)


async def _safe_edit_text(
    query,
    text: str,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = ParseMode.HTML,
):
    """Edit a callback message, swallowing benign errors.

    For media messages (photo/document/video) that cannot be text-edited,
    deletes the media message and sends a fresh text message instead.
    Returns the resulting Message object so callers can track its id.
    """
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
        return query.message
    except BadRequest as exc:
        err = str(exc).lower()
        if not any(s in err for s in _EDIT_IGNORED_ERRORS):
            raise
        # Telegram rejects edit_message_text on photo/document/video messages.
        # Fall back to deleting the media message and sending plain text.
        message = getattr(query, "message", None)
        if message and (
            message.document
            or message.photo
            or message.video
            or message.animation
        ):
            try:
                await message.delete()
            except (BadRequest, Forbidden, TelegramError):
                pass
            new_msg = await query.get_bot().send_message(
                chat_id=message.chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
            return new_msg
        return message


async def _safe_answer(query, text: Optional[str] = None, *, show_alert: bool = False) -> None:
    """Acknowledge a callback query, ignoring 'too old / already answered' errors."""
    try:
        await query.answer(text=text, show_alert=show_alert)
    except BadRequest:
        # Query expired or already answered — the spinner has gone anyway.
        pass
    except TelegramError:
        pass


async def _consume_prompt_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete the bot's stored prompt message (the one with a Cancel button), if any."""
    msg_id = context.user_data.pop(_PROMPT_MSG_ID, None)
    chat_id = _chat_id(update)
    if msg_id is None or chat_id is None:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except (BadRequest, Forbidden, TelegramError):
        # Already gone, too old, or perms — ignore.
        pass


async def _delete_callback_message(update: Update) -> None:
    """Remove the inline-keyboard message that triggered the active callback."""
    query = update.callback_query
    if not query or not query.message:
        return
    try:
        await query.message.delete()
    except (BadRequest, Forbidden, TelegramError):
        pass


async def _send_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete any existing panel then send a fresh menu at the bottom of the chat."""
    chat_id = _chat_id(update)
    if chat_id is None:
        return
    old_msg_id = context.application.bot_data.get(PANEL_MSG_ID)
    if old_msg_id is not None:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
        except (BadRequest, Forbidden, TelegramError):
            pass
        context.application.bot_data.pop(PANEL_MSG_ID, None)
    msg = await update.message.reply_text(
        WELCOME_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.main_menu(),
    )
    context.application.bot_data[PANEL_MSG_ID] = msg.message_id
    context.application.bot_data[PANEL_CHAT_ID] = chat_id
    async with get_session() as session:
        await crud.set_setting(session, "panel_msg_id", str(msg.message_id))
        await crud.set_setting(session, "panel_chat_id", str(chat_id))


async def _reply_or_edit(
    update: Update,
    text: str,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = ParseMode.HTML,
) -> None:
    """Send text — edit the existing message for callback flows, reply for command flows."""
    if update.callback_query:
        await _safe_edit_text(
            update.callback_query,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    elif update.message:
        await update.message.reply_text(
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )


# ---------- View renderers ----------

async def _render_account_card(
    username: str, service: Optional[MonitorService] = None
) -> Optional[str]:
    async with get_session() as session:
        account = await crud.get_account(session, username)
        if not account:
            return None
        snapshot = await crud.get_latest_snapshot(
            session, account.id, successful_only=True
        )
        media = await crud.latest_media_hash(session, account.id)
        highlight_catalog = await crud.get_highlight_catalog(session, account.id)

    marker = "🟢 active" if account.active else "⏸ paused"
    last = fmt_timestamp(account.last_checked_at) if account.last_checked_at else "never"
    status = f"HTTP {account.last_status_code}" if account.last_status_code else "—"
    fails = account.consecutive_failures or 0

    lines = [
        f"<b>@{esc(account.username)}</b>",
        f"Instagram ID: <code>{esc(account.instagram_id or 'unknown')}</code>",
        f"State: {marker}",
        f"Last check: <code>{last}</code> · {status}",
    ]
    if fails:
        lines.append(f"Consecutive failures: <b>{fails}</b>")

    if snapshot:
        lines.append("")
        lines.append("<b>Latest snapshot</b>")
        if snapshot.full_name:
            lines.append(f"Name: <code>{esc(snapshot.full_name)}</code>")
        lines.append(
            f"Followers: <b>{fmt_number(snapshot.followers_count)}</b> · "
            f"Following: <b>{fmt_number(snapshot.following_count)}</b>"
        )
        lines.append(f"Posts: <b>{fmt_number(snapshot.posts_count)}</b>")
        flags: list[str] = []
        if snapshot.is_private:
            flags.append("🔒 private")
        if snapshot.is_verified:
            flags.append("✓ verified")
        if snapshot.is_business:
            flags.append("💼 business")
        if flags:
            lines.append(" · ".join(flags))

        # Story / live status for public accounts. Fetch it LIVE so a story
        # posted since the last check shows immediately — the stored snapshot's
        # reel_data is only as fresh as the last sweep/recheck. Fall back to the
        # stored value if the live fetch is unavailable or fails.
        if not snapshot.is_private:
            reel_data = (snapshot.raw_response or {}).get("reel_data") or {}
            has_story = bool(reel_data.get("has_public_story"))
            is_live = bool(reel_data.get("is_live"))
            if service is not None and account.instagram_id:
                try:
                    live = await service.instagram.fetch_reel_user(
                        str(account.instagram_id)
                    )
                except Exception:  # pragma: no cover - network failure path
                    live = None
                logger.info(
                    "Card live story @{} (id={}): fetched={} has_story={} is_live={} (stored had_story={})",
                    account.username, account.instagram_id, live is not None,
                    None if live is None else live.get("has_public_story"),
                    None if live is None else live.get("is_live"), has_story,
                )
                if live is not None:
                    has_story = bool(live.get("has_public_story"))
                    is_live = bool(live.get("is_live"))
            if is_live:
                story_state = "🔴 live now"
            elif has_story:
                story_state = "🎬 has an active story"
            else:
                story_state = "⭕ no active story"
            lines.append(f"Story: {story_state}")

    if highlight_catalog:
        lines.append("")
        lines.append(f"<b>✨ Highlights ({len(highlight_catalog)})</b>")
        for title in sorted(highlight_catalog.values()):
            lines.append(f"  • {esc(title) or '(untitled)'}")
        lines.append("<i>Tap ✨ Highlights to see all highlight names.</i>")

    if media:
        lines.append("")
        lines.append(
            f"Profile picture captured: <code>{fmt_timestamp(media.created_at)}</code>"
        )

    return "\n".join(lines)


def _scheduler(context: ContextTypes.DEFAULT_TYPE) -> Optional[WatcherScheduler]:
    sched = context.application.bot_data.get("scheduler")
    return sched if isinstance(sched, WatcherScheduler) else None


async def _render_status_message(context: ContextTypes.DEFAULT_TYPE) -> str:
    async with get_session() as session:
        stats = await crud.stats_summary(session)

    scheduler_state = context.application.bot_data.get("scheduler_state", "unknown")
    next_run = context.application.bot_data.get("next_run")
    next_run_str = fmt_timestamp(next_run) if next_run else "—"

    sched = _scheduler(context)
    interval = sched.interval_seconds if sched else settings.check_interval

    return (
        "<b>📊 Watcher status</b>\n\n"
        f"Accounts: <b>{stats['accounts_total']}</b> "
        f"(active: <b>{stats['accounts_active']}</b>)\n"
        f"Snapshots stored: <b>{fmt_number(stats['snapshots_total'])}</b>\n"
        f"Notifications sent: <b>{fmt_number(stats['notifications_total'])}</b>\n\n"
        f"Scheduler: <b>{esc(str(scheduler_state))}</b>\n"
        f"Interval: <b>{_format_interval(interval)}</b> "
        f"(±{settings.jitter_seconds}s jitter)\n"
        f"Next sweep: <b>{next_run_str}</b>"
    )


async def _render_interval_message(context: ContextTypes.DEFAULT_TYPE) -> str:
    sched = _scheduler(context)
    current = sched.interval_seconds if sched else settings.check_interval
    next_run = context.application.bot_data.get("next_run")
    next_run_str = fmt_timestamp(next_run) if next_run else "—"
    return (
        "<b>⏱ Recheck interval</b>\n\n"
        f"Current: <b>{_format_interval(current)}</b> "
        f"(±{settings.jitter_seconds}s jitter)\n"
        f"Next sweep: <b>{next_run_str}</b>\n\n"
        f"Tap a preset, or send a custom value like "
        f"<code>45m</code>, <code>2h</code>, or <code>900s</code>.\n"
        f"Range: <code>{MIN_INTERVAL}s</code> – <code>{MAX_INTERVAL // 3600}h</code>."
    )


async def _apply_interval(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    seconds: int,
) -> None:
    sched = _scheduler(context)
    if sched is None:
        await _reply_or_edit(
            update,
            "Scheduler is unavailable — try again in a moment.",
            reply_markup=keyboards.back_to_menu(),
        )
        return
    applied = await sched.set_interval(seconds)
    if applied != seconds:
        note = f" (clamped to {_format_interval(applied)})"
    else:
        note = ""
    text = await _render_interval_message(context)
    await _reply_or_edit(
        update,
        f"✅ Interval set to <b>{_format_interval(applied)}</b>{note}.\n\n{text}",
        reply_markup=keyboards.interval_presets(applied),
    )


async def _render_history_message(username: str) -> str:
    async with get_session() as session:
        account = await crud.get_account(session, username)
        if not account:
            return f"<b>@{esc(username)}</b> is not monitored."
        notes = await crud.recent_notifications(session, account.id, limit=15)

    if not notes:
        return f"No recorded changes for <b>@{esc(username)}</b>."

    lines = [f"<b>📜 Recent changes for @{esc(username)}</b>", ""]
    for n in notes:
        ts = fmt_timestamp(n.created_at)
        payload = n.payload or {}
        if n.change_type == "fetch_failure":
            detail = (
                f"HTTP {payload.get('status')} — "
                f"{esc(str(payload.get('error')))}"
            )
        elif n.change_type == "profile_picture":
            detail = (
                f"pic hash {esc(str(payload.get('old'))[:8])}… → "
                f"{esc(str(payload.get('new'))[:8])}…"
            )
        else:
            old = payload.get("old")
            new = payload.get("new")
            detail = truncate(f"{esc(str(old))} → {esc(str(new))}", 200)
        lines.append(f"<code>{ts}</code>\n<b>{esc(n.change_type)}</b>: {detail}\n")

    return "\n".join(lines)


async def _build_csv_export() -> tuple[bytes, int]:
    async with get_session() as session:
        records = await crud.export_all(session)
        accounts = {
            a.id: a.username
            for a in await crud.list_accounts(session, only_active=False)
        }

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["timestamp_utc", "username", "change_type", "old", "new", "delivered"]
    )
    count = 0
    for r in records:
        payload = r.payload or {}
        writer.writerow(
            [
                r.created_at.isoformat() if r.created_at else "",
                accounts.get(r.account_id, ""),
                r.change_type,
                _stringify(payload.get("old")),
                _stringify(payload.get("new")),
                "yes" if r.delivered else "no",
            ]
        )
        count += 1

    return buf.getvalue().encode("utf-8"), count


def _stringify(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return str(value)
    return truncate(str(value), 500)


# ---------- Action implementations (used by both command and callback paths) ----------

async def _do_add(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    username: str,
    instagram_id: Optional[str] = None,
) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    chat_id = _chat_id(update)

    async with get_session() as session:
        account, created = await crud.add_account(
            session,
            username,
            added_by=user_id,
            instagram_id=instagram_id,
        )

    if not created:
        await _reply_or_edit(
            update,
            f"<b>@{esc(account.username)}</b> is already being monitored.",
            reply_markup=keyboards.open_account(account.username),
        )
        return

    await _reply_or_edit(
        update,
        f"Now monitoring <b>@{esc(account.username)}</b>.\nRunning first check…",
        reply_markup=keyboards.open_account(account.username),
    )

    service: MonitorService = context.application.bot_data["monitor"]
    try:
        result = await service.check_username(account.username, notify_unchanged=True)
        if not result.get("ok") and chat_id is not None:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"Initial fetch for <b>@{esc(account.username)}</b> failed: "
                    f"<code>{esc(str(result.get('error')))}</code>"
                ),
                parse_mode=ParseMode.HTML,
            )
    except Exception as exc:
        logger.exception("Initial check failed for {}: {}", account.username, exc)
        if chat_id is not None:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Initial check error: <code>{esc(repr(exc))}</code>",
                parse_mode=ParseMode.HTML,
            )


async def _send_profile_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    username: str,
) -> None:
    """Download the CURRENT profile picture at best quality and send it.

    Fetches fresh every time (the bot's media dir is ephemeral on Render, so a
    previously stored file may be gone) and works for any username, monitored
    or not.
    """
    chat_id = _chat_id(update)
    if chat_id is None:
        return
    query = update.callback_query
    if query is not None:
        await _safe_edit_text(
            query, f"⏳ Fetching profile picture for <b>@{esc(username)}</b>…"
        )
    service: MonitorService = context.application.bot_data["monitor"]
    result = await service.fetch_profile_picture(username)
    keyboard = await _actions_keyboard(username)
    if not result.get("ok"):
        await _reply_or_edit(
            update,
            f"Couldn't fetch profile picture for <b>@{esc(username)}</b>: "
            f"<code>{esc(str(result.get('error')))}</code>",
            reply_markup=keyboard,
        )
        return
    quality = (
        "HD" if result.get("hd")
        else "standard (320px — anonymous max for this account)"
    )
    caption = (
        f"<b>@{esc(username)}</b>\n"
        f"SHA256: <code>{esc(result['sha256'])}</code>\n"
        f"Size: {result['byte_size'] // 1024} KB · {quality}"
    )
    with open(result["path"], "rb") as f:
        await context.bot.send_document(
            chat_id=chat_id,
            document=f,
            filename=f"{username}_profile.jpg",
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    # Photo successfully sent — drop the now-redundant card that hosted the button.
    await _delete_callback_message(update)


async def _actions_keyboard(username: str):
    """Account-card actions when the user is monitored; lightweight story/
    highlights actions when it's an ad-hoc (non-monitored) lookup."""
    async with get_session() as session:
        account = await crud.get_account(session, username)
    return (
        keyboards.account_actions(username)
        if account is not None
        else keyboards.fetch_actions(username)
    )


async def _send_story_on_demand(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    username: str,
) -> None:
    """Fetch the account's current story right now, download it, and send it."""
    query = update.callback_query
    await _safe_answer(query, "Fetching story…")
    await _safe_edit_text(
        query, f"⏳ Fetching current story for <b>@{esc(username)}</b>…"
    )
    service: MonitorService = context.application.bot_data["monitor"]
    result = await service.fetch_and_send_stories(username)
    keyboard = await _actions_keyboard(username)
    if not result.get("ok"):
        await _safe_edit_text(
            query,
            f"Couldn't fetch story for <b>@{esc(username)}</b>: "
            f"<code>{esc(str(result.get('error')))}</code>",
            reply_markup=keyboard,
        )
        return
    count = result.get("count", 0)
    if count == 0:
        text = (
            f"<b>@{esc(username)}</b> has no active story right now "
            "(or the account is private)."
        )
    else:
        noun = "item" if count == 1 else "items"
        text = f"📖 Sent {count} story {noun} for <b>@{esc(username)}</b>."
    await _safe_edit_text(query, text, reply_markup=keyboard)


async def _show_highlights(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    username: str,
) -> None:
    """List the account's highlight names, each tappable to download."""
    query = update.callback_query
    await _safe_answer(query, "Loading highlights…")
    await _safe_edit_text(
        query, f"⏳ Loading highlights for <b>@{esc(username)}</b>…"
    )
    service: MonitorService = context.application.bot_data["monitor"]
    result = await service.list_highlights(username)
    if not result.get("ok"):
        await _safe_edit_text(
            query,
            f"Couldn't load highlights for <b>@{esc(username)}</b>: "
            f"<code>{esc(str(result.get('error')))}</code>",
            reply_markup=await _actions_keyboard(username),
        )
        return
    items = result.get("items", [])
    if not items:
        await _safe_edit_text(
            query,
            f"<b>@{esc(username)}</b> has no highlights.",
            reply_markup=await _actions_keyboard(username),
        )
        return
    lines = [f"<b>✨ Highlights for @{esc(username)}</b> ({len(items)})", ""]
    for i, (_hid, title) in enumerate(items, start=1):
        lines.append(f"{i}. {esc(title) or '(untitled)'}")
    lines.append("")
    lines.append("Tap a highlight below to download and send it.")
    await _safe_edit_text(
        query,
        "\n".join(lines),
        reply_markup=keyboards.highlights_view(username, items),
    )


async def _download_highlight(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    username: str,
    index_raw: str,
) -> None:
    """Download one highlight (by its list index) and send its items."""
    query = update.callback_query
    if not index_raw.lstrip("-").isdigit():
        await _safe_answer(query, "Invalid highlight.")
        return
    await _safe_answer(query, "Downloading…")
    service: MonitorService = context.application.bot_data["monitor"]
    result = await service.download_highlight(username, int(index_raw))
    title = result.get("title") or "(untitled)"
    if not result.get("ok"):
        await _safe_answer(
            query,
            str(result.get("error") or "Download failed."),
            show_alert=True,
        )
        return
    count = result.get("count", 0)
    if count == 0:
        await _safe_answer(query, f"No items in “{title}”.", show_alert=True)
    else:
        noun = "item" if count == 1 else "items"
        await _safe_answer(query, f"Sent {count} {noun} from “{title}”.")


async def _download_all_highlights(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    username: str,
) -> None:
    """Download and send every highlight reel for an account at once."""
    query = update.callback_query
    await _safe_answer(query, "Downloading all highlights…")
    await _safe_edit_text(
        query,
        f"⏳ Downloading <b>all</b> highlights for <b>@{esc(username)}</b>… "
        "this can take a while.",
    )
    service: MonitorService = context.application.bot_data["monitor"]
    result = await service.download_all_highlights(username)
    keyboard = await _actions_keyboard(username)
    if not result.get("ok"):
        await _safe_edit_text(
            query,
            f"Couldn't download highlights for <b>@{esc(username)}</b>: "
            f"<code>{esc(str(result.get('error')))}</code>",
            reply_markup=keyboard,
        )
        return
    count = result.get("count", 0)
    reels = result.get("reels", 0)
    if count == 0:
        text = f"<b>@{esc(username)}</b> has no highlights to download."
    else:
        item_noun = "item" if count == 1 else "items"
        reel_noun = "highlight" if reels == 1 else "highlights"
        text = (
            f"✨ Sent {count} {item_noun} from {reels} {reel_noun} "
            f"for <b>@{esc(username)}</b>."
        )
    await _safe_edit_text(query, text, reply_markup=keyboard)


async def _send_csv_export(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    chat_id = _chat_id(update)
    if chat_id is None:
        return
    data, count = await _build_csv_export()
    with tempfile.NamedTemporaryFile(
        prefix="watcher-export-", suffix=".csv", delete=False
    ) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        with open(tmp_path, "rb") as f:
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=f"watcher-export-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.csv",
                caption=f"Exported {count} notification rows",
                reply_markup=keyboards.back_to_menu(),
            )
        # The export landed — remove the menu message that triggered it so
        # the user isn't left with stale buttons above the document.
        await _delete_callback_message(update)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


# ---------- Command handlers ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    context.user_data.pop(_AWAITING_USERNAME, None)
    context.user_data.pop(_AWAITING_INTERVAL, None)
    await _consume_prompt_message(update, context)
    await _send_panel(update, context)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    context.user_data.pop(_AWAITING_USERNAME, None)
    context.user_data.pop(_AWAITING_INTERVAL, None)
    await _consume_prompt_message(update, context)
    await _send_panel(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    await update.message.reply_text(
        HELP_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.back_to_menu(),
        disable_web_page_preview=True,
    )


async def _resolve_username_for_instagram_id(
    context: ContextTypes.DEFAULT_TYPE,
    instagram_id: str,
) -> Optional[str]:
    service: MonitorService = context.application.bot_data["monitor"]
    return await service.instagram.fetch_username_by_id(instagram_id)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    if context.args:
        username, instagram_id = _parse_add_target(context.args[0])
        if not username and not instagram_id:
            await update.message.reply_text(
                "That doesn't look like a valid Instagram username or numeric user ID. "
                "Letters, numbers, dots, and underscores only (max 30 chars).",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboards.back_to_menu(),
            )
            return
        if instagram_id:
            username = await _resolve_username_for_instagram_id(
                context, instagram_id
            )
            if not username:
                await update.message.reply_text(
                    "Could not resolve that Instagram ID to a username. "
                    "Try a current numeric user ID or use @username instead.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboards.back_to_menu(),
                )
                return
            await _do_add(update, context, username, instagram_id=instagram_id)
            return
        await _do_add(update, context, username)
        return
    # No argument: enter the username-prompt state.
    # Clear any lingering prompt from a previous interaction first.
    await _consume_prompt_message(update, context)
    context.user_data[_AWAITING_USERNAME] = True
    prompt = await update.message.reply_text(
        "Send the Instagram <b>username</b>, <b>profile URL</b>, or <b>numeric user ID</b> you want to monitor.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.cancel_only(),
    )
    context.user_data[_PROMPT_MSG_ID] = prompt.message_id


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    username = _username_arg(context)
    if not username:
        await update.message.reply_text(
            "Usage: <code>/remove &lt;username&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    async with get_session() as session:
        removed = await crud.remove_account(session, username)
    if removed:
        await update.message.reply_text(
            f"🗑 Removed <b>@{esc(username)}</b> from monitoring.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.back_to_menu(),
        )
    else:
        await update.message.reply_text(
            f"<b>@{esc(username)}</b> wasn't monitored.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.back_to_menu(),
        )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    async with get_session() as session:
        accounts = await crud.list_accounts(session, only_active=False)
    if not accounts:
        await update.message.reply_text(
            "<b>No accounts monitored yet.</b>\n\n"
            "Tap ➕ Add account to start watching an Instagram profile.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.main_menu(),
        )
        return
    await update.message.reply_text(
        f"<b>Monitored accounts</b> ({len(accounts)})\n"
        "Tap an account to see actions.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.accounts_list(accounts, page=0),
        disable_web_page_preview=True,
    )


async def cmd_recheck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    username = _username_arg(context)
    if not username:
        await update.message.reply_text(
            "Usage: <code>/recheck &lt;username&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(
        f"⏳ Forcing check for <b>@{esc(username)}</b>…",
        parse_mode=ParseMode.HTML,
    )

    service: MonitorService = context.application.bot_data["monitor"]
    result = await service.check_username(username, notify_unchanged=True)

    if not result.get("ok"):
        await update.message.reply_text(
            f"Check failed: <code>{esc(str(result.get('error')))}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.back_to_menu(),
        )
        return

    username = result.get("username", username)
    msg = (
        f"<b>@{esc(username)}</b> check done · status {result['status']} · "
        f"{'CHANGES' if result.get('changed') else 'no changes'}"
    )
    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.account_actions(username),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    text = await _render_status_message(context)
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.status_actions(),
    )


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    username = _username_arg(context)
    if not username:
        await update.message.reply_text(
            "Usage: <code>/history &lt;username&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    text = await _render_history_message(username)
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.account_actions(username),
        disable_web_page_preview=True,
    )


async def cmd_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    username = _username_arg(context)
    if not username:
        await update.message.reply_text(
            "Usage: <code>/photo &lt;username&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await _send_profile_photo(update, context, username)


async def cmd_fetchphoto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch and send the current profile picture (max quality) for any username."""
    if await _reject_if_unauthorized(update):
        return
    username = _username_arg(context)
    if not username:
        await update.message.reply_text(
            "Usage: <code>/fetchphoto &lt;username&gt;</code>\n"
            "Downloads the current Instagram profile picture at best quality, "
            "without adding the account to monitoring.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.back_to_menu(),
        )
        return
    await _send_profile_photo(update, context, username)


async def cmd_story(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download and send the current story for any public username, monitored or not."""
    if await _reject_if_unauthorized(update):
        return
    username = _username_arg(context)
    if not username:
        await update.message.reply_text(
            "Usage: <code>/story &lt;username&gt;</code>\n"
            "Downloads the account's current story now — works for any public "
            "account, monitored or not.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.back_to_menu(),
        )
        return
    status_msg = await update.message.reply_text(
        f"⏳ Fetching current story for <b>@{esc(username)}</b>…",
        parse_mode=ParseMode.HTML,
    )
    service: MonitorService = context.application.bot_data["monitor"]
    result = await service.fetch_and_send_stories(username)
    if not result.get("ok"):
        await status_msg.edit_text(
            f"Couldn't fetch story for <b>@{esc(username)}</b>: "
            f"<code>{esc(str(result.get('error')))}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.fetch_actions(username),
        )
        return
    count = result.get("count", 0)
    if count == 0:
        text = (
            f"<b>@{esc(username)}</b> has no active story right now "
            "(or the account is private)."
        )
    else:
        noun = "item" if count == 1 else "items"
        text = f"📖 Sent {count} story {noun} for <b>@{esc(username)}</b>."
    await status_msg.edit_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboards.fetch_actions(username)
    )


async def cmd_highlights(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List highlights (each tappable to download) for any public username."""
    if await _reject_if_unauthorized(update):
        return
    username = _username_arg(context)
    if not username:
        await update.message.reply_text(
            "Usage: <code>/highlights &lt;username&gt;</code>\n"
            "Lists an account's highlights to download — works for any public account.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.back_to_menu(),
        )
        return
    status_msg = await update.message.reply_text(
        f"⏳ Loading highlights for <b>@{esc(username)}</b>…",
        parse_mode=ParseMode.HTML,
    )
    service: MonitorService = context.application.bot_data["monitor"]
    result = await service.list_highlights(username)
    if not result.get("ok"):
        await status_msg.edit_text(
            f"Couldn't load highlights for <b>@{esc(username)}</b>: "
            f"<code>{esc(str(result.get('error')))}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.fetch_actions(username),
        )
        return
    items = result.get("items", [])
    if not items:
        await status_msg.edit_text(
            f"<b>@{esc(username)}</b> has no highlights.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.fetch_actions(username),
        )
        return
    lines = [f"<b>✨ Highlights for @{esc(username)}</b> ({len(items)})", ""]
    for i, (_hid, title) in enumerate(items, start=1):
        lines.append(f"{i}. {esc(title) or '(untitled)'}")
    lines.append("")
    lines.append("Tap a highlight below to download and send it.")
    await status_msg.edit_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.highlights_view(username, items),
    )


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    await _send_csv_export(update, context)


async def cmd_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    context.user_data.pop(_AWAITING_INTERVAL, None)

    if not context.args:
        sched = _scheduler(context)
        current = sched.interval_seconds if sched else settings.check_interval
        text = await _render_interval_message(context)
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.interval_presets(current),
            disable_web_page_preview=True,
        )
        return

    seconds = _parse_interval(" ".join(context.args))
    if seconds is None:
        await update.message.reply_text(
            "Couldn't read that as a duration. Examples: "
            "<code>30m</code>, <code>1h</code>, <code>1800s</code>, <code>1800</code>.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.back_to_menu(),
        )
        return
    if not MIN_INTERVAL <= seconds <= MAX_INTERVAL:
        await update.message.reply_text(
            f"Out of range. Use between <code>{MIN_INTERVAL}s</code> and "
            f"<code>{MAX_INTERVAL // 3600}h</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    await _apply_interval(update, context, seconds)


# ---------- Text capture (for the "Add account" prompt) ----------

async def on_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return

    if context.user_data.get(_AWAITING_INTERVAL):
        context.user_data.pop(_AWAITING_INTERVAL, None)
        # The user has answered the prompt — clear the message that's still
        # showing a Cancel button so they don't see a stale control.
        await _consume_prompt_message(update, context)

        raw = (update.message.text or "").strip()
        seconds = _parse_interval(raw)
        if seconds is None:
            await update.message.reply_text(
                "Couldn't read that as a duration. Examples: "
                "<code>30m</code>, <code>1h</code>, <code>1800s</code>.",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboards.interval_presets(
                    _scheduler(context).interval_seconds
                    if _scheduler(context)
                    else settings.check_interval
                ),
            )
            return
        if not MIN_INTERVAL <= seconds <= MAX_INTERVAL:
            await update.message.reply_text(
                f"Out of range. Use between <code>{MIN_INTERVAL}s</code> and "
                f"<code>{MAX_INTERVAL // 3600}h</code>.",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboards.interval_presets(
                    _scheduler(context).interval_seconds
                    if _scheduler(context)
                    else settings.check_interval
                ),
            )
            return
        await _apply_interval(update, context, seconds)
        return

    if context.user_data.get(_AWAITING_FETCH_USERNAME):
        context.user_data.pop(_AWAITING_FETCH_USERNAME, None)
        await _consume_prompt_message(update, context)
        username = _normalize_username((update.message.text or "").strip())
        if not username:
            await update.message.reply_text(
                "That doesn't look like a valid Instagram username. "
                "Letters, numbers, dots, and underscores only.",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboards.main_menu(),
            )
            return
        await update.message.reply_text(
            f"<b>@{esc(username)}</b> — grab its public story or highlights:",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.fetch_actions(username),
        )
        return

    if not context.user_data.get(_AWAITING_USERNAME):
        return  # Ignore typed text unless we're waiting for input.
    context.user_data.pop(_AWAITING_USERNAME, None)
    await _consume_prompt_message(update, context)

    raw = (update.message.text or "").strip()
    username, instagram_id = _parse_add_target(raw)
    if not username and not instagram_id:
        await update.message.reply_text(
            "That doesn't look like a valid Instagram username or numeric user ID. "
            "Letters, numbers, dots, and underscores only (max 30 chars).",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.main_menu(),
        )
        return
    if instagram_id:
        username = await _resolve_username_for_instagram_id(context, instagram_id)
        if not username:
            await update.message.reply_text(
                "Could not resolve that Instagram ID to a username. "
                "Try a current numeric user ID or use @username instead.",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboards.main_menu(),
            )
            return
        await _do_add(update, context, username, instagram_id=instagram_id)
        return
    await _do_add(update, context, username)


# ---------- Callback router ----------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    query = update.callback_query
    data = query.data or ""
    current_msg_id = query.message.message_id if query.message else None

    # A new button press supersedes any pending text prompt — but keep the
    # awaiting flag alive if this same press is the one that opens that prompt.
    keep_interval_prompt = (data == "menu:setinterval:custom")
    keep_username_prompt = (data == "menu:add")
    keep_fetch_prompt = (data == "menu:fetch")
    if not keep_interval_prompt:
        context.user_data.pop(_AWAITING_INTERVAL, None)
    if not keep_username_prompt:
        context.user_data.pop(_AWAITING_USERNAME, None)
    if not keep_fetch_prompt:
        context.user_data.pop(_AWAITING_FETCH_USERNAME, None)

    # If the user has a Cancel-button prompt left over on a different message
    # than the one they're now interacting with, remove the orphaned prompt
    # so they don't see stale buttons.
    stale_prompt_id = context.user_data.get(_PROMPT_MSG_ID)
    if stale_prompt_id is not None and stale_prompt_id != current_msg_id:
        chat_id = _chat_id(update)
        if chat_id is not None:
            try:
                await context.bot.delete_message(
                    chat_id=chat_id, message_id=stale_prompt_id
                )
            except (BadRequest, Forbidden, TelegramError):
                pass
        context.user_data.pop(_PROMPT_MSG_ID, None)
    elif not (keep_interval_prompt or keep_username_prompt):
        # Same message, but we're navigating it away from being a prompt.
        context.user_data.pop(_PROMPT_MSG_ID, None)

    if data == "noop":
        await _safe_answer(query)
        return

    parts = data.split(":")
    try:
        if parts[0] == "menu":
            await _handle_menu(update, context, parts[1:])
        elif parts[0] == "acc":
            await _handle_account(update, context, parts[1:])
        else:
            await _safe_answer(query, "Unknown action.")
    except Exception as exc:
        logger.exception("Callback handler failed for {}: {}", data, exc)
        await _safe_answer(query, "Something went wrong. Check logs.", show_alert=True)


async def _handle_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parts: list[str],
) -> None:
    query = update.callback_query
    if not parts:
        await _safe_answer(query)
        return
    action = parts[0]

    if action == "main":
        await _safe_answer(query)
        result = await _safe_edit_text(
            query, WELCOME_TEXT, reply_markup=keyboards.main_menu()
        )
        # result may be a new message if the original was a media message
        panel = result or query.message
        if panel:
            mid = panel.message_id
            cid = panel.chat_id
            context.application.bot_data[PANEL_MSG_ID] = mid
            context.application.bot_data[PANEL_CHAT_ID] = cid
            async with get_session() as session:
                await crud.set_setting(session, "panel_msg_id", str(mid))
                await crud.set_setting(session, "panel_chat_id", str(cid))
        return

    if action == "list":
        await _safe_answer(query)
        page = (
            int(parts[1])
            if len(parts) > 1 and parts[1].lstrip("-").isdigit()
            else 0
        )
        async with get_session() as session:
            accounts = await crud.list_accounts(session, only_active=False)
        if not accounts:
            await _safe_edit_text(
                query,
                "<b>No accounts monitored yet.</b>\n\n"
                "Tap ➕ Add account to start watching an Instagram profile.",
                reply_markup=keyboards.main_menu(),
            )
            return
        await _safe_edit_text(
            query,
            f"<b>Monitored accounts</b> ({len(accounts)})\n"
            "Tap an account to see actions.",
            reply_markup=keyboards.accounts_list(accounts, page=page),
        )
        return

    if action == "status":
        await _safe_answer(query)
        text = await _render_status_message(context)
        await _safe_edit_text(
            query, text, reply_markup=keyboards.status_actions()
        )
        return

    if action == "interval":
        await _safe_answer(query)
        sched = _scheduler(context)
        current = sched.interval_seconds if sched else settings.check_interval
        text = await _render_interval_message(context)
        await _safe_edit_text(
            query, text, reply_markup=keyboards.interval_presets(current)
        )
        return

    if action == "setinterval":
        choice = parts[1] if len(parts) > 1 else ""
        if choice == "custom":
            await _safe_answer(query)
            context.user_data[_AWAITING_INTERVAL] = True
            if query.message:
                context.user_data[_PROMPT_MSG_ID] = query.message.message_id
            await _safe_edit_text(
                query,
                "Send a duration like <code>45m</code>, <code>2h</code>, "
                "or <code>900s</code>.",
                reply_markup=keyboards.cancel_only(),
            )
            return
        if not choice.isdigit():
            await _safe_answer(query, "Invalid preset.")
            return
        seconds = int(choice)
        if not MIN_INTERVAL <= seconds <= MAX_INTERVAL:
            await _safe_answer(query, "Out of range.")
            return
        await _safe_answer(query, "Updating…")
        await _apply_interval(update, context, seconds)
        return

    if action == "add":
        await _safe_answer(query)
        context.user_data[_AWAITING_USERNAME] = True
        if query.message:
            context.user_data[_PROMPT_MSG_ID] = query.message.message_id
        await _safe_edit_text(
            query,
            "Send the Instagram <b>username</b>, <b>profile URL</b>, or <b>numeric user ID</b> you want to monitor.",
            reply_markup=keyboards.cancel_only(),
        )
        return

    if action == "fetch":
        await _safe_answer(query)
        context.user_data[_AWAITING_FETCH_USERNAME] = True
        if query.message:
            context.user_data[_PROMPT_MSG_ID] = query.message.message_id
        await _safe_edit_text(
            query,
            "Send any public Instagram <b>username</b> to grab its current "
            "<b>story</b> or <b>highlights</b>.\n\n"
            "<i>It won't be added to monitoring.</i>",
            reply_markup=keyboards.cancel_only(),
        )
        return

    if action == "export":
        await _safe_answer(query, "Building CSV…")
        await _send_csv_export(update, context)
        return

    if action == "help":
        await _safe_answer(query)
        await _safe_edit_text(
            query, HELP_TEXT, reply_markup=keyboards.back_to_menu()
        )
        return

    if action == "sweep":
        backfill_ids = len(parts) > 1 and parts[1] == "ids"
        sched = _scheduler(context)
        if sched is None:
            await _safe_answer(query, "Scheduler unavailable.", show_alert=True)
            return
        if sched.sweep_in_flight:
            await _safe_answer(query, "⏳ Sweep already in progress.", show_alert=True)
            return
        alert = "Sweep started — also fetching missing Instagram IDs!" if backfill_ids else "Sweep started!"
        await _safe_answer(query, alert)
        asyncio.create_task(sched.trigger_now(backfill_ids=backfill_ids))
        text = await _render_status_message(context)
        running_msg = "🔄 Sweep running"
        if backfill_ids:
            running_msg += " (resolving Instagram IDs + checking profiles)"
        await _safe_edit_text(
            query,
            f"{running_msg} — results will appear in the chat.\n\n{text}",
            reply_markup=keyboards.status_actions(),
        )
        return

    if action == "cleardb":
        await _safe_answer(query)
        await _safe_edit_text(
            query,
            "⚠️ <b>Clear history?</b>\n\n"
            "This will delete all snapshots (except the latest per account), "
            "all notification logs, seen stories, and stored highlight catalogs.\n"
            "Monitored accounts will not be affected.",
            reply_markup=keyboards.confirm_clear_db(),
        )
        return

    if action == "cleardb_yes":
        await _safe_answer(query, "Clearing…")
        async with get_session() as session:
            totals = await crud.clear_history(session)
        await _safe_edit_text(
            query,
            "✅ <b>History cleared</b>\n\n"
            f"Snapshots deleted: <b>{totals['snapshots_deleted']}</b>\n"
            f"Notifications deleted: <b>{totals['notifications_deleted']}</b>\n"
            f"Seen stories deleted: <b>{totals['stories_deleted']}</b>\n"
            f"Highlight catalogs cleared: <b>{totals.get('highlights_deleted', 0)}</b>\n\n"
            "<i>Latest snapshot per account was kept as the change-detection baseline.</i>",
            reply_markup=keyboards.back_to_menu(),
        )
        return

    await _safe_answer(query, "Unknown menu action.")


async def _handle_account(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parts: list[str],
) -> None:
    query = update.callback_query
    if len(parts) < 2:
        await _safe_answer(query)
        return
    action = parts[0]

    if action == "hldl":
        # acc:hldl:<index>:<username> — index references the highlights list.
        if len(parts) < 3:
            await _safe_answer(query)
            return
        hl_username = _normalize_username(parts[2]) or parts[2].lower()
        await _download_highlight(update, context, hl_username, parts[1])
        return

    username = _normalize_username(parts[1]) or parts[1].lower()

    if action == "story":
        await _send_story_on_demand(update, context, username)
        return

    if action == "highlights":
        await _show_highlights(update, context, username)
        return

    if action == "hlall":
        await _download_all_highlights(update, context, username)
        return

    if action == "open":
        await _safe_answer(query)
        service: MonitorService = context.application.bot_data["monitor"]
        text = await _render_account_card(username, service)
        if text is None:
            # Not monitored — still let the user grab its public story/highlights.
            await _safe_edit_text(
                query,
                f"<b>@{esc(username)}</b> isn't monitored, but you can still grab "
                "its public story or highlights:",
                reply_markup=keyboards.fetch_actions(username),
            )
            return
        await _safe_edit_text(
            query, text, reply_markup=keyboards.account_actions(username)
        )
        return

    if action == "recheck":
        await _safe_answer(query, "Checking…")
        await _safe_edit_text(
            query, f"⏳ Forcing check for <b>@{esc(username)}</b>…"
        )
        service: MonitorService = context.application.bot_data["monitor"]
        result = await service.check_username(username, notify_unchanged=True)
        if not result.get("ok"):
            await _safe_edit_text(
                query,
                f"Check for <b>@{esc(username)}</b> failed: "
                f"<code>{esc(str(result.get('error')))}</code>",
                reply_markup=keyboards.account_actions(username),
            )
            return
        username = result.get("username", username)
        text = await _render_account_card(username, service)
        suffix = (
            "\n\n<i>Changes detected ✓</i>"
            if result.get("changed")
            else "\n\n<i>No changes.</i>"
        )
        if text is None:
            await _safe_edit_text(
                query,
                f"<b>@{esc(username)}</b> check done · status "
                f"{result['status']}{suffix}",
                reply_markup=keyboards.back_to_list(),
            )
            return
        await _safe_edit_text(
            query, text + suffix, reply_markup=keyboards.account_actions(username)
        )
        return

    if action == "history":
        await _safe_answer(query)
        text = await _render_history_message(username)
        await _safe_edit_text(
            query, text, reply_markup=keyboards.account_actions(username)
        )
        return

    if action == "photo":
        await _safe_answer(query, "Sending photo…")
        await _send_profile_photo(update, context, username)
        return

    if action == "remove":
        await _safe_answer(query)
        await _safe_edit_text(
            query,
            f"⚠️ Remove <b>@{esc(username)}</b> from monitoring?\n"
            "Snapshots and change history will be deleted.",
            reply_markup=keyboards.confirm_remove(username),
        )
        return

    if action == "remove_yes":
        await _safe_answer(query, "Removing…")
        async with get_session() as session:
            removed = await crud.remove_account(session, username)
        await _safe_edit_text(
            query,
            (
                f"🗑 Removed <b>@{esc(username)}</b> from monitoring."
                if removed
                else f"<b>@{esc(username)}</b> wasn't monitored."
            ),
            reply_markup=keyboards.back_to_list(),
        )
        return

    await _safe_answer(query, "Unknown action.")


# ---------- Errors / unknown commands ----------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Telegram handler error: {}", context.error)


async def _unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    if update.message:
        await update.message.reply_text(
            "Unknown command. Try /menu or /help.",
            reply_markup=keyboards.main_menu(),
        )


# ---------- Registration ----------

def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("rm", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("recheck", cmd_recheck))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("interval", cmd_interval))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("photo", cmd_photo))
    app.add_handler(CommandHandler("fetchphoto", cmd_fetchphoto))
    app.add_handler(CommandHandler("story", cmd_story))
    app.add_handler(CommandHandler("highlights", cmd_highlights))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CallbackQueryHandler(on_callback))
    # Plain text — used to capture usernames after the Add prompt.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_plain_text))
    # Unknown slash-commands (commands above already matched).
    app.add_handler(MessageHandler(filters.COMMAND, _unknown_command))
    app.add_error_handler(on_error)
