"""Telegram bot command handlers and inline-button callback routing."""

from __future__ import annotations

import csv
import io
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from telegram import BotCommand, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
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


# ---------- Static text & bot menu ----------

WELCOME_TEXT = (
    "<b>👁 The Watcher</b>\n"
    "<i>Instagram intelligence monitoring</i>\n\n"
    "Tap a button below, or send a command (for example "
    "<code>/add @username</code>)."
)

HELP_TEXT = (
    "<b>👁 The Watcher — help</b>\n\n"
    "<b>How it works</b>\n"
    "Use the inline buttons to navigate. Open an account to see its actions: "
    "Recheck · History · Photo · Remove. The 🏠 button always returns to the "
    "main menu.\n\n"
    "<b>Commands</b>\n"
    "<code>/menu</code> — open the main menu\n"
    "<code>/add @user</code> — start monitoring\n"
    "<code>/remove @user</code> — stop monitoring\n"
    "<code>/list</code> — paginated accounts list\n"
    "<code>/recheck @user</code> — force a check now\n"
    "<code>/status</code> — global monitoring stats\n"
    "<code>/history @user</code> — recent changes\n"
    "<code>/photo @user</code> — current profile picture\n"
    "<code>/export</code> — CSV of all changes"
)

BOT_COMMANDS: list[BotCommand] = [
    BotCommand("menu", "Open the main menu"),
    BotCommand("add", "Start monitoring an account"),
    BotCommand("remove", "Stop monitoring an account"),
    BotCommand("list", "List monitored accounts"),
    BotCommand("status", "Show monitoring statistics"),
    BotCommand("recheck", "Force a check for a username"),
    BotCommand("history", "Recent changes for a username"),
    BotCommand("photo", "Current profile picture"),
    BotCommand("export", "Export change history as CSV"),
    BotCommand("help", "Show help"),
]

_AWAITING_USERNAME = "awaiting_username"
# Instagram usernames: 1–30 chars, ASCII letters/digits/dots/underscores.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


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


def _chat_id(update: Update) -> Optional[int]:
    chat = update.effective_chat
    return chat.id if chat else None


async def _safe_edit_text(
    query,
    text: str,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = ParseMode.HTML,
) -> None:
    """Edit a callback message, swallowing harmless "not modified" / "not found" errors."""
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        msg = str(exc).lower()
        if "not modified" in msg or "message to edit not found" in msg:
            return
        raise


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

