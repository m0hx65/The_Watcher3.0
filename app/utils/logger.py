"""Centralized logging using loguru."""

from __future__ import annotations

import logging
import sys

from loguru import logger

from app.config import settings

# Paths whose PASSING access-log line says nothing. The host's health checker
# hits /health every few seconds, which is roughly twelve lines a minute of
# "still alive" — enough to bury a sweep's own log between two checks.
_QUIET_ACCESS_PATHS = frozenset({"/health", "/ready"})


class HealthCheckAccessFilter(logging.Filter):
    """Drop uvicorn's access line for a health check that PASSED.

    A FAILING one is kept: that is the line that matters, and hiding it would
    trade noise for the one symptom worth seeing. Anything this filter cannot
    confidently parse is kept too — a log filter must never be the reason a
    record went missing.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access formats from a 5-tuple:
        # (client_addr, method, full_path, http_version, status_code).
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        path = str(args[2]).split("?", 1)[0]
        if path not in _QUIET_ACCESS_PATHS:
            return True
        try:
            status = int(args[4])
        except (TypeError, ValueError):
            return True
        return status >= 400


def quiet_health_check_logs() -> None:
    """Install the filter on uvicorn's access logger, once.

    Called at startup rather than at import: uvicorn configures its own
    loggers with `dictConfig` when the server boots, and while that does not
    clear filters, attaching after it is the ordering that needs no such
    assumption. `LOG_HEALTH_CHECKS=true` keeps every line, for when the
    question is whether the health checker is running at all.
    """
    if settings.log_health_checks:
        return
    access = logging.getLogger("uvicorn.access")
    if any(isinstance(f, HealthCheckAccessFilter) for f in access.filters):
        return
    access.addFilter(HealthCheckAccessFilter())
    logger.info(
        "Health-check access logs are hidden while they pass "
        "(LOG_HEALTH_CHECKS=true shows them); a failing one still logs"
    )


def configure_logging() -> None:
    logger.remove()
    logger.add(
        sys.stdout,
        level=settings.log_level,
        backtrace=False,
        diagnose=False,
        enqueue=True,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
    )


configure_logging()

__all__ = [
    "logger",
    "configure_logging",
    "quiet_health_check_logs",
    "HealthCheckAccessFilter",
]
