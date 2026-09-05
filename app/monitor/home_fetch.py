"""The home fetcher's job broker — the bot's side of the fourth door.

A machine on a connection Instagram trusts (the owner's phone or PC — see
tools/home_fetcher) polls ``GET /home-fetch/jobs``, fetches the profile page
it is handed, and POSTs the HTML back to ``/home-fetch/jobs/<id>``. Nothing
dials INTO the home network: the owner's line sits behind carrier-grade NAT (a
10.x WAN address) and an unrooted phone can neither forward a port nor run a
Tailscale Funnel — so the worker pulls work over ordinary outbound HTTPS, and
this broker hands it out and matches each answer to the check waiting for it.

Everything lives in memory. A job outlives neither the check that asked for it
nor this process, and a worker that is not polling is simply "not connected" —
a fast, quiet answer, so a phone that is off costs a sweep nothing.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from app.utils.logger import logger

# A worker that has polled within this many seconds counts as connected. Polls
# are long (up to POLL_WAIT_MAX seconds each) and back to back, so a healthy
# worker is never more than one poll away.
CONNECTED_WINDOW_SECONDS = 90.0
# The longest a single poll may hold its connection open before answering
# "no job". Well under any proxy/host request timeout.
POLL_WAIT_MAX_SECONDS = 25.0


@dataclass
class PageJob:
    id: str
    username: str
    created: float = field(default_factory=time.monotonic)


@dataclass
class PageResult:
    """What Instagram told the worker: its own status, the HTML, the final URL
    after redirects (a login-page URL means the home IP was refused too)."""

    status: int
    body: str
    final_url: str = ""


class HomeFetchBroker:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue[PageJob]] = None
        self._waiters: dict[str, asyncio.Future[PageResult]] = {}
        self._last_poll: Optional[float] = None
        self._worker: Optional[str] = None
        self.delivered = 0
        self.timed_out = 0

    # ----------------------------------------------------------- state

    @property
    def connected(self) -> bool:
        return (
            self._last_poll is not None
            and time.monotonic() - self._last_poll < CONNECTED_WINDOW_SECONDS
        )

    @property
    def last_seen_seconds(self) -> Optional[float]:
        if self._last_poll is None:
            return None
        return time.monotonic() - self._last_poll

    def describe(self) -> str:
        """One phrase for /status and /probe: who, and how long ago."""
        seen = self.last_seen_seconds
        if seen is None:
            return "not connected (no worker has ever polled)"
        who = f"worker {self._worker}" if self._worker else "worker"
        if self.connected:
            return f"connected ({who}, last poll {seen:.0f}s ago)"
        minutes = seen / 60.0
        ago = f"{seen:.0f}s" if minutes < 1 else f"{minutes:.0f} min"
        return f"not connected ({who} last polled {ago} ago)"

    # ------------------------------------------------------ the bot side

    async def request_page(
        self, username: str, *, timeout: float = 30.0
    ) -> Optional[PageResult]:
        """Hand `username` to the worker and wait for the page.

        Returns None at once when no worker is connected, and None after
        `timeout` seconds when the worker took the job but never answered
        (its own Instagram request hung, or the phone dropped off mid-way).
        """
        if not self.connected:
            return None
        loop = asyncio.get_running_loop()
        job = PageJob(id=uuid.uuid4().hex, username=username)
        future: asyncio.Future[PageResult] = loop.create_future()
        self._waiters[job.id] = future
        await self._get_queue().put(job)
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self.timed_out += 1
            logger.info(
                "Home fetcher did not answer for @{} within {:.0f}s",
                username, timeout,
            )
            return None
        finally:
            self._waiters.pop(job.id, None)

    # --------------------------------------------------- the worker side

    async def next_job(self, *, wait: float, worker: str) -> Optional[PageJob]:
        """Long-poll: the next job, or None after `wait` seconds of nothing.

        Marks the worker as connected on the way in and again on the way out,
        so a job handed over just before the connected window would otherwise
        lapse keeps the worker counted as present.
        """
        self._last_poll = time.monotonic()
        self._worker = worker
        deadline = time.monotonic() + max(0.0, min(wait, POLL_WAIT_MAX_SECONDS))
        queue = self._get_queue()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                job = await asyncio.wait_for(queue.get(), remaining)
            except asyncio.TimeoutError:
                return None
            self._last_poll = time.monotonic()
            if job.id in self._waiters:
                return job
            # The check that asked gave up already — nobody is waiting for
            # this one, so don't send the worker after it.

    def deliver(self, job_id: str, result: PageResult) -> bool:
        """The worker's answer. False when nobody is waiting for it any more."""
        future = self._waiters.get(job_id)
        if future is None or future.done():
            return False
        future.set_result(result)
        self.delivered += 1
        return True

    # ---------------------------------------------------------- internal

    def _get_queue(self) -> asyncio.Queue[PageJob]:
        # An asyncio.Queue binds to the loop that first uses it; a fresh loop
        # (tests, a restart of the server loop) gets a fresh queue. Pending
        # jobs belong to the old loop's checks and are already lost with it.
        loop = asyncio.get_running_loop()
        if self._queue is None or self._loop is not loop:
            self._queue = asyncio.Queue()
            self._loop = loop
        return self._queue


broker = HomeFetchBroker()
