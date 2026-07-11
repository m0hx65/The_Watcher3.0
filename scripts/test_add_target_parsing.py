"""Tests for _parse_add_target — the /add input parser.

Security-relevant: the parser must recognize genuine Instagram profile URLs and
usernames while REJECTING look-alike / embedded hosts (the "incomplete URL
substring sanitization" class). The anchored regex is the only host check; there
is no fragile `"instagram.com" in raw` substring test.

Runs offline.
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

from app.bot.handlers import _parse_add_target  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def test_valid_inputs() -> None:
    cases = {
        "user": ("user", None),
        "@user": ("user", None),
        "Some.User_1": ("some.user_1", None),
        "1234567890": (None, "1234567890"),
        "instagram.com/target": ("target", None),
        "www.instagram.com/target": ("target", None),
        "https://instagram.com/target": ("target", None),
        "https://www.instagram.com/target/": ("target", None),
        "HTTPS://Instagram.com/Target": ("target", None),
        "https://www.instagram.com/target?hl=en": ("target", None),
    }
    for raw, expected in cases.items():
        got = _parse_add_target(raw)
        expect(f"valid: {raw!r} -> {expected}", got == expected, repr(got))


def test_lookalike_hosts_rejected() -> None:
    # Every one of these contains the literal "instagram.com" but is NOT a real
    # Instagram profile URL. All must parse to (None, None).
    hostile = [
        "evilinstagram.com/victim",
        "instagram.com.evil.com/victim",
        "https://evil.com/instagram.com/victim",
        "https://evil.com/?next=instagram.com/victim",
        "notinstagram.com/victim",
        "https://instagram.com.attacker.net/victim",
        "http://instagramXcom/victim",
    ]
    for raw in hostile:
        got = _parse_add_target(raw)
        expect(f"look-alike rejected: {raw!r}", got == (None, None), repr(got))


def test_other_urls_rejected() -> None:
    for raw in [
        "https://twitter.com/foo",
        "https://example.com/",
        "ftp://instagram.com/foo",  # unsupported scheme -> has "://", not matched
        "foo/bar",
    ]:
        got = _parse_add_target(raw)
        expect(f"non-IG URL rejected: {raw!r}", got == (None, None), repr(got))


def test_empty() -> None:
    expect("empty string", _parse_add_target("") == (None, None))
    expect("whitespace", _parse_add_target("   ") == (None, None))


def main() -> int:
    test_valid_inputs()
    test_lookalike_hosts_rejected()
    test_other_urls_rejected()
    test_empty()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All add-target parsing tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
