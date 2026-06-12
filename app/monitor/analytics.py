"""Pure functions for activity-rhythm analytics over delivered-item times.

Kept dependency-free (no DB, no Telegram) so it's trivially unit-testable: the
caller pulls timestamps from seen_stories via crud and passes them in.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.utils.formatting import DAMASCUS_TZ, esc

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


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