async def _render_account_card(username: str) -> Optional[str]:
    async with get_session() as session:
        account = await crud.get_account(session, username)
        if not account:
            return None
        snapshot = await crud.get_latest_snapshot(
            session, account.id, successful_only=True
        )
        media = await crud.latest_media_hash(session, account.id)

    marker = "🟢 active" if account.active else "⏸ paused"
    last = fmt_timestamp(account.last_checked_at) if account.last_checked_at else "never"
    status = f"HTTP {account.last_status_code}" if account.last_status_code else "—"
    fails = account.consecutive_failures or 0

    lines = [
        f"<b>@{esc(account.username)}</b>",
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

    if media:
        lines.append("")
        lines.append(
            f"Profile picture captured: <code>{fmt_timestamp(media.created_at)}</code>"
        )

    return "\n".join(lines)


async def _render_status_message(context: ContextTypes.DEFAULT_TYPE) -> str:
    async with get_session() as session:
        stats = await crud.stats_summary(session)

    scheduler_state = context.application.bot_data.get("scheduler_state", "unknown")
    next_run = context.application.bot_data.get("next_run")
    next_run_str = fmt_timestamp(next_run) if next_run else "—"

    return (
        "<b>📊 Watcher status</b>\n\n"
        f"Accounts: <b>{stats['accounts_total']}</b> "
        f"(active: <b>{stats['accounts_active']}</b>)\n"
        f"Snapshots stored: <b>{fmt_number(stats['snapshots_total'])}</b>\n"
        f"Notifications sent: <b>{fmt_number(stats['notifications_total'])}</b>\n\n"
        f"Scheduler: <b>{esc(str(scheduler_state))}</b>\n"
        f"Interval: <b>{settings.check_interval}s</b> "
        f"(±{settings.jitter_seconds}s jitter)\n"
        f"Next sweep: <b>{next_run_str}</b>"
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


async def _resolve_profile_photo(
    username: str,
) -> tuple[Optional[Path], str]:
    """Return (path, caption_or_error_text). When path is None, the caption is an error message."""
    async with get_session() as session:
        account = await crud.get_account(session, username)
        if not account:
            return None, f"<b>@{esc(username)}</b> is not monitored."
        media = await crud.latest_media_hash(session, account.id)

    if not media or not media.local_path:
        return None, f"No stored profile picture for <b>@{esc(username)}</b> yet."
    path = Path(media.local_path)
    if not path.exists():
        return None, "Stored profile picture file is missing on disk."

    caption = (
        f"<b>@{esc(username)}</b>\n"
        f"SHA256: <code>{esc(media.sha256)}</code>\n"
        f"Captured: <code>{fmt_timestamp(media.created_at)}</code>"
    )
    return path, caption


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
) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    chat_id = _chat_id(update)

    async with get_session() as session:
        account, created = await crud.add_account(session, username, added_by=user_id)

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
    chat_id = _chat_id(update)
    path, caption_or_err = await _resolve_profile_photo(username)
    if path is None:
        await _reply_or_edit(
            update,
            caption_or_err,
            reply_markup=keyboards.account_actions(username),
        )
        return
    if chat_id is None:
        return
    with open(path, "rb") as f:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=f,
            caption=caption_or_err,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.account_actions(username),
        )


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
    await update.message.reply_text(
        WELCOME_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.main_menu(),
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    context.user_data.pop(_AWAITING_USERNAME, None)
    await update.message.reply_text(
        WELCOME_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.main_menu(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    await update.message.reply_text(
        HELP_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.back_to_menu(),
        disable_web_page_preview=True,
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    if context.args:
        username = _normalize_username(context.args[0])
        if not username:
            await update.message.reply_text(
                "That doesn't look like a valid Instagram username. "
                "Letters, numbers, dots, and underscores only (max 30 chars).",
                reply_markup=keyboards.back_to_menu(),
            )
            return
        await _do_add(update, context, username)
        return
    # No argument: enter the username-prompt state.
    context.user_data[_AWAITING_USERNAME] = True
    await update.message.reply_text(
        "Send the Instagram <b>username</b> you want to monitor "
        "(with or without <code>@</code>).",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.cancel_only(),
    )


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
        reply_markup=keyboards.back_to_menu(),
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


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    await _send_csv_export(update, context)


# ---------- Text capture (for the "Add account" prompt) ----------

async def on_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    if not context.user_data.get(_AWAITING_USERNAME):
        return  # Ignore typed text unless we're waiting for a username.
    context.user_data.pop(_AWAITING_USERNAME, None)

    raw = (update.message.text or "").strip()
    username = _normalize_username(raw)
    if not username:
        await update.message.reply_text(
            "That doesn't look like a valid Instagram username. "
            "Letters, numbers, dots, and underscores only (max 30 chars).",
            reply_markup=keyboards.main_menu(),
        )
        return
    await _do_add(update, context, username)


# ---------- Callback router ----------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    query = update.callback_query
    data = query.data or ""
    # A new button press supersedes any pending text prompt.
    context.user_data.pop(_AWAITING_USERNAME, None)

    if data == "noop":
        await query.answer()
        return

    parts = data.split(":")
    try:
        if parts[0] == "menu":
            await _handle_menu(update, context, parts[1:])
        elif parts[0] == "acc":
            await _handle_account(update, context, parts[1:])
        else:
            await query.answer("Unknown action.")
    except Exception as exc:
        logger.exception("Callback handler failed for {}: {}", data, exc)
        try:
            await query.answer("Something went wrong. Check logs.", show_alert=True)
        except Exception:
            pass


async def _handle_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parts: list[str],
) -> None:
    query = update.callback_query
    if not parts:
        await query.answer()
        return
    action = parts[0]

    if action == "main":
        await query.answer()
        await _safe_edit_text(
            query, WELCOME_TEXT, reply_markup=keyboards.main_menu()
        )
        return

    if action == "list":
        page = (
            int(parts[1])
            if len(parts) > 1 and parts[1].lstrip("-").isdigit()
            else 0
        )
        async with get_session() as session:
            accounts = await crud.list_accounts(session, only_active=False)
        await query.answer()
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
        await query.answer()
        text = await _render_status_message(context)
        await _safe_edit_text(
            query, text, reply_markup=keyboards.back_to_menu()
        )
        return

    if action == "add":
        context.user_data[_AWAITING_USERNAME] = True
        await query.answer()
        await _safe_edit_text(
            query,
            "Send the Instagram <b>username</b> you want to monitor "
            "(with or without <code>@</code>).",
            reply_markup=keyboards.cancel_only(),
        )
        return

    if action == "export":
        await query.answer("Building CSV…")
        await _send_csv_export(update, context)
        return

    if action == "help":
        await query.answer()
        await _safe_edit_text(
            query, HELP_TEXT, reply_markup=keyboards.back_to_menu()
        )
        return

    await query.answer("Unknown menu action.")


async def _handle_account(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parts: list[str],
) -> None:
    query = update.callback_query
    if len(parts) < 2:
        await query.answer()
        return
    action = parts[0]
    username = _normalize_username(parts[1]) or parts[1].lower()

    if action == "open":
        await query.answer()
        text = await _render_account_card(username)
        if text is None:
            await _safe_edit_text(
                query,
                f"<b>@{esc(username)}</b> is not monitored.",
                reply_markup=keyboards.back_to_list(),
            )
            return
        await _safe_edit_text(
            query, text, reply_markup=keyboards.account_actions(username)
        )
        return

    if action == "recheck":
        await query.answer("Checking…")
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
        text = await _render_account_card(username)
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
        await query.answer()
        text = await _render_history_message(username)
        await _safe_edit_text(
            query, text, reply_markup=keyboards.account_actions(username)
        )
        return

    if action == "photo":
        await query.answer("Sending photo…")
        await _send_profile_photo(update, context, username)
        return

    if action == "remove":
        await query.answer()
        await _safe_edit_text(
            query,
            f"⚠️ Remove <b>@{esc(username)}</b> from monitoring?\n"
            "Snapshots and change history will be deleted.",
            reply_markup=keyboards.confirm_remove(username),
        )
        return

    if action == "remove_yes":
        async with get_session() as session:
            removed = await crud.remove_account(session, username)
        await query.answer("Removed." if removed else "Not monitored.")
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

    await query.answer("Unknown action.")


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
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("photo", cmd_photo))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CallbackQueryHandler(on_callback))
    # Plain text — used to capture usernames after the Add prompt.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_plain_text))
    # Unknown slash-commands (commands above already matched).
    app.add_handler(MessageHandler(filters.COMMAND, _unknown_command))
    app.add_error_handler(on_error)
