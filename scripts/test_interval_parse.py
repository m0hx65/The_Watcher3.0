"""Smoke test the _parse_interval / _format_interval helpers without spinning
up Telegram or the database."""

from __future__ import annotations

import os
import sys

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "x")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")

from app.bot.handlers import _format_interval, _parse_interval

CASES = [
    ("30m", 1800),
    ("1h", 3600),
    ("1H", 3600),
    ("1h30m", 5400),
    ("90s", 90),
    ("1800s", 1800),
    ("1800", 1800),
    ("2h15m30s", 2 * 3600 + 15 * 60 + 30),
    (" 45m ", 2700),
    ("", None),
    ("nope", None),
    ("0", None),
    ("0m0s", None),
]

fmt_cases = [
    (60, "1m"),
    (90, "1m30s"),
    (1800, "30m"),
    (3600, "1h"),
    (5400, "1h30m"),
    (3661, "1h1m1s"),
    (0, "0s"),
]


def main() -> int:
    failures: list[str] = []
    for raw, expected in CASES:
        got = _parse_interval(raw)
        ok = got == expected
        print(f"parse({raw!r}) -> {got!r} (expected {expected!r}) {'ok' if ok else 'FAIL'}")
        if not ok:
            failures.append(f"parse({raw!r}) -> {got!r} != {expected!r}")

    for seconds, expected in fmt_cases:
        got = _format_interval(seconds)
        ok = got == expected
        print(f"fmt({seconds}) -> {got!r} (expected {expected!r}) {'ok' if ok else 'FAIL'}")
        if not ok:
            failures.append(f"fmt({seconds}) -> {got!r} != {expected!r}")

    if failures:
        print("\n".join(failures))
        return 1
    print("all good")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
