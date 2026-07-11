"""In-memory fetch-health telemetry.

This module ONLY records outcomes — it never changes a request, a header, a
retry, or the pacing of a fetch. It exists so a recurring "9 of 13 accounts
401'd" can be seen as a number per endpoint instead of a vibe, and so the
sweep circuit breaker and /status have a shared source of truth.

Everything is process-local and cheap: cumulative counters per endpoint plus a
short rolling window for a "recent" view. Nothing is persisted (a restart is a
clean slate, which is what you want for a live-health readout).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Optional

# Endpoint labels. Keep these stable — /status and the tests key off them.
IG_PROFILE = "ig_profile"   # web_profile_info (the main per-account fetch)
IG_REEL = "ig_reel"         # graphql reel query (story/live status + highlights)
SAVEINSTA = "saveinsta"     # anonymous story/post/highlight media source

_ENDPOINTS = (IG_PROFILE, IG_REEL, SAVEINSTA)

# Outcome categories.
OK = "ok"
UNAUTHORIZED = "unauthorized"  # 401/403 — the datacenter block we care about
RATE_LIMITED = "rate_limited"  # 429
NOT_FOUND = "not_found"        # 404 (a real answer, not a failure)
ERROR = "error"                # timeout / 5xx / network / status 0
_CATEGORIES = (OK, UNAUTHORIZED, RATE_LIMITED, NOT_FOUND, ERROR)

# How long an event stays in the "recent" rolling window.
_WINDOW_SECONDS = 3600.0
# Hard cap on retained events per endpoint so memory can't grow unbounded on a
# very busy process (the window is time-based; this is just a backstop).
_MAX_EVENTS = 2000


def classify_status(status: Optional[int]) -> str:
    """Map an HTTP status (or 0/None for a network error) to a category."""
    if status == 200:
        return OK
    if status in (401, 403):
        return UNAUTHORIZED
    if status == 429:
        return RATE_LIMITED
    if status == 404:
        return NOT_FOUND
    return ERROR


class FetchHealth:
    """Thread-safe per-endpoint outcome counters + a rolling recent window."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # endpoint -> {category: cumulative count}
        self._totals: dict[str, dict[str, int]] = {
            ep: {cat: 0 for cat in _CATEGORIES} for ep in _ENDPOINTS
        }
        # endpoint -> deque[(monotonic_ts, category)]
        self._recent: dict[str, Deque[tuple[float, str]]] = {
            ep: deque(maxlen=_MAX_EVENTS) for ep in _ENDPOINTS
        }
        self._last_event: dict[str, Optional[float]] = {ep: None for ep in _ENDPOINTS}
        self._started_at = time.time()

    def record(self, endpoint: str, category: str) -> None:
        """Record one outcome. Unknown endpoints/categories are ignored so a
        stray label can never crash a fetch path."""
        if endpoint not in self._totals or category not in _CATEGORIES:
            return
        now = time.monotonic()
        with self._lock:
            self._totals[endpoint][category] += 1
            self._recent[endpoint].append((now, category))
            self._last_event[endpoint] = time.time()

    def record_status(self, endpoint: str, status: Optional[int]) -> None:
        """Convenience: classify an HTTP status and record it."""
        self.record(endpoint, classify_status(status))

    def _recent_counts(self, endpoint: str, now: float) -> dict[str, int]:
        cutoff = now - _WINDOW_SECONDS
        counts = {cat: 0 for cat in _CATEGORIES}
        for ts, cat in self._recent[endpoint]:
            if ts >= cutoff:
                counts[cat] += 1
        return counts

    def snapshot(self) -> dict:
        """A JSON-friendly readout: per-endpoint totals + last-hour window.

        Each endpoint carries {total, ok, unauthorized, rate_limited, not_found,
        error, recent:{...}, recent_total, recent_block_rate, last_event}.
        `recent_block_rate` is the fraction of the last hour's ATTEMPTS that were
        401/403 — the single number that answers "is Instagram blocking us right
        now" — as a 0–1 float (None when there were no recent attempts).
        """
        now = time.monotonic()
        out: dict = {"endpoints": {}, "uptime_seconds": time.time() - self._started_at}
        with self._lock:
            for ep in _ENDPOINTS:
                totals = dict(self._totals[ep])
                recent = self._recent_counts(ep, now)
                recent_total = sum(recent.values())
                block_rate = (
                    recent[UNAUTHORIZED] / recent_total if recent_total else None
                )
                out["endpoints"][ep] = {
                    "total": sum(totals.values()),
                    **totals,
                    "recent": recent,
                    "recent_total": recent_total,
                    "recent_block_rate": block_rate,
                    "last_event": self._last_event[ep],
                }
        return out

    def reset(self) -> None:
        """Clear all counters (used by tests)."""
        with self._lock:
            for ep in _ENDPOINTS:
                for cat in _CATEGORIES:
                    self._totals[ep][cat] = 0
                self._recent[ep].clear()
                self._last_event[ep] = None
            self._started_at = time.time()


# Process-wide singleton. Import and call fetch_health.record_status(...) at any
# fetch outcome point; call fetch_health.snapshot() to read it in /status.
fetch_health = FetchHealth()


def render_health_lines(snapshot: dict) -> list[str]:
    """Human-readable /status lines summarizing fetch health (HTML-safe).

    One line per endpoint with last-hour attempt count and, when there were
    any, the block rate and a traffic-light. Returns [] when nothing has been
    fetched yet so /status stays clean on a fresh start.
    """
    labels = {
        IG_PROFILE: "Profile API",
        IG_REEL: "Reel query",
        SAVEINSTA: "saveinsta",
    }
    lines: list[str] = []
    endpoints = snapshot.get("endpoints", {})
    any_traffic = any(
        endpoints.get(ep, {}).get("recent_total") for ep in _ENDPOINTS
    )
    if not any_traffic:
        return lines
    lines.append("<b>Fetch health (last hour)</b>")
    for ep in _ENDPOINTS:
        data = endpoints.get(ep, {})
        recent_total = data.get("recent_total", 0)
        if not recent_total:
            continue
        rate = data.get("recent_block_rate")
        recent = data.get("recent", {})
        ok = recent.get(OK, 0)
        if rate is None:
            light = "⚪"
        elif rate >= 0.5:
            light = "🔴"
        elif rate >= 0.2:
            light = "🟡"
        else:
            light = "🟢"
        detail = f"{ok}/{recent_total} ok"
        if rate:
            detail += f" · {round(rate * 100)}% blocked"
        errs = recent.get(ERROR, 0)
        if errs:
            detail += f" · {errs} err"
        lines.append(f"{light} {labels[ep]}: {detail}")
    return lines
