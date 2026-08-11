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

The second thing it must not do is delete a view the user is still reading.
Re-anchoring means deleting the panel, and the panel is also where Status,
"Sweep running", and Dark radar are rendered — so pressing 🔄 Sweep All used to
open a view that the sweep's own first notification wiped a second later, back
to a bare menu. A panel navigated off the menu is therefore left alone for
PANEL_VIEW_GRACE_SECONDS; pressing 🏠 Home ends that immediately.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError

from app.bot import keyboards
from app.bot.handlers import (
    PANEL_CHAT_ID,
    PANEL_MSG_ID,
    PANEL_VIEW_AT,
    PANEL_VIEW_GRACE_SECONDS,
    WELCOME_TEXT,
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

    def _view_is_being_read(self) -> bool:
        """True while the panel shows a view the user just opened.

        Re-anchoring would delete it mid-read. The mark is set when a panel
        button navigates away from the menu and cleared by 🏠 Home, so this is
        only ever true for a short window after an actual button press.
        """
        opened_at = self._bot_data.get(PANEL_VIEW_AT)
        if opened_at is None:
            return False
        if time.monotonic() - opened_at < PANEL_VIEW_GRACE_SECONDS:
            return True
        # Grace spent — the panel is fair game again, and stays that way until
        # the next press (which re-stamps it).
        self._bot_data.pop(PANEL_VIEW_AT, None)
        return False

    async def _bump(self) -> None:
        # Let concurrent sweep notifications all land first.
        await asyncio.sleep(self._debounce)
        # A manual download may have started during the debounce window; bumping
        # now would still bury the menu under its media, so re-check and bail.
        if self._download_active():
            return
        if self._view_is_being_read():
            logger.debug("Panel bump skipped — a panel view was just opened")
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
            self._bot_data.pop(PANEL_VIEW_AT, None)  # a fresh menu, not a view
            if self._persist is not None:
                await self._persist(new_msg.message_id, chat_id)
        except Exception as exc:  # pragma: no cover - network failure path
            logger.warning("Panel bump failed: {}", exc)
