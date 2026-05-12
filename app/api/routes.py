"""HTTP API endpoints for health, status, and manual operations."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.config import settings
from app.database import crud
from app.database.session import get_session
from app.monitor.service import MonitorService
from app.utils.logger import logger
from app.workers.scheduler import WatcherScheduler

router = APIRouter()


def _check_token(token: Optional[str]) -> None:
    """If WEB_API_TOKEN is set, require it on mutating endpoints."""
    expected = settings.web_api_token
    if not expected:
        return
    if not token or token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing token"
        )


def get_service(request: Request) -> MonitorService:
    svc = getattr(request.app.state, "monitor", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="Monitor service not initialized")
    return svc


def get_scheduler(request: Request) -> WatcherScheduler:
    sched = getattr(request.app.state, "scheduler", None)
    if sched is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialized")
    return sched


@router.get("/health")
async def health() -> dict:
    return {"ok": True}


@router.get("/ready")
async def ready(request: Request) -> dict:
    monitor = getattr(request.app.state, "monitor", None)
    scheduler = getattr(request.app.state, "scheduler", None)
    return {
        "ok": bool(monitor and scheduler and scheduler.scheduler.running),
        "monitor": bool(monitor),
        "scheduler_running": bool(scheduler and scheduler.scheduler.running),
    }


@router.get("/status")
async def status_endpoint(request: Request) -> dict:
    async with get_session() as session:
        stats = await crud.stats_summary(session)

    scheduler: WatcherScheduler = request.app.state.scheduler
    return {
        **stats,
        "scheduler_running": scheduler.scheduler.running,
        "next_run": (
            scheduler.next_run_time.isoformat() if scheduler.next_run_time else None
        ),
        "check_interval": settings.check_interval,
        "jitter_seconds": settings.jitter_seconds,
    }


@router.get("/accounts")
async def list_accounts() -> dict:
    async with get_session() as session:
        accounts = await crud.list_accounts(session, only_active=False)

    return {
        "accounts": [
            {
                "username": a.username,
                "instagram_id": a.instagram_id,
                "active": a.active,
                "last_checked_at": (
                    a.last_checked_at.isoformat() if a.last_checked_at else None
                ),
                "last_status_code": a.last_status_code,
                "consecutive_failures": a.consecutive_failures,
            }
            for a in accounts
        ]
    }


@router.post("/accounts/{username}/recheck")
async def force_recheck(
    username: str,
    request: Request,
    x_api_token: Optional[str] = Header(default=None),
    service: MonitorService = Depends(get_service),
) -> dict:
    _check_token(x_api_token)
    result = await service.check_username(username)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/sweep")
async def trigger_sweep(
    x_api_token: Optional[str] = Header(default=None),
    scheduler: WatcherScheduler = Depends(get_scheduler),
) -> dict:
    """Cron-style endpoint. Render Cron Jobs can call this to trigger a sweep."""
    _check_token(x_api_token)
    logger.info("Sweep triggered via HTTP")
    await scheduler.trigger_now()
    return {"ok": True}
