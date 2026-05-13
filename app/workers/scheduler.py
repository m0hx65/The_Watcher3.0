"""Periodic monitoring scheduler built on APScheduler."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.database import crud
from app.database.session import get_session
from app.monitor.service import MonitorService
from app.utils.logger import logger

SWEEP_JOB_ID = "watcher-sweep"
CLEANUP_JOB_ID = "watcher-cleanup"
SETTING_INTERVAL = "check_interval_seconds"

# Sane bounds enforced wherever interval values are accepted.
MIN_INTERVAL = 60
MAX_INTERVAL = 86_400


async def load_persisted_interval() -> int:
    """Return the interval in seconds — DB value if set, otherwise env default."""
    async with get_session() as session:
        raw = await crud.get_setting(session, SETTING_INTERVAL)
    if raw and raw.isdigit():
        value = int(raw)
        if MIN_INTERVAL <= value <= MAX_INTERVAL:
            return value
    return max(MIN_INTERVAL, settings.check_interval)


async def persist_interval(seconds: int) -> None:
    async with get_session() as session:
        await crud.set_setting(session, SETTING_INTERVAL, str(seconds))


class WatcherScheduler:
    """Wraps APScheduler with jittered sweep scheduling."""

    def __init__(self, service: MonitorService) -> None:
        self.service = service
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self._on_state_change = None
        self._interval_seconds: int = max(MIN_INTERVAL, settings.check_interval)

    def set_state_callback(self, callback) -> None:
        """Callback invoked with (state_str, next_run_dt) on changes."""
        self._on_state_change = callback

    @property
    def interval_seconds(self) -> int:
        return self._interval_seconds

    async def start(self) -> None:
        self._interval_seconds = await load_persisted_interval()
        jitter = max(0, settings.jitter_seconds)

        trigger = IntervalTrigger(seconds=self._interval_seconds, jitter=jitter)
        first_run = datetime.now(timezone.utc) + timedelta(
            seconds=random.randint(15, 60)
        )

        self.scheduler.add_job(
            self._sweep_wrapper,
            trigger=trigger,
            id=SWEEP_JOB_ID,
            next_run_time=first_run,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
        self.scheduler.add_job(
            self._cleanup_wrapper,
            trigger=CronTrigger(hour=3, minute=0, timezone="UTC"),
            id=CLEANUP_JOB_ID,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        self.scheduler.start()
        logger.info(
            "Scheduler started — interval={}s jitter=±{}s first run={}",
            self._interval_seconds, jitter, first_run.isoformat(),
        )
        self._emit_state("running")

    async def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")
        self._emit_state("stopped")

    @property
    def next_run_time(self) -> Optional[datetime]:
        job = self.scheduler.get_job(SWEEP_JOB_ID)
        return job.next_run_time if job else None

    async def trigger_now(self) -> None:
        """Run a sweep immediately, on top of the scheduled cadence."""
        logger.info("Manual sweep triggered")
        await self._sweep_wrapper()

    async def set_interval(self, seconds: int) -> int:
        """Persist a new interval and reschedule the live job. Returns clamped value."""
        seconds = max(MIN_INTERVAL, min(MAX_INTERVAL, int(seconds)))
        await persist_interval(seconds)
        self._interval_seconds = seconds

        if self.scheduler.running and self.scheduler.get_job(SWEEP_JOB_ID):
            jitter = max(0, settings.jitter_seconds)
            self.scheduler.reschedule_job(
                SWEEP_JOB_ID,
                trigger=IntervalTrigger(seconds=seconds, jitter=jitter),
            )
            logger.info("Scheduler interval updated to {}s", seconds)
            self._emit_state("running")
        return seconds

    async def _sweep_wrapper(self) -> None:
        try:
            await self.service.check_all()
        except Exception as exc:
            logger.exception("Sweep crashed: {}", exc)
        finally:
            self._emit_state("running")

    async def _cleanup_wrapper(self) -> None:
        snap_days = settings.snapshot_retention_days
        notif_days = settings.notification_retention_days
        raw_days = settings.raw_response_retention_days
        if snap_days == 0 and notif_days == 0 and raw_days == 0:
            return
        try:
            async with get_session() as session:
                totals = await crud.purge_old_data(
                    session,
                    snapshot_days=snap_days,
                    notification_days=notif_days,
                    raw_response_days=raw_days,
                )
            logger.info(
                "Daily cleanup done — snapshots deleted={} raw_responses_nulled={} notifications deleted={}",
                totals["snapshots_deleted"],
                totals["raw_responses_nulled"],
                totals["notifications_deleted"],
            )
        except Exception as exc:
            logger.exception("Cleanup job crashed: {}", exc)

    def _emit_state(self, state: str) -> None:
        if self._on_state_change is None:
            return
        try:
            self._on_state_change(state, self.next_run_time)
        except Exception:
            logger.exception("State callback failed")
