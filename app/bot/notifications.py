"""Telegram notification dispatcher and message renderers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError, TimedOut

from app.config import settings
from app.monitor.change_detector import ChangeSet
from app.monitor.instagram import ProfileFetchResult
from app.utils.formatting import esc, fmt_delta, fmt_number, truncate
from app.utils.logger import logger


# Media uploads need far longer than the python-telegram-bot 5 s default: the
# file is streamed to Telegram and then Telegram processes it before replying.
# From a cloud host (Render) this routinely takes >5 s, the request times out,
# and — fatally — the old retry loop resent the same file, posting it 2–4×.
# Generous write/read windows make a genuine timeout rare, and on the rare one
# we treat an upload as delivered rather than retrying (see _send_with_retry).
_UPLOAD_TIMEOUTS = {
    "connect_timeout": 30.0,
    "write_timeout": 180.0,  # the upload itself (big story videos)
    "read_timeout": 120.0,   # waiting for Telegram to accept + reply
    "pool_timeout": 60.0,
}


class NotificationDispatcher:
    """Thin wrapper around python-telegram-bot for sending alerts."""

    def __init__(
        self,
        bot: Bot,
        chat_id: Union[int, str],
        mirror_chat_ids: Optional[list[Union[int, str]]] = None,
    ) -> None:
        self.bot = bot
        # Primary chat — the one that owns per-account forum topics.
        self.chat_id = chat_id
        # Extra chats that receive a flat copy of every message (no topics).
        self.mirror_chat_ids: list[Union[int, str]] = list(mirror_chat_ids or [])
        self._send_lock = asyncio.Lock()
        self.post_send_hook: Optional[Callable[[], Awaitable[None]]] = None

    def _targets(
        self, message_thread_id: Optional[int]
    ) -> list[tuple[Union[int, str], Optional[int]]]:
        """(chat_id, thread) for each destination: the primary keeps its topic
        thread; mirrors always post flat (None) since DMs/non-forum chats have
        no topics."""
        targets: list[tuple[Union[int, str], Optional[int]]] = [
            (self.chat_id, message_thread_id)
        ]
        for mid in self.mirror_chat_ids:
            targets.append((mid, None))
        return targets

    async def create_forum_topic(self, name: str) -> Optional[int]:
        """Create a forum topic in the configured chat; return its thread id.

        Returns None when the chat isn't a forum (or the bot lacks the
        manage-topics right) — callers then fall back to the General thread."""
        try:
            topic = await self.bot.create_forum_topic(
                chat_id=self.chat_id, name=name[:128]
            )
            return topic.message_thread_id
        except TelegramError as exc:
            logger.debug("create_forum_topic({}) failed: {}", name, exc)
            return None

    async def send_text(
        self,
        text: str,
        *,
        parse_mode: str = ParseMode.HTML,
        message_thread_id: Optional[int] = None,
    ) -> bool:
        # Telegram caps text at 4096 chars
        chunks = _split_text(text, limit=4000)
        delivered_primary = False
        for chunk in chunks:
            for cid, thread in self._targets(message_thread_id):
                ok = await self._send_with_retry(
                    lambda chat, th, c=chunk: self.bot.send_message(
                        chat_id=chat,
                        text=c,
                        parse_mode=parse_mode,
                        disable_web_page_preview=True,
                        message_thread_id=th,
                    ),
                    chat_id=cid,
                    message_thread_id=thread,
                )
                if cid == self.chat_id:
                    delivered_primary |= ok
        if delivered_primary and self.post_send_hook is not None:
            await self.post_send_hook()
        return delivered_primary

    async def send_photo(
        self,
        path: Path,
        caption: Optional[str] = None,
        *,
        message_thread_id: Optional[int] = None,
    ) -> bool:
        def _send(chat, thread):
            with open(path, "rb") as f:
                return self.bot.send_photo(
                    chat_id=chat,
                    photo=f,
                    caption=caption or "",
                    parse_mode=ParseMode.HTML,
                    message_thread_id=thread,
                    **_UPLOAD_TIMEOUTS,
                )

        ok = await self._send_to_targets(_send, message_thread_id, is_upload=True)
        if ok and self.post_send_hook is not None:
            await self.post_send_hook()
        return ok

    async def send_document(
        self,
        path: Path,
        caption: Optional[str] = None,
        *,
        message_thread_id: Optional[int] = None,
    ) -> bool:
        def _send(chat, thread):
            with open(path, "rb") as f:
                return self.bot.send_document(
                    chat_id=chat,
                    document=f,
                    caption=caption or "",
                    parse_mode=ParseMode.HTML,
                    message_thread_id=thread,
                    **_UPLOAD_TIMEOUTS,
                )

        return await self._send_to_targets(_send, message_thread_id, is_upload=True)

    async def send_video(
        self,
        path: Path,
        caption: Optional[str] = None,
        *,
        message_thread_id: Optional[int] = None,
    ) -> bool:
        def _send(chat, thread):
            with open(path, "rb") as f:
                return self.bot.send_video(
                    chat_id=chat,
                    video=f,
                    caption=caption or "",
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                    message_thread_id=thread,
                    **_UPLOAD_TIMEOUTS,
                )

        ok = await self._send_to_targets(_send, message_thread_id, is_upload=True)
        if ok and self.post_send_hook is not None:
            await self.post_send_hook()
        return ok

    async def _send_to_targets(
        self, action, message_thread_id: Optional[int], *, is_upload: bool = False
    ) -> bool:
        """Send one media item to the primary + every mirror. Returns the
        primary's delivery result (mirrors are best-effort)."""
        delivered_primary = False
        for cid, thread in self._targets(message_thread_id):
            ok = await self._send_with_retry(
                action, is_upload=is_upload, chat_id=cid, message_thread_id=thread
            )
            if cid == self.chat_id:
                delivered_primary = ok
        return delivered_primary

    async def _send_with_retry(
        self,
        action,
        *,
        is_upload: bool = False,
        chat_id: Union[int, str],
        message_thread_id: Optional[int] = None,
    ) -> bool:
        # `action(chat_id, thread)` — thread can be cleared mid-flight if
        # Telegram reports the topic gone (handled in the BadRequest branch).
        thread = message_thread_id

        for attempt in range(1, 5):
            delay: float = 0.0
            async with self._send_lock:
                try:
                    result = action(chat_id, thread)
                    if asyncio.iscoroutine(result):
                        await result
                    return True
                except BadRequest as exc:
                    # A deleted/invalid topic must not eat the message — drop the
                    # thread and resend to General instead of failing.
                    if thread is not None and "thread" in str(exc).lower():
                        logger.warning(
                            "Topic {} invalid ({}); resending to General",
                            thread, exc,
                        )
                        thread = None
                        continue
                    delay = min(10.0, 2 ** attempt)
                    logger.warning("Telegram error attempt {}/4: {}", attempt, exc)
                except RetryAfter as ra:
                    # Cap at 30s so the lock isn't held (or slept) for minutes
                    delay = min(float(ra.retry_after) + 1.0, 30.0)
                    logger.warning(
                        "Telegram rate limit, sleeping {:.1f}s (attempt {})", delay, attempt
                    )
                except TimedOut as exc:
                    # A media upload that times out has almost always already
                    # reached Telegram — it's the *response* that was slow.
                    # Retrying would post the same photo/video again (the 2–4×
                    # duplicates users saw), so treat it as delivered instead.
                    if is_upload:
                        logger.warning(
                            "Upload timed out (attempt {}/4) — assuming delivered, "
                            "not retrying to avoid a duplicate: {}",
                            attempt, exc,
                        )
                        return True
                    delay = min(10.0, 2 ** attempt)
                    logger.warning(
                        "Telegram timeout attempt {}/4: {}", attempt, exc
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


def render_new_stories_alert(username: str, count: int) -> str:
    noun = "item" if count == 1 else "items"
    return f"📖 <b>@{esc(username)}</b> posted {count} new story {noun}"


def render_highlight_catalog_changes(
    username: str,
    *,
    added: list[tuple[str, str]],
    removed: list[tuple[str, str]],
    renamed: list[tuple[str, str, str]],
    total: int,
) -> str:
    lines = [f"✨ <b>@{esc(username)}</b> highlights updated", ""]
    if added:
        lines.append(f"<b>Added ({len(added)}):</b>")
        for _hid, title in added:
            lines.append(f"  • {esc(title) or '(untitled)'}")
        lines.append("")
    if removed:
        lines.append(f"<b>Removed ({len(removed)}):</b>")
        for _hid, title in removed:
            lines.append(f"  • {esc(title) or '(untitled)'}")
        lines.append("")
    if renamed:
        lines.append(f"<b>Renamed ({len(renamed)}):</b>")
        for _hid, old_title, new_title in renamed:
            lines.append(
                f"  • {esc(old_title) or '(untitled)'} → {esc(new_title) or '(untitled)'}"
            )
        lines.append("")
    lines.append(f"Total highlights: <b>{total}</b>")
    return "\n".join(lines).rstrip()


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
    mirrors = [m for m in settings.mirror_chat_ids if m != chat_id]
    if mirrors:
        logger.info("Mirroring notifications to {} extra chat(s)", len(mirrors))
    return NotificationDispatcher(bot, chat_id, mirror_chat_ids=mirrors)
