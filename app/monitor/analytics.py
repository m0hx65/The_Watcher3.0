"""Pure functions for activity-rhythm analytics over delivered-item times.

Kept dependency-free (no DB, no Telegram) so it's trivially unit-testable: the
caller pulls timestamps from seen_stories via crud and passes them in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.utils.formatting import DAMASCUS_TZ, esc, fmt_number

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class FollowerAnomaly:
    """An unusually large follower jump between two consecutive observations."""

    direction: str  # "spike" | "drop"
    old: int
    new: int
    delta: int      # signed (new - old)
    pct: float      # signed fraction, e.g. -0.12 for a 12% drop


def classify_follower_change(
    old: Optional[int],
    new: Optional[int],
    *,
    abs_min: int,
    pct_min: float,
) -> Optional[FollowerAnomaly]:
    """Flag a follower change that's large in BOTH absolute and relative terms.

    Requiring both thresholds is what keeps this from crying wolf: a tiny
    account's ±% swings are ignored because the absolute floor isn't met, and a
    huge account's normal daily drift is ignored because the percentage floor
    isn't met. Only a jump that's big for THIS account's size qualifies.

    Returns None when disabled (either threshold ≤ 0), when there's no prior
    baseline (old ≤ 0), or when the change is within normal range.
    """
    if old is None or new is None:
        return None
    if abs_min <= 0 or pct_min <= 0:
        return None  # detection disabled
    try:
        old = int(old)
        new = int(new)
    except (TypeError, ValueError):
        return None
    if old <= 0:
        return None  # no meaningful baseline to measure against
    delta = new - old
    if delta == 0:
        return None
    pct = delta / old
    if abs(delta) >= abs_min and abs(pct) >= pct_min:
        return FollowerAnomaly(
            direction="spike" if delta > 0 else "drop",
            old=old,
            new=new,
            delta=delta,
            pct=pct,
        )
    return None


def render_follower_anomaly(username: str, anomaly: FollowerAnomaly) -> str:
    """Render a high-visibility alert for an unusual follower jump (HTML)."""
    arrow = "📈" if anomaly.direction == "spike" else "📉"
    verb = "gained" if anomaly.delta > 0 else "lost"
    return (
        f"⚠️ {arrow} <b>@{esc(username)}</b> — unusual follower {anomaly.direction}\n"
        f"{verb} <b>{fmt_number(abs(anomaly.delta))}</b> "
        f"({abs(anomaly.pct) * 100:.0f}%) in one check\n"
        f"{fmt_number(anomaly.old)} → {fmt_number(anomaly.new)}"
    )


def compute_rhythm(
    timestamps: list[datetime], tz: timezone = DAMASCUS_TZ
) -> dict:
    """Bucket activity timestamps into hour-of-day and day-of-week histograms.

    Returns {total, by_hour (24), by_weekday (7), peak_hour, quiet_hour,
    peak_weekday}. All bucketing is in the given local timezone so the result
    matches the times the user sees elsewhere in the bot.
    """
    by_hour = [0] * 24
    by_weekday = [0] * 7
    for ts in timestamps:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local = ts.astimezone(tz)
        by_hour[local.hour] += 1
        by_weekday[local.weekday()] += 1

    total = sum(by_hour)
    peak_hour = max(range(24), key=lambda h: by_hour[h]) if total else None
    quiet_hour = min(range(24), key=lambda h: by_hour[h]) if total else None
    peak_weekday = (
        max(range(7), key=lambda d: by_weekday[d]) if total else None
    )
    return {
        "total": total,
        "by_hour": by_hour,
        "by_weekday": by_weekday,
        "peak_hour": peak_hour,
        "quiet_hour": quiet_hour,
        "peak_weekday": peak_weekday,
    }


def _bar(count: int, peak: int, width: int = 10) -> str:
    """A proportional bar of block characters (empty peak → no bar)."""
    if peak <= 0 or count <= 0:
        return ""
    filled = max(1, round(count / peak * width))
    return "█" * filled


def _busiest_window(by_hour: list[int], span: int = 3) -> Optional[tuple[int, int]]:
    """Return the (start_hour, end_hour) of the busiest `span`-hour window."""
    total = sum(by_hour)
    if total == 0:
        return None
    best_start, best_sum = 0, -1
    for start in range(24):
        window = sum(by_hour[(start + i) % 24] for i in range(span))
        if window > best_sum:
            best_sum, best_start = window, start
    return best_start, (best_start + span) % 24


def render_rhythm(
    username: str,
    rhythm: dict,
    *,
    first: Optional[datetime] = None,
    last: Optional[datetime] = None,
    tz: timezone = DAMASCUS_TZ,
) -> str:
    """Render the rhythm as an HTML text block with hour & weekday histograms."""
    total = rhythm["total"]
    if total == 0:
        return (
            f"📊 <b>Activity rhythm — @{esc(username)}</b>\n\n"
            "No delivered stories, posts, or highlights yet, so there's no "
            "rhythm to chart. Once the bot catches activity, patterns appear "
            "here."
        )

    by_hour = rhythm["by_hour"]
    by_weekday = rhythm["by_weekday"]
    hour_peak = max(by_hour)

    lines = [
        f"📊 <b>Activity rhythm — @{esc(username)}</b>",
        f"<i>Based on {total} item{'s' if total != 1 else ''} caught "
        f"(stories · posts · highlights), times in Damascus.</i>",
        "",
        "<b>By hour</b>",
    ]
    # Compact 24-hour histogram, two hours per line gets noisy — one per line
    # but only show hours with activity plus a few neighbours would be complex;
    # a full 24-row block is the clearest and fits well under Telegram's limit.
    for h in range(24):
        bar = _bar(by_hour[h], hour_peak)
        count = f" {by_hour[h]}" if by_hour[h] else ""
        lines.append(f"<code>{h:02d}</code> {bar}{count}")

    window = _busiest_window(by_hour)
    if window is not None:
        start, end = window
        lines.append("")
        lines.append(f"🔥 Most active: <b>{start:02d}:00–{end:02d}:00</b>")
    if rhythm["quiet_hour"] is not None and hour_peak > 0:
        lines.append(
            f"😴 Quietest hour: <b>{rhythm['quiet_hour']:02d}:00</b>"
        )

    wk_peak = max(by_weekday)
    lines.append("")
    lines.append("<b>By day of week</b>")
    for d in range(7):
        bar = _bar(by_weekday[d], wk_peak, width=10)
        count = f" {by_weekday[d]}" if by_weekday[d] else ""
        lines.append(f"<code>{_WEEKDAYS[d]}</code> {bar}{count}")

    if first is not None and last is not None:
        span_days = max(1, (last - first).days + 1)
        lines.append("")
        lines.append(
            f"<i>Window: {span_days} day{'s' if span_days != 1 else ''} of "
            f"observation.</i>"
        )
    return "\n".join(lines)
