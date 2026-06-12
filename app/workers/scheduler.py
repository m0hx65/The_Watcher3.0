"""Periodic monitoring scheduler built on APScheduler."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.database import crud
from app.database.session import get_session
from app.monitor.service import MonitorService
from app.utils.formatting import esc, fmt_timestamp
from app.utils.logger import logger

SWEEP_JOB_ID = "watcher-sweep"
CLEANUP_JOB_ID = "watcher-cleanup"
SETTING_INTERVAL = "check_interval_seconds"
SETTING_LAST_SWEEP_AT = "last_sweep_at"
SETTING_STAKEOUTS = "active_stakeouts"

# Sane bounds enforced wherever interval values are accepted.
MIN_INTERVAL = 60
MAX_INTERVAL = 86_400


def _stakeout_job_id(account_id: int) -> str:
    return f"stakeout:{account_id}"


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
        self._sweep_in_flight: bool = False
        # account_id -> {"username", "interval", "end": datetime(UTC)}
        self._stakeouts: dict[int, dict] = {}

    def set_state_callback(self, callback) -> None:
        """Callback invoked with (state_str, next_run_dt) on changes."""
        self._on_state_change = callback

    @property
    def interval_seconds(self) -> int:
        return self._interval_seconds

    @property
    def sweep_in_flight(self) -> bool:
        return self._sweep_in_flight

    async def start(self) -> None:
        self._interval_seconds = await load_persisted_interval()
        jitter = max(0, settings.jitter_seconds)

        async with get_session() as session:
            raw_last = await crud.get_setting(session, SETTING_LAST_SWEEP_AT)

        now = datetime.now(timezone.utc)
        first_run: datetime
        if raw_last:
            try:
                last_sweep_at = datetime.fromisoformat(raw_last)
                expected_next = last_sweep_at + timedelta(seconds=self._interval_seconds)
                if now >= expected_next:
                    # Overdue or on-time — run shortly after boot
                    first_run = now + timedelta(seconds=5)
                    logger.info(
                        "Scheduler: last sweep was {}, next was due {}. Running soon.",
                        last_sweep_at.isoformat(), expected_next.isoformat(),
                    )
                else:
                    # Still within the window — wait until originally-scheduled time
                    first_run = expected_next
                    logger.info(
                        "Scheduler: next sweep not due until {}. Waiting.",
                        expected_next.isoformat(),
                    )
            except ValueError:
                first_run = now + timedelta(seconds=30)
        else:
            # No prior sweep on record — fresh install, small startup delay
            first_run = now + timedelta(seconds=30)
            logger.info("Scheduler: no prior sweep recorded, first run in 30s.")

        trigger = IntervalTrigger(seconds=self._interval_seconds, jitter=jitter)

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
        await self._restore_stakeouts()

    async def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")
        self._emit_state("stopped")

    @property
    def next_run_time(self) -> Optional[datetime]:
        job = self.scheduler.get_job(SWEEP_JOB_ID)
        return job.next_run_time if job else None

    async def trigger_now(self, *, backfill_ids: bool = False) -> None:
        """Run a sweep immediately, on top of the scheduled cadence."""
        logger.info("Manual sweep triggered (backfill_ids={})", backfill_ids)
        await self._sweep_wrapper(backfill_ids=backfill_ids)

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

    # ---------- Stakeout mode (temporary high-frequency single-target watch) ----------

    def active_stakeouts(self) -> list[dict]:
        """Return non-expired stakeouts as a list of
        {account_id, username, interval, end} dicts, soonest-ending first."""
        now = datetime.now(timezone.utc)
        out = [
            {"account_id": aid, **info}
            for aid, info in self._stakeouts.items()
            if info["end"] > now
        ]
        out.sort(key=lambda s: s["end"])
        return out

    def stakeout_for(self, account_id: int) -> Optional[dict]:
        info = self._stakeouts.get(account_id)
        if info and info["end"] > datetime.now(timezone.utc):
            return {"account_id": account_id, **info}
        return None

    async def start_stakeout(
        self,
        account_id: int,
        username: str,
        *,
        interval: Optional[int] = None,
        duration: Optional[int] = None,
    ) -> dict:
        """Begin (or restart) a temporary high-frequency watch on one account.

        Interval is floored at STAKEOUT_MIN_INTERVAL (above the 90s reel cache,
        so each tick gets fresh data without hammering Instagram into 401s) and
        duration is capped at STAKEOUT_MAX_DURATION. Every tick runs the same
        full check_username — profile, posts/reels, stories, highlights — all
        through the Cloudflare edge proxy. Returns the stored stakeout dict.
        """
        interval = int(interval or settings.stakeout_default_interval)
        interval = max(settings.stakeout_min_interval, min(MAX_INTERVAL, interval))
        duration = int(duration or settings.stakeout_default_duration)
        duration = max(interval, min(settings.stakeout_max_duration, duration))

        end = datetime.now(timezone.utc) + timedelta(seconds=duration)
        self._stakeouts[account_id] = {
            "username": username,
            "interval": interval,
            "end": end,
        }
        if self.scheduler.running:
            # first tick one interval from now (an immediate manual check is
            # done by the caller, so we don't double-fire on start).
            self.scheduler.add_job(
                self._stakeout_tick,
                trigger=IntervalTrigger(seconds=interval),
                id=_stakeout_job_id(account_id),
                kwargs={"account_id": account_id},
                next_run_time=datetime.now(timezone.utc) + timedelta(seconds=interval),
                max_instances=1,
                coalesce=True,
                misfire_grace_time=interval,
                replace_existing=True,
            )
        await self._persist_stakeouts()
        logger.info(
            "Stakeout started for @{} (id={}) every {}s until {}",
            username, account_id, interval, end.isoformat(),
        )
        return {"account_id": account_id, **self._stakeouts[account_id]}

    async def stop_stakeout(self, account_id: int, *, notify: bool = False) -> bool:
        """End a stakeout early. Returns True if one was active."""
        info = self._stakeouts.pop(account_id, None)
        try:
            self.scheduler.remove_job(_stakeout_job_id(account_id))
        except Exception:
            pass  # job already gone / never scheduled
        await self._persist_stakeouts()
        if info is not None:
            logger.info("Stakeout stopped for @{} (id={})", info["username"], account_id)
            if notify:
                await self._notify(
                    f"🎯 Stakeout on <b>@{esc(info['username'])}</b> ended."
                )
            return True
        return False

    async def _stakeout_tick(self, account_id: int) -> None:
        info = self._stakeouts.get(account_id)
        if info is None:
            try:
                self.scheduler.remove_job(_stakeout_job_id(account_id))
            except Exception:
                pass
            return
        username = info["username"]
        # Expired? finish up and announce it.
        if datetime.now(timezone.utc) >= info["end"]:
            await self.stop_stakeout(account_id)
            await self._notify(
                f"🎯 Stakeout on <b>@{esc(username)}</b> complete — back to the "
                "regular sweep schedule."
            )
            return
        try:
            await self.service.check_username(username, notify_unchanged=False)
        except Exception as exc:
            logger.exception("Stakeout tick failed for @{}: {}", username, exc)

    async def _persist_stakeouts(self) -> None:
        payload = [
            {
                "account_id": aid,
                "username": info["username"],
                "interval": info["interval"],
                "end": info["end"].isoformat(),
            }
            for aid, info in self._stakeouts.items()
        ]
        async with get_session() as session:
            if payload:
                await crud.set_setting(
                    session, SETTING_STAKEOUTS, json.dumps(payload)
                )
            else:
                await crud.delete_setting(session, SETTING_STAKEOUTS)

    async def _restore_stakeouts(self) -> None:
        """Re-arm stakeouts that survived a restart and haven't expired yet."""
        async with get_session() as session:
            raw = await crud.get_setting(session, SETTING_STAKEOUTS)
        if not raw:
            return
        try:
            entries = json.loads(raw)
        except (ValueError, TypeError):
            return
        now = datetime.now(timezone.utc)
        restored = 0
        for e in entries:
            try:
                account_id = int(e["account_id"])
                end = datetime.fromisoformat(e["end"])
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
            except (KeyError, ValueError, TypeError):
                continue
            if end <= now:
                continue
            interval = int(e.get("interval") or settings.stakeout_default_interval)
            self._stakeouts[account_id] = {
                "username": e.get("username", ""),
                "interval": interval,
                "end": end,
            }
            self.scheduler.add_job(
                self._stakeout_tick,
                trigger=IntervalTrigger(seconds=interval),
                id=_stakeout_job_id(account_id),
                kwargs={"account_id": account_id},
                next_run_time=now + timedelta(seconds=interval),
                max_instances=1,
                coalesce=True,
                misfire_grace_time=interval,
                replace_existing=True,
            )
            restored += 1
        await self._persist_stakeouts()  # drop expired entries
        if restored:
            logger.info("Restored {} active stakeout(s) after restart", restored)

    async def _notify(self, text: str) -> None:
        try:
            await self.service.notifier.send_text(text)
        except Exception as exc:  # pragma: no cover - notifier failure path
            logger.debug("Stakeout notify failed: {}", exc)

    async def _sweep_wrapper(self, *, backfill_ids: bool = False) -> None:
        if self._sweep_in_flight:
            logger.info("Sweep skipped — another sweep is already in progress")
            return
        self._sweep_in_flight = True
        # Write timestamp immediately so rapid restarts don't pile up duplicate sweeps.
        async with get_session() as session:
            await crud.set_setting(
                session, SETTING_LAST_SWEEP_AT,
                datetime.now(timezone.utc).isoformat(),
            )
        try:
            # Hard cap: if check_all() never returns (hung HTTP connection, etc.)
            # _sweep_in_flight would stay True and block every subsequent scheduled
            # run indefinitely. 10 minutes is generous for any realistic account count.
            await asyncio.wait_for(
                self.service.check_all(backfill_ids=backfill_ids),
                timeout=600,
            )
        except asyncio.TimeoutError:
            logger.error("Sweep timed out after 10 minutes — forcing flag reset")
        except Exception as exc:
            logger.exception("Sweep crashed: {}", exc)
        finally:
            self._sweep_in_flight = False
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
