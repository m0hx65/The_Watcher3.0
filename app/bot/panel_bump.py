"""Keeps the main-menu panel anchored at the bottom of the chat.

Automated sweep notifications push the menu upward as they land, so after they
settle we delete the old panel and re-post a fresh one at the bottom. This runs
as the dispatcher's `post_send_hook` (fired after every delivered message).

The one thing it must NOT do is re-anchor during an on-demand download. Those
downloads (🔎 Any user, 📦 Download all, /story, /highlights, …) deliver their
media through the very same notifier, so bumping afterwards drops a redundant
second menu underneath the result — the duplicate users complained about. While
`download_active()` is true the bump is skipped entirely; the result message
carries its own keyboard (with a 🏠 Home button), so the menu stays reachable.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError

from app.bot import keyboards
from app.bot.handlers import PANEL_CHAT_ID, PANEL_MSG_ID, WELCOME_TEXT
from app.utils.logger import logger


class PanelBumper:
    """Debounced re-anchor of the main-menu panel, gated on download activity."""

    def __init__(
        self,
        bot,
        bot_data: dict,
        *,
        download_active: Callable[[], bool],
        persist: Optional[Callable[[int, int], Awaitable[None]]] = None,
        debounce: float = 2.0,
    ) -> None:
        self._bot = bot
        self._bot_data = bot_data
        self._download_active = download_active
        # async (panel_msg_id, chat_id) -> None; persists the new panel position
        # so it survives restarts. Optional so tests can run without a DB.
        self._persist = persist
        self._debounce = debounce
        self._pending: Optional[asyncio.Task] = None

    async def schedule(self) -> None:
        """post_send_hook entrypoint: queue one debounced bump.

        No-ops when an on-demand download is running (its media just went out
        through the notifier) or when a bump is already queued — the debounce
        lets a burst of sweep notifications collapse into a single re-anchor.
        """
        if self._download_active():
            return
        if self._pending is not None and not self._pending.done():
            return
        self._pending = asyncio.create_task(self._bump())

    async def _bump(self) -> None:
        # Let concurrent sweep notifications all land first.
        await asyncio.sleep(self._debounce)
        # A manual download may have started during the debounce window; bumping
        # now would still bury the menu under its media, so re-check and bail.
        if self._download_active():
            return
        msg_id = self._bot_data.get(PANEL_MSG_ID)
        chat_id = self._bot_data.get(PANEL_CHAT_ID)
        if msg_id is None or chat_id is None:
            return
        try:
            await self._bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except (BadRequest, Forbidden, TelegramError):
            pass
        self._bot_data.pop(PANEL_MSG_ID, None)
        try:
            new_msg = await self._bot.send_message(
                chat_id=chat_id,
                text=WELCOME_TEXT,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboards.main_menu(),
                disable_web_page_preview=True,
            )
            self._bot_data[PANEL_MSG_ID] = new_msg.message_id
            if self._persist is not None:
                await self._persist(new_msg.message_id, chat_id)
        except Exception as exc:  # pragma: no cover - network failure path
            logger.warning("Panel bump failed: {}", exc)
