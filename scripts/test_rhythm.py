"""Unit test for the activity-rhythm analytics (pure, no DB/network)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.monitor.analytics import compute_rhythm, render_rhythm  # noqa: E402
from app.utils.formatting import DAMASCUS_TZ  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, cond: bool, detail: str = "") -> None:
    print(("ok" if cond else "FAIL") + f": {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    # Empty input.
    r = compute_rhythm([])
    expect("empty total is 0", r["total"] == 0)
    expect("empty peak_hour None", r["peak_hour"] is None)
    msg = render_rhythm("ghost", r)
    expect("empty render mentions no rhythm", "no rhythm" in msg.lower() or "No delivered" in msg)

    # Build timestamps: 5 items at 22:00 Damascus, 2 at 09:00 Damascus, on a Monday.
    # Damascus is UTC+3, so 22:00 local = 19:00 UTC.
    base = datetime(2026, 6, 8, tzinfo=DAMASCUS_TZ)  # 2026-06-08 is a Monday
    ts = []
    for _ in range(5):
        ts.append(base.replace(hour=22).astimezone(timezone.utc))
    for _ in range(2):
        ts.append(base.replace(hour=9).astimezone(timezone.utc))

    r = compute_rhythm(ts)
    expect("total counts all items", r["total"] == 7, str(r["total"]))
    expect("peak hour is 22 (local)", r["peak_hour"] == 22, str(r["peak_hour"]))
    expect("hour 22 bucket = 5", r["by_hour"][22] == 5, str(r["by_hour"][22]))
    expect("hour 09 bucket = 2", r["by_hour"][9] == 2, str(r["by_hour"][9]))
    expect("peak weekday is Monday(0)", r["peak_weekday"] == 0, str(r["peak_weekday"]))
    expect("weekday Mon bucket = 7", r["by_weekday"][0] == 7, str(r["by_weekday"][0]))

    msg = render_rhythm("nightowl", r, first=ts[0], last=ts[-1])
    expect("render has the handle", "@nightowl" in msg)
    expect("render shows most-active window", "Most active" in msg)
    expect("render has by-day section", "By day of week" in msg)

    # Timezone correctness: an item at 00:30 UTC must land at 03:30 Damascus.
    utc_midnight = datetime(2026, 6, 8, 0, 30, tzinfo=timezone.utc)
    r2 = compute_rhythm([utc_midnight])
    expect("UTC 00:30 -> Damascus hour 3", r2["by_hour"][3] == 1, str(r2["by_hour"]))

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {FAILURES}")
        return 1
    print("\nall good")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
