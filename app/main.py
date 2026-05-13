"""FastAPI application entrypoint.

Wires together the database, Instagram client, media hasher, Telegram bot,
notification dispatcher, change detection engine, and APScheduler-driven worker.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from telegram import Bot
from telegram.ext import Application as TgApplication

from app.api.routes import router as api_router
from app.bot import keyboards
from app.bot.handlers import BOT_COMMANDS, PANEL_CHAT_ID, PANEL_MSG_ID, register_handlers
from app.bot.handlers import WELCOME_TEXT
from app.bot.notifications import build_dispatcher
from app.config import settings
from app.database.session import dispose_engine, init_db
from app.monitor.instagram import InstagramClient
from app.monitor.media_hasher import MediaHasher
from app.monitor.service import MonitorService
from app.utils.logger import logger
from app.workers.scheduler import WatcherScheduler


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("Starting The Watcher V3.0…")

    await init_db()

    # Telegram bot
    tg_app = (
        TgApplication.builder()
        .token(settings.telegram_bot_token)
        .build()
    )
    register_handlers(tg_app)

    # Build core services
    instagram = InstagramClient()
    hasher = MediaHasher()
    dispatcher = build_dispatcher(tg_app.bot)
    monitor = MonitorService(instagram, hasher, dispatcher)
    scheduler = WatcherScheduler(monitor)

    # Cross-wire so /status can read scheduler info & handlers can run checks
    tg_app.bot_data["monitor"] = monitor
    tg_app.bot_data["scheduler"] = scheduler

    def _state_change(state: str, next_run) -> None:
        tg_app.bot_data["scheduler_state"] = state
        tg_app.bot_data["next_run"] = next_run

    scheduler.set_state_callback(_state_change)

    async def _bump_panel() -> None:
        """Move the main-menu panel to the bottom of the chat after a sweep."""
        from telegram.constants import ParseMode
        from telegram.error import BadRequest, Forbidden, TelegramError

        msg_id = tg_app.bot_data.get(PANEL_MSG_ID)
        chat_id = tg_app.bot_data.get(PANEL_CHAT_ID)
        if msg_id is None or chat_id is None:
            return
        try:
            await tg_app.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except (BadRequest, Forbidden, TelegramError):
            pass
        tg_app.bot_data.pop(PANEL_MSG_ID, None)
        try:
            new_msg = await tg_app.bot.send_message(
                chat_id=chat_id,
                text=WELCOME_TEXT,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboards.main_menu(),
                disable_web_page_preview=True,
            )
            tg_app.bot_data[PANEL_MSG_ID] = new_msg.message_id
        except Exception as exc:
            logger.warning("Panel bump failed: {}", exc)

    scheduler.post_sweep_hook = _bump_panel

    # Save on app state for HTTP API access
    app.state.monitor = monitor
    app.state.scheduler = scheduler
    app.state.tg_app = tg_app
    app.state.instagram = instagram
    app.state.hasher = hasher

    # Start the Telegram Application. In webhook mode we never start the
    # updater — incoming updates are pushed into `tg_app.update_queue` by
    # the FastAPI webhook endpoint. In polling mode we run the updater.
    await tg_app.initialize()
    await tg_app.start()
    try:
        await tg_app.bot.set_my_commands(BOT_COMMANDS)
    except Exception as exc:
        logger.warning("Failed to set bot commands menu: {}", exc)

    allowed_updates = ["message", "edited_message", "callback_query"]

    if settings.telegram_use_webhook:
        webhook_url = settings.telegram_webhook_full_url
        # Drop the webhook first so any prior URL/secret is cleared, then
        # register ours. `drop_pending_updates` discards the backlog that
        # built up while polling was running, preventing a flood at switchover.
        try:
            await tg_app.bot.delete_webhook(drop_pending_updates=True)
        except Exception as exc:
            logger.warning("Failed to clear prior webhook: {}", exc)
        await tg_app.bot.set_webhook(
            url=webhook_url,
            secret_token=settings.telegram_webhook_secret or None,
            allowed_updates=allowed_updates,
            drop_pending_updates=True,
        )
        logger.info("Telegram webhook registered: {}", webhook_url)
    else:
        # Local dev fallback — long-polling. Only one consumer per token works,
        # so do not run this alongside a deployed webhook.
        if tg_app.updater:
            await tg_app.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=allowed_updates,
            )
            logger.info("Telegram long-polling started (no webhook URL configured)")

    await scheduler.start()
    logger.info("The Watcher is online.")

    try:
        yield
    finally:
        logger.info("Shutting down…")
        try:
            await scheduler.shutdown()
        except Exception as exc:
            logger.warning("Scheduler shutdown error: {}", exc)

        try:
            if settings.telegram_use_webhook:
                # Leave the webhook registered on shutdown — Render's rolling
                # deploys overlap, so deleting here would briefly break the
                # incoming side for the new instance. The new instance will
                # re-register on startup with the same URL/secret.
                pass
            elif tg_app.updater and tg_app.updater.running:
                await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        except Exception as exc:
            logger.warning("Telegram shutdown error: {}", exc)

        try:
            await instagram.close()
            await hasher.close()
        except Exception as exc:
            logger.warning("HTTP client shutdown error: {}", exc)

        await dispose_engine()
        logger.info("Shutdown complete.")


app = FastAPI(
    title="The Watcher V3.0",
    description="Instagram account intelligence monitoring platform.",
    version="3.0.0",
    lifespan=lifespan,
)


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse(
        {
            "name": "The Watcher V3.0",
            "status": "running",
            "endpoints": [
                "/health",
                "/ready",
                "/status",
                "/accounts",
                "/accounts/{username}/recheck",
                "/sweep",
            ],
        }
    )


app.include_router(api_router)
