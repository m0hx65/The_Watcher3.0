"""Keeps the main-menu panel anchored at the bottom of the chat.

Automated sweep notifications push the menu upward as they land, so after they
settle we delete the old panel and re-post a fresh one at the bottom. This runs
as the dispatcher's `post_send_hook` (fired after every delivered message).

The one thing it must NOT do is re-anchor BETWEEN the items of an on-demand
download. Those downloads (🔎 Any user, 📦 Download all, /story, /highlights, …)
deliver their media through the very same notifier, so bumping per item would
wedge a menu between every photo — the duplicates users complained about. The
re-anchor is therefore deferred, not dropped: it waits for `download_active()`
to clear and then runs once, so the panel still ends up last.

What it re-posts is whatever the panel is CURRENTLY showing, not a hardcoded
menu. The panel is also where Status, "Sweep running" and Dark radar render, so
posting the menu back threw those views away: pressing 🔄 Sweep All opened a
view that the sweep's own first notification replaced with a bare menu a second
later. The panel keeps its content and still ends up last.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError

from app.bot import keyboards
from app.bot.handlers import (
    PANEL_CHAT_ID,
    PANEL_MSG_ID,
    WELCOME_TEXT,
    current_render,
    forget_render,
    remember_render,
)
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
        max_deferral: float = 600.0,
    ) -> None:
        self._bot = bot
        self._bot_data = bot_data
        self._download_active = download_active
        # async (panel_msg_id, chat_id) -> None; persists the new panel position
        # so it survives restarts. Optional so tests can run without a DB.
        self._persist = persist
        self._debounce = debounce
        # Ceiling on waiting out a download. A wedged download must not leave a
        # task waiting forever; the next delivered message queues a new one.
        self._max_deferral = max_deferral
        self._pending: Optional[asyncio.Task] = None

    async def schedule(self) -> None:
        """post_send_hook entrypoint: queue one debounced bump.

        No-ops only when a bump is already queued — the debounce lets a burst of
        sweep notifications collapse into a single re-anchor. A download in
        flight does NOT cancel it: _bump waits the download out so the panel
        lands under the finished batch instead of between its items.
        """
        if self._pending is not None and not self._pending.done():
            return
        self._pending = asyncio.create_task(self._bump())

    async def _bump(self) -> None:
        # Let concurrent sweep notifications all land first.
        await asyncio.sleep(self._debounce)
        # Wait out an on-demand download (it may also have STARTED during the
        # debounce) so the panel lands under the whole batch rather than
        # between its media items.
        waited = 0.0
        while self._download_active() and waited < self._max_deferral:
            await asyncio.sleep(self._debounce)
            waited += self._debounce
        if self._download_active():
            logger.debug(
                "Panel bump abandoned — a download has been running for {:.0f}s",
                waited,
            )
            return
        msg_id = self._bot_data.get(PANEL_MSG_ID)
        chat_id = self._bot_data.get(PANEL_CHAT_ID)
        if msg_id is None or chat_id is None:
            return

        # Re-post what the panel shows right now — Status, "Sweep running",
        # whatever the last button opened. Only when that is unknown (a restart
        # dropped the record) does it fall back to the menu.
        text, markup = current_render(msg_id) or (WELCOME_TEXT, keyboards.main_menu())

        try:
            await self._bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except (BadRequest, Forbidden, TelegramError):
            pass
        self._bot_data.pop(PANEL_MSG_ID, None)
        forget_render(msg_id)
        try:
            new_msg = await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
            self._bot_data[PANEL_MSG_ID] = new_msg.message_id
            # The view moved to a new message id; carry the record across so the
            # NEXT bump preserves it too.
            remember_render(new_msg.message_id, text, markup)
            if self._persist is not None:
                await self._persist(new_msg.message_id, chat_id)
        except Exception as exc:  # pragma: no cover - network failure path
            logger.warning("Panel bump failed: {}", exc)
