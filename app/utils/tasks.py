"""Background tasks that keep a reference and report their own failures.

`asyncio.create_task(...)` on its own has two sharp edges, and this bot ran a
whole SWEEP through them:

- the event loop keeps only a WEAK reference to a task, so one whose result
  nobody holds can be garbage-collected mid-flight and simply stop;
- an exception in a task nobody awaits surfaces as a "Task exception was never
  retrieved" warning at collection time, if at all — so a sweep launched from
  the Telegram button or `POST /sweep` could die with its traceback going
  nowhere.

`spawn()` fixes both: the task is held until it finishes, and whatever it
raises is logged against a name that says where it came from.
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, Set

from app.utils.logger import logger

# Strong references to everything in flight. Entries remove themselves on
# completion, so this only ever holds what is actually running.
_BACKGROUND: Set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
    """Run `coro` in the background, held and reported."""
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND.add(task)
    task.add_done_callback(_finished)
    return task


def pending() -> int:
    """How many background tasks are in flight — for tests and diagnostics."""
    return len(_BACKGROUND)


def _finished(task: asyncio.Task) -> None:
    _BACKGROUND.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.opt(exception=exc).error(
            "Background task '{}' failed", task.get_name()
        )


__all__ = ["spawn", "pending"]
