"""Telegram notification dispatcher and message renderers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError

from app.config import settings
from app.monitor.change_detector import ChangeSet
from app.monitor.instagram import ProfileFetchResult
from app.utils.formatting import esc, fmt_delta, fmt_number, truncate
from app.utils.logger import logger


class NotificationDispatcher:
    """Thin wrapper around python-telegram-bot for sending alerts."""

    def __init__(self, bot: Bot, chat_id: Union[int, str]) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self._send_lock = asyncio.Lock()
        self.post_send_hook: Optional[Callable[[], Awaitable[None]]] = None

    async def send_text(self, text: str, *, parse_mode: str = ParseMode.HTML) -> bool:
        # Telegram caps text at 4096 chars
        chunks = _split_text(text, limit=4000)
        delivered_any = False
        for chunk in chunks:
            delivered_any |= await self._send_with_retry(
                lambda c=chunk: self.bot.send_message(
                    chat_id=self.chat_id,
                    text=c,
                    parse_mode=parse_mode,
                    disable_web_page_preview=True,
                )
            )
        if delivered_any and self.post_send_hook is not None:
            await self.post_send_hook()
        return delivered_any

    async def send_photo(
        self, path: Path, caption: Optional[str] = None
    ) -> bool:
        async def _send():
            with open(path, "rb") as f:
                await self.bot.send_photo(
                    chat_id=self.chat_id,
                    photo=f,
                    caption=caption or "",
                    parse_mode=ParseMode.HTML,
                )

        ok = await self._send_with_retry(_send)
        if ok and self.post_send_hook is not None:
            await self.post_send_hook()
        return ok

    async def send_document(self, path: Path, caption: Optional[str] = None) -> bool:
        async def _send():
            with open(path, "rb") as f:
                await self.bot.send_document(
                    chat_id=self.chat_id,
                    document=f,
                    caption=caption or "",
                    parse_mode=ParseMode.HTML,
                )

        return await self._send_with_retry(_send)

    async def send_video(self, path: Path, caption: Optional[str] = None) -> bool:
        async def _send():
            with open(path, "rb") as f:
                await self.bot.send_video(
                    chat_id=self.chat_id,
                    video=f,
                    caption=caption or "",
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                )

        ok = await self._send_with_retry(_send)
        if ok and self.post_send_hook is not None:
            await self.post_send_hook()
        return ok

    async def _send_with_retry(self, action) -> bool:
        for attempt in range(1, 5):
            delay: float = 0.0
            async with self._send_lock:
                try:
                    result = action()
                    if asyncio.iscoroutine(result):
                        await result
                    return True
                except RetryAfter as ra:
                    # Cap at 30s so the lock isn't held (or slept) for minutes
                    delay = min(float(ra.retry_after) + 1.0, 30.0)
                    logger.warning(
                        "Telegram rate limit, sleeping {:.1f}s (attempt {})", delay, attempt
                    )
                except TelegramError as exc:
                    delay = min(10.0, 2 ** attempt)
                    logger.warning(
                        "Telegram error attempt {}/4: {}", attempt, exc
                    )
                except Exception as exc:
                    logger.exception("Unexpected Telegram send error: {}", exc)
                    return False
            # Sleep *outside* the lock so other sends aren't blocked during backoff
            if delay > 0:
                await asyncio.sleep(delay)
        logger.error("Telegram send permanently failed")
        return False


# ---------- Message renderers ----------

def render_changes_message(changeset: ChangeSet, *, first_seen: bool = False) -> str:
    """Build a single message describing all non-photo changes."""
    if not changeset.changes and not changeset.profile_pic_changed:
        return ""

    lines: list[str] = []
    header = f"<b>@{esc(changeset.username)}</b> profile updated"
    if first_seen:
        header = f"<b>@{esc(changeset.username)}</b> baseline recorded"
    lines.append(header)
    lines.append("")

    for change in changeset.changes:
        lines.append(_render_change_block(change))
        lines.append("")

    if changeset.profile_pic_changed and not changeset.changes:
        lines.append("Profile picture changed (photo follows).")

    return "\n".join(lines).rstrip()


def _render_change_block(change) -> str:
    field = change.field
    old = change.old
    new = change.new
    label = change.label

    if field in {"followers_count", "following_count", "posts_count", "reels_count", "story_count"}:
        delta = fmt_delta(old, new)
        verb = "gained" if (new or 0) > (old or 0) else "lost"
        diff = abs((new or 0) - (old or 0))
        # User-friendly label for followers
        if field == "followers_count":
            return (
                f"<b>{verb} {fmt_number(diff)} followers</b>\n"
                f"Old: {fmt_number(old)}\n"
                f"New: {fmt_number(new)}{delta}"
            )
        return (
            f"<b>{label.capitalize()}:</b> {fmt_number(old)} → {fmt_number(new)}{delta}"
        )

    if field == "is_private":
        return (
            "<b>Visibility changed</b>\n"
            f"{'PRIVATE' if old else 'PUBLIC'} → {'PRIVATE' if new else 'PUBLIC'}"
        )

    if field == "is_verified":
        return (
            "<b>Verification changed</b>\n"
            f"{'VERIFIED' if old else 'UNVERIFIED'} → {'VERIFIED' if new else 'UNVERIFIED'}"
        )

    if field == "is_business":
        return (
            "<b>Business account changed</b>\n"
            f"{'YES' if old else 'NO'} → {'YES' if new else 'NO'}"
        )

    if field == "biography":
        return (
            "<b>Bio changed</b>\n"
            f"Old:\n<code>{esc(truncate(old, 500)) or '(empty)'}</code>\n\n"
            f"New:\n<code>{esc(truncate(new, 500)) or '(empty)'}</code>"
        )

    if field == "full_name":
        return (
            f"<b>Full name changed</b>\n"
            f"<code>{esc(old) or '(empty)'}</code> → <code>{esc(new) or '(empty)'}</code>"
        )

    if field == "username":
        return (
            "<b>Username changed</b>\n"
            f"<code>@{esc(old)}</code> → <code>@{esc(new)}</code>"
        )

    if field == "external_url":
        return (
            "<b>External link changed</b>\n"
            f"Old: <code>{esc(old) or '(none)'}</code>\n"
            f"New: <code>{esc(new) or '(none)'}</code>"
        )

    return f"<b>{esc(label)}:</b> <code>{esc(str(old))}</code> → <code>{esc(str(new))}</code>"


def render_failure_message(username: str, fetch: ProfileFetchResult) -> str:
    return (
        f"<b>Instagram monitor failed for @{esc(username)}</b>\n"
        f"HTTP status: <code>{fetch.http_status}</code>\n"
        f"Error: <code>{esc(fetch.error or 'unknown')}</code>"
    )


def _split_text(text: str, *, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        # Try to split on a newline within the limit
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return parts


def build_dispatcher(bot: Bot) -> NotificationDispatcher:
    chat_id: Union[int, str] = settings.telegram_chat_id
    if isinstance(chat_id, str) and chat_id.lstrip("-").isdigit():
        chat_id = int(chat_id)
    return NotificationDispatcher(bot, chat_id)
