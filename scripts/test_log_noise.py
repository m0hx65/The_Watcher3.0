"""The health checker must not bury the logs (2026-09-07).

Render pings /health every few seconds and uvicorn logs an access line for
each — about twelve a minute of "still alive", enough that a sweep's own
output arrives between two of them and the owner has to hunt for it.

The filter that hides them is the sort of code that is only ever noticed when
it is wrong, and being wrong here means a FAILING health check disappears —
trading noise for the one line worth seeing. So: passing checks on /health
and /ready are dropped, everything else is kept, and anything the filter
cannot confidently parse is kept too.

Runs fully offline.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.config import settings  # noqa: E402
from app.utils.logger import (  # noqa: E402
    HealthCheckAccessFilter, quiet_health_check_logs,
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


def _access(path: str, status: int, method: str = "GET") -> logging.LogRecord:
    """One uvicorn access record. Its formatter reads a 5-tuple of
    (client_addr, method, full_path, http_version, status_code) — this
    mirrors that shape exactly, because the filter reads the same tuple."""
    record = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg='%s - "%s %s HTTP/%s" %d', args=("10.0.0.1:1234", method, path, "1.1", status),
        exc_info=None,
    )
    return record


def test_passing_health_checks_are_dropped() -> None:
    f = HealthCheckAccessFilter()
    expect("a passing /health line is dropped", not f.filter(_access("/health", 200)))
    expect("so is /ready", not f.filter(_access("/ready", 200)))
    expect("and a HEAD check", not f.filter(_access("/health", 200, "HEAD")))
    expect("a 3xx redirect on /health is still quiet",
           not f.filter(_access("/health", 301)))


def test_a_failing_health_check_is_always_kept() -> None:
    """The line that matters. Hiding this would trade the noise for the one
    symptom worth seeing."""
    f = HealthCheckAccessFilter()
    expect("a 500 on /health is kept", f.filter(_access("/health", 500)))
    expect("a 503 on /ready is kept", f.filter(_access("/ready", 503)))
    expect("so is a 404", f.filter(_access("/health", 404)))


def test_everything_else_is_untouched() -> None:
    f = HealthCheckAccessFilter()
    expect("the phone's polls still log",
           f.filter(_access("/home-fetch/jobs?wait=25&batch=8", 200)))
    expect("so do Telegram updates",
           f.filter(_access("/telegram/webhook", 200)))
    expect("and a path that merely starts with /health",
           f.filter(_access("/healthz", 200)))
    expect("a query string does not smuggle a path past the check",
           not f.filter(_access("/health?probe=1", 200)))


def test_an_unparseable_record_is_kept() -> None:
    """A log filter must never be the reason a record went missing, so
    anything that does not look like uvicorn's access tuple passes."""
    f = HealthCheckAccessFilter()
    plain = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg="something else entirely", args=None, exc_info=None,
    )
    expect("a record with no args is kept", f.filter(plain))
    short = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg="%s %s", args=("/health", 200), exc_info=None,
    )
    expect("a tuple of the wrong shape is kept", f.filter(short))
    odd = _access("/health", 200)
    odd.args = ("10.0.0.1", "GET", "/health", "1.1", "not-a-status")
    expect("an unreadable status is kept", f.filter(odd))


def test_install_is_idempotent_and_opt_out() -> None:
    access = logging.getLogger("uvicorn.access")
    before = list(access.filters)
    old = settings.log_health_checks
    try:
        settings.log_health_checks = False
        quiet_health_check_logs()
        quiet_health_check_logs()
        installed = [
            f for f in access.filters if isinstance(f, HealthCheckAccessFilter)
        ]
        expect("the filter is installed exactly once, however often it is called",
               len(installed) == 1, repr(access.filters))

        # Opt back in: someone asking "is the health checker running at all?"
        # needs the lines, so the toggle must actually leave them alone.
        access.filters = list(before)
        settings.log_health_checks = True
        quiet_health_check_logs()
        expect("LOG_HEALTH_CHECKS=true installs nothing",
               not any(isinstance(f, HealthCheckAccessFilter)
                       for f in access.filters), repr(access.filters))
    finally:
        settings.log_health_checks = old
        access.filters = list(before)


def main() -> int:
    test_passing_health_checks_are_dropped()
    test_a_failing_health_check_is_always_kept()
    test_everything_else_is_untouched()
    test_an_unparseable_record_is_kept()
    test_install_is_idempotent_and_opt_out()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("All log-noise checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
