"""Telegram bot command handlers."""

from __future__ import annotations

import csv
import io
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import settings
from app.database import crud
from app.database.session import get_session
from app.monitor.service import MonitorService
from app.utils.formatting import esc, fmt_number, fmt_timestamp, truncate
from app.utils.logger import logger

HELP_TEXT = """\
<b>The Watcher — Instagram intel bot</b>

<b>Commands:</b>
/add &lt;username&gt; — start monitoring an account
/remove &lt;username&gt; — stop monitoring an account
/list — show all monitored accounts
/recheck &lt;username&gt; — force an immediate check
/status — global monitoring stats
/history &lt;username&gt; — recent changes for an account
/photo &lt;username&gt; — current profile picture
/export — export change history as CSV
/help — show this message
"""


# ---------- Authorization ----------

def _is_authorized(update: Update) -> bool:
    admins = settings.admin_ids
    if not admins:
        return True  # No admin list configured — allow all
    user = update.effective_user
    chat = update.effective_chat
    if user and user.id in admins:
        return True
    if chat and chat.id in admins:
        return True
    return False


def _username_arg(context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    if not context.args:
        return None
    raw = context.args[0].strip().lstrip("@")
    return raw.lower() if raw else None


async def _reject_if_unauthorized(update: Update) -> bool:
    if _is_authorized(update):
        return False
    if update.message:
        await update.message.reply_text("Unauthorized.")
    return True


# ---------- Command implementations ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    username = _username_arg(context)
    if not username:
        await update.message.reply_text("Usage: /add <username>")
        return

    async with get_session() as session:
        account, created = await crud.add_account(
            session, username, added_by=update.effective_user.id if update.effective_user else None
        )

    if created:
        await update.message.reply_text(
            f"Now monitoring <b>@{esc(account.username)}</b>. "
            f"Running first check…",
            parse_mode=ParseMode.HTML,
        )
        # Kick off an immediate check
        service: MonitorService = context.application.bot_data["monitor"]
        try:
            result = await service.check_username(account.username, notify_unchanged=True)
            if not result.get("ok"):
                await update.message.reply_text(
                    f"Initial fetch failed: <code>{esc(str(result.get('error')))}</code>",
                    parse_mode=ParseMode.HTML,
                )
        except Exception as exc:
            logger.exception("Initial check failed for {}: {}", account.username, exc)
            await update.message.reply_text(f"Initial check error: {exc!r}")
    else:
        await update.message.reply_text(
            f"<b>@{esc(account.username)}</b> is already being monitored.",
            parse_mode=ParseMode.HTML,
        )


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    username = _username_arg(context)
    if not username:
        await update.message.reply_text("Usage: /remove <username>")
        return

    async with get_session() as session:
        removed = await crud.remove_account(session, username)

    if removed:
        await update.message.reply_text(
            f"Removed <b>@{esc(username)}</b> from monitoring.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            f"<b>@{esc(username)}</b> wasn't monitored.",
            parse_mode=ParseMode.HTML,
        )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    async with get_session() as session:
        accounts = await crud.list_accounts(session, only_active=False)

    if not accounts:
        await update.message.reply_text("No accounts are being monitored.")
        return

    lines = ["<b>Monitored accounts:</b>", ""]
    for a in accounts:
        marker = "🟢" if a.active else "⏸"
        last = fmt_timestamp(a.last_checked_at) if a.last_checked_at else "never"
        status = f"HTTP {a.last_status_code}" if a.last_status_code else "—"
        lines.append(
            f"{marker} <b>@{esc(a.username)}</b> · last: {last} · {status}"
        )
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_recheck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    username = _username_arg(context)
    if not username:
        await update.message.reply_text("Usage: /recheck <username>")
        return

    await update.message.reply_text(
        f"Forcing check for <b>@{esc(username)}</b>…", parse_mode=ParseMode.HTML
    )

    service: MonitorService = context.application.bot_data["monitor"]
    result = await service.check_username(username, notify_unchanged=True)

    if not result.get("ok"):
        await update.message.reply_text(
            f"Check failed: <code>{esc(str(result.get('error')))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    msg = (
        f"<b>@{esc(username)}</b> check done · status {result['status']} · "
        f"{'CHANGES' if result.get('changed') else 'no changes'}"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    async with get_session() as session:
        stats = await crud.stats_summary(session)

    scheduler_state = context.application.bot_data.get("scheduler_state", "unknown")
    next_run = context.application.bot_data.get("next_run")
    next_run_str = fmt_timestamp(next_run) if next_run else "—"

    msg = (
        "<b>Watcher status</b>\n\n"
        f"Accounts: <b>{stats['accounts_total']}</b> "
        f"(active: <b>{stats['accounts_active']}</b>)\n"
        f"Snapshots stored: <b>{fmt_number(stats['snapshots_total'])}</b>\n"
        f"Notifications sent: <b>{fmt_number(stats['notifications_total'])}</b>\n\n"
        f"Scheduler: <b>{scheduler_state}</b>\n"
        f"Interval: <b>{settings.check_interval}s</b> "
        f"(±{settings.jitter_seconds}s jitter)\n"
        f"Next sweep: <b>{next_run_str}</b>"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    username = _username_arg(context)
    if not username:
        await update.message.reply_text("Usage: /history <username>")
        return

    async with get_session() as session:
        account = await crud.get_account(session, username)
        if not account:
            await update.message.reply_text(
                f"<b>@{esc(username)}</b> is not monitored.",
                parse_mode=ParseMode.HTML,
            )
            return
        notes = await crud.recent_notifications(session, account.id, limit=15)

    if not notes:
        await update.message.reply_text(
            f"No recorded changes for <b>@{esc(username)}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [f"<b>Recent changes for @{esc(username)}</b>", ""]
    for n in notes:
        ts = fmt_timestamp(n.created_at)
        payload = n.payload or {}
        if n.change_type == "fetch_failure":
            detail = f"HTTP {payload.get('status')} — {esc(str(payload.get('error')))}"
        elif n.change_type == "profile_picture":
            detail = (
                f"pic hash {esc(str(payload.get('old'))[:8])}… → "
                f"{esc(str(payload.get('new'))[:8])}…"
            )
        else:
            old = payload.get("old")
            new = payload.get("new")
            detail = f"{esc(str(old))} → {esc(str(new))}"
            detail = truncate(detail, 200)
        lines.append(f"<code>{ts}</code>\n<b>{esc(n.change_type)}</b>: {detail}\n")

    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    username = _username_arg(context)
    if not username:
        await update.message.reply_text("Usage: /photo <username>")
        return

    async with get_session() as session:
        account = await crud.get_account(session, username)
        if not account:
            await update.message.reply_text(
                f"<b>@{esc(username)}</b> is not monitored.",
                parse_mode=ParseMode.HTML,
            )
            return
        media = await crud.latest_media_hash(session, account.id)

    if not media or not media.local_path:
        await update.message.reply_text(
            f"No stored profile picture for <b>@{esc(username)}</b> yet.",
            parse_mode=ParseMode.HTML,
        )
        return

    path = Path(media.local_path)
    if not path.exists():
        await update.message.reply_text(
            "Stored profile picture file is missing on disk."
        )
        return

    caption = (
        f"<b>@{esc(username)}</b>\n"
        f"SHA256: <code>{esc(media.sha256)}</code>\n"
        f"Captured: <code>{fmt_timestamp(media.created_at)}</code>"
    )
    with open(path, "rb") as f:
        await update.message.reply_photo(photo=f, caption=caption, parse_mode=ParseMode.HTML)


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return

    async with get_session() as session:
        records = await crud.export_all(session)
        accounts = {a.id: a.username for a in await crud.list_accounts(session, only_active=False)}

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp_utc", "username", "change_type", "old", "new", "delivered"])
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

    data = buf.getvalue().encode("utf-8")
    with tempfile.NamedTemporaryFile(
        prefix="watcher-export-", suffix=".csv", delete=False
    ) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)

    try:
        with open(tmp_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=f"watcher-export-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.csv",
                caption=f"Exported {count} notification rows",
            )
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _stringify(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return str(value)
    return truncate(str(value), 500)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Telegram handler error: {}", context.error)


# ---------- Registration ----------

def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
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
    # Reject unknown commands quietly to avoid spamming groups
    app.add_handler(
        MessageHandler(filters.COMMAND, _unknown_command)
    )
    app.add_error_handler(on_error)


async def _unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    if update.message:
        await update.message.reply_text("Unknown command. Try /help.")
