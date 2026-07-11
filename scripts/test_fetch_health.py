"""Regression tests for the fetch-health telemetry (app/monitor/health.py).

Telemetry records outcomes only — it never changes a request. These checks
verify the classification, the rolling-window block rate, and the /status
render helper, plus that recording is resilient to junk labels.

Runs fully offline.
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

from app.monitor.health import (  # noqa: E402
    ERROR,
    IG_PROFILE,
    IG_REEL,
    OK,
    SAVEINSTA,
    UNAUTHORIZED,
    FetchHealth,
    classify_status,
    render_health_lines,
)

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def test_classify() -> None:
    expect("200 -> ok", classify_status(200) == OK)
    expect("401 -> unauthorized", classify_status(401) == UNAUTHORIZED)
    expect("403 -> unauthorized", classify_status(403) == UNAUTHORIZED)
    expect("429 -> rate_limited", classify_status(429) == "rate_limited")
    expect("404 -> not_found", classify_status(404) == "not_found")
    expect("0 -> error", classify_status(0) == ERROR)
    expect("None -> error", classify_status(None) == ERROR)
    expect("500 -> error", classify_status(500) == ERROR)


def test_counts_and_block_rate() -> None:
    h = FetchHealth()
    # Simulate the reported situation: 13 profile fetches, 9 blocked.
    for _ in range(4):
        h.record_status(IG_PROFILE, 200)
    for _ in range(9):
        h.record_status(IG_PROFILE, 401)

    snap = h.snapshot()
    prof = snap["endpoints"][IG_PROFILE]
    expect("profile total counted", prof["total"] == 13, repr(prof["total"]))
    expect("profile ok counted", prof[OK] == 4, repr(prof[OK]))
    expect("profile unauthorized counted", prof[UNAUTHORIZED] == 9)
    expect("recent total matches", prof["recent_total"] == 13)
    expect(
        "block rate is 9/13",
        abs(prof["recent_block_rate"] - 9 / 13) < 1e-9,
        repr(prof["recent_block_rate"]),
    )
    # Untouched endpoints report a clean zero, not a crash.
    expect("idle endpoint has no traffic",
           snap["endpoints"][SAVEINSTA]["recent_total"] == 0)
    expect("idle endpoint block rate is None",
           snap["endpoints"][SAVEINSTA]["recent_block_rate"] is None)


def test_junk_is_ignored() -> None:
    h = FetchHealth()
    h.record("not_a_real_endpoint", OK)   # unknown endpoint
    h.record(IG_PROFILE, "not_a_category")  # unknown category
    snap = h.snapshot()
    expect("junk endpoint ignored", "not_a_real_endpoint" not in snap["endpoints"])
    expect("junk category ignored",
           snap["endpoints"][IG_PROFILE]["total"] == 0)


def test_render_lines() -> None:
    h = FetchHealth()
    # Nothing fetched yet -> no health block (keeps /status clean on boot).
    expect("no lines before any traffic", render_health_lines(h.snapshot()) == [])

    for _ in range(8):
        h.record_status(IG_PROFILE, 200)
    for _ in range(2):
        h.record_status(IG_PROFILE, 401)
    for _ in range(5):
        h.record_status(IG_REEL, 200)

    lines = render_health_lines(h.snapshot())
    text = "\n".join(lines)
    expect("render has a header", lines and "Fetch health" in lines[0], text)
    expect("profile line present", "Profile API" in text, text)
    expect("reel line present", "Reel query" in text, text)
    # 2/10 profile blocked = 20% -> yellow light.
    expect("20% block shows yellow", "🟡" in text, text)
    expect("block percent rendered", "20% blocked" in text, text)
    # Reel had zero blocks -> green.
    expect("clean endpoint shows green", "🟢" in text, text)
    # saveinsta had no traffic -> no line for it.
    expect("idle endpoint omitted", "saveinsta" not in text, text)


def test_reset() -> None:
    h = FetchHealth()
    h.record_status(IG_PROFILE, 200)
    h.reset()
    expect("reset clears counters",
           h.snapshot()["endpoints"][IG_PROFILE]["total"] == 0)


def main() -> int:
    test_classify()
    test_counts_and_block_rate()
    test_junk_is_ignored()
    test_render_lines()
    test_reset()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All fetch-health tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
