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
from app.bot.handlers import register_handlers
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

    # Save on app state for HTTP API access
    app.state.monitor = monitor
    app.state.scheduler = scheduler
    app.state.tg_app = tg_app
    app.state.instagram = instagram
    app.state.hasher = hasher

    # Start Telegram polling in the background
    await tg_app.initialize()
    await tg_app.start()
    if tg_app.updater:
        await tg_app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "edited_message"],
        )

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
            if tg_app.updater and tg_app.updater.running:
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
