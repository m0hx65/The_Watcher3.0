"""Regression tests for follower-anomaly detection (app/monitor/analytics.py).

A follower change is flagged only when it's large in BOTH absolute and relative
terms, so it never fires on a small account's noise or a big account's normal
drift. Pure function — runs fully offline.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.monitor.analytics import (  # noqa: E402
    classify_follower_change,
    render_follower_anomaly,
)

FAILURES: list[str] = []
ABS = 500
PCT = 0.10


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def classify(old, new):
    return classify_follower_change(old, new, abs_min=ABS, pct_min=PCT)


def test_disabled_and_guards() -> None:
    expect("abs_min=0 disables",
           classify_follower_change(1000, 5000, abs_min=0, pct_min=PCT) is None)
    expect("pct_min=0 disables",
           classify_follower_change(1000, 5000, abs_min=ABS, pct_min=0) is None)
    expect("None old -> None", classify(None, 5000) is None)
    expect("None new -> None", classify(5000, None) is None)
    expect("no baseline (old=0) -> None", classify(0, 5000) is None)
    expect("no change -> None", classify(5000, 5000) is None)
    expect("non-numeric -> None", classify("x", "y") is None)


def test_requires_both_thresholds() -> None:
    # Big account, big absolute (+4000) but small relative (0.8%): normal drift.
    expect("big-abs small-pct is NOT an anomaly",
           classify(500_000, 504_000) is None)
    # Small account, big relative (+50%) but small absolute (+100): noise.
    expect("big-pct small-abs is NOT an anomaly",
           classify(200, 300) is None)
    # Both large: a genuine spike.
    a = classify(10_000, 12_000)  # +2000 (20%)
    expect("both-large IS an anomaly", a is not None)
    expect("classified as a spike", a and a.direction == "spike")
    expect("delta is signed positive", a and a.delta == 2000)


def test_drop() -> None:
    a = classify(20_000, 16_000)  # -4000 (20%)
    expect("large loss IS an anomaly", a is not None)
    expect("classified as a drop", a and a.direction == "drop")
    expect("delta is signed negative", a and a.delta == -4000)
    expect("pct is signed negative", a and a.pct < 0)


def test_boundary() -> None:
    # Exactly at both floors (500 abs, 10%): inclusive → flagged.
    a = classify(5000, 5500)  # +500 (10%)
    expect("exactly at both floors is flagged", a is not None,
           repr(a))
    # Just under the absolute floor.
    expect("just under abs floor is not flagged",
           classify(5000, 5499) is None)


def test_render() -> None:
    a = classify(10_000, 12_500)  # +2500 (25%)
    text = render_follower_anomaly("targetuser", a)
    expect("render mentions the user", "@targetuser" in text, text)
    expect("render says spike", "spike" in text, text)
    expect("render shows gained", "gained" in text, text)
    expect("render shows the percentage", "25%" in text, text)
    expect("render is HTML-bold somewhere", "<b>" in text, text)

    drop = classify(10_000, 7_000)  # -3000 (30%)
    dtext = render_follower_anomaly("targetuser", drop)
    expect("drop render says lost", "lost" in dtext, dtext)
    expect("drop render says drop", "drop" in dtext, dtext)


def main() -> int:
    test_disabled_and_guards()
    test_requires_both_thresholds()
    test_drop()
    test_boundary()
    test_render()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All follower-anomaly tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
