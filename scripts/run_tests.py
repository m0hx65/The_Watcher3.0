"""Run every offline test suite and report one pass/fail summary.

Each `scripts/test_*.py` is a standalone script that exits non-zero on failure
(there's no pytest dependency). This runner discovers them, runs each in its own
subprocess so a crash or a leaked event loop in one can't poison another, and
returns a non-zero exit code if any suite fails — which is exactly what CI needs.

Live-network probes (which need real Instagram/proxy access) are skipped by
default; pass --all to include them.

    python scripts/run_tests.py           # all offline suites
    python scripts/run_tests.py --all     # include live-network probes
    python scripts/run_tests.py -k pic    # only suites whose name contains "pic"
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent

# Suites that hit the real network / third-party services. Skipped unless --all.
# They fail on any machine without outbound access (CI sandboxes included), and
# a red suite that means "no internet here" teaches everyone to ignore red.
LIVE_SUITES = {
    "test_proxy_live.py",
    "test_ig_fetch.py",    # probes instagram.com directly
    "test_stories.py",     # end-to-end saveinsta.to + reel query smoke test
}


def discover(pattern: str | None, include_live: bool) -> list[Path]:
    suites = sorted(SCRIPTS_DIR.glob("test_*.py"))
    out: list[Path] = []
    for path in suites:
        if not include_live and path.name in LIVE_SUITES:
            continue
        if pattern and pattern not in path.name:
            continue
        out.append(path)
    return out


def run_one(path: Path) -> tuple[bool, float, str]:
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(path)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    elapsed = time.monotonic() - start
    return proc.returncode == 0, elapsed, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    # Suite output is captured as UTF-8 (emoji, box drawing, the U+FFFD that
    # `errors="replace"` leaves behind). A Windows console defaults to cp1252
    # and would crash the runner while printing a FAILING suite's output —
    # losing the report exactly when it matters.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Run the offline test suites.")
    parser.add_argument("--all", action="store_true", help="include live-network probes")
    parser.add_argument("-k", dest="pattern", default=None, help="only suites whose name contains this")
    parser.add_argument("-v", "--verbose", action="store_true", help="print each suite's output")
    args = parser.parse_args()

    suites = discover(args.pattern, args.all)
    if not suites:
        print("No matching test suites found.")
        return 1

    print(f"Running {len(suites)} suite(s)…\n")
    failures: list[str] = []
    total_time = 0.0
    for path in suites:
        ok, elapsed, output = run_one(path)
        total_time += elapsed
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {path.name}  ({elapsed:.1f}s)")
        if args.verbose or not ok:
            indented = "\n".join("    " + line for line in output.rstrip().splitlines())
            if indented:
                print(indented)
        if not ok:
            failures.append(path.name)

    print("\n" + "=" * 60)
    passed = len(suites) - len(failures)
    print(f"{passed}/{len(suites)} suites passed in {total_time:.1f}s")
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("All suites passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
