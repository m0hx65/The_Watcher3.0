"""FastAPI application entrypoint.

Wires together the database, Instagram client, media hasher, Telegram bot,
notification dispatcher, change detection engine, and APScheduler-driven worker.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from telegram.ext import Application as TgApplication

from app.api.routes import router as api_router
from app.bot.handlers import BOT_COMMANDS, PANEL_CHAT_ID, PANEL_MSG_ID, register_handlers
from app.bot.notifications import build_dispatcher
from app.bot.panel_bump import PanelBumper
from app.config import settings
from app.database import crud
from app.database.session import dispose_engine, get_session, init_db
from app.monitor.instagram import InstagramClient
from app.monitor.media_hasher import MediaHasher
from app.monitor.service import MonitorService
from app.monitor.stories import StoriesClient
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
        .concurrent_updates(True)
        .build()
    )
    register_handlers(tg_app)

    # Build core services
    instagram = InstagramClient()
    hasher = MediaHasher()
    stories = StoriesClient()
    dispatcher = build_dispatcher(tg_app.bot)
    monitor = MonitorService(instagram, hasher, dispatcher, stories)
    scheduler = WatcherScheduler(monitor)

    # Cross-wire so /status can read scheduler info & handlers can run checks
    tg_app.bot_data["monitor"] = monitor
    tg_app.bot_data["scheduler"] = scheduler

    def _state_change(state: str, next_run) -> None:
        tg_app.bot_data["scheduler_state"] = state
        tg_app.bot_data["next_run"] = next_run

    scheduler.set_state_callback(_state_change)

    # --- Panel-bump: keep the main-menu message at the bottom of the chat ---
    # Load persisted panel position from DB so it survives server restarts.
    async with get_session() as _s:
        _saved_mid = await crud.get_setting(_s, "panel_msg_id")
        _saved_cid = await crud.get_setting(_s, "panel_chat_id")
    if _saved_mid and _saved_cid:
        tg_app.bot_data[PANEL_MSG_ID] = int(_saved_mid)
        tg_app.bot_data[PANEL_CHAT_ID] = int(_saved_cid)

    async def _persist_panel(msg_id: int, chat_id: int) -> None:
        async with get_session() as session:
            await crud.set_setting(session, "panel_msg_id", str(msg_id))
            await crud.set_setting(session, "panel_chat_id", str(chat_id))

    # The bump keeps the menu at the bottom after automated sweep notifications,
    # but skips while a manual download is running so it never drops a duplicate
    # menu under an on-demand result (see PanelBumper).
    panel_bumper = PanelBumper(
        tg_app.bot,
        tg_app.bot_data,
        download_active=lambda: monitor.download_active,
        persist=_persist_panel,
    )
    dispatcher.post_send_hook = panel_bumper.schedule

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
            await stories.close()
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
