"""The home fetcher's job broker — the bot's side of the fourth door.

A device on a connection Instagram trusts (the owner's phone or PC — see
tools/home_fetcher) polls ``GET /home-fetch/jobs``, fetches what it is handed,
and POSTs Instagram's answers back to ``/home-fetch/jobs/<id>``. Nothing dials
INTO the home network: the owner's line sits behind carrier-grade NAT (a 10.x
WAN address) and an unrooted phone can neither forward a port nor run a
Tailscale Funnel — so the worker pulls work over ordinary outbound HTTPS, and
this broker hands it out and keeps the answers.

Two kinds of job:

- ``page`` — the profile page ``instagram.com/<username>/`` (counts, bio,
  privacy, story-up flag), keyed by username;
- ``reel`` — the graphql reel query by numeric id (current username, avatar,
  story/live status, highlight catalog), keyed by that id. The Worker's route
  for this is refused per colo and each refusal costs ~9 s; the phone's
  connection answers it in one.

Two things make it fast rather than merely working:

- **Checks never wait on the phone by design.** A sweep hands the broker its
  whole list up front (`prefetch`, `prefetch_reels`); the phone works through
  it — several jobs per poll, fetch after fetch — while the sweep runs, and
  each check picks its answers from the results already in hand (`cached`,
  `cached_reel`). A check only waits when its answer has not arrived yet.
- **Everything lives in memory and expires.** A result serves the sweep that
  asked for it (RESULT_TTL_SECONDS); a job nobody picked up within
  JOB_MAX_AGE_SECONDS is dropped; a worker that is not polling is simply
  "not connected" — a fast, quiet answer, so a phone that is off costs a
  sweep nothing.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.utils.logger import logger

# A worker that has polled within this many seconds counts as connected. Polls
# are long (up to POLL_WAIT_MAX seconds each) and back to back, so a healthy
# worker is never more than one poll away.
CONNECTED_WINDOW_SECONDS = 90.0
# The longest a single poll may hold its connection open before answering
# "no job". Well under any proxy/host request timeout.
POLL_WAIT_MAX_SECONDS = 25.0
# The most jobs one poll hands out. The phone fetches them one after another,
# paced, and uploads in the background, so a batch costs it nothing extra and
# saves a round trip per job.
BATCH_MAX = 8
# An answer fetched during a sweep serves that sweep. Manual checks ask fresh.
RESULT_TTL_SECONDS = 900.0
# A queued job nobody picked up in this long is stale — the sweep that
# wanted it is long over.
JOB_MAX_AGE_SECONDS = 600.0

KIND_PAGE = "page"
KIND_REEL = "reel"
KINDS = (KIND_PAGE, KIND_REEL)


@dataclass
class PageJob:
    """One unit of work for the phone. `key` is the username for a page and
    the numeric id for a reel; `username` is the account's handle either way,
    for the worker's and the bot's logs."""

    id: str
    username: str
    kind: str = KIND_PAGE
    key: str = ""
    prefetch: bool = False
    created: float = field(default_factory=time.monotonic)
    handed: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.key:
            self.key = self.username

    @property
    def user_id(self) -> Optional[str]:
        return self.key if self.kind == KIND_REEL else None


@dataclass
class PageResult:
    """What Instagram told the worker: its own status, the body (a page's
    extracted payload, or the reel query's JSON), the final URL after
    redirects (a login-page URL means the home IP was refused too)."""

    status: int
    body: str
    final_url: str = ""
    fetched_at: float = field(default_factory=time.monotonic)


class HomeFetchBroker:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue[PageJob]] = None
        # job id -> job, for everything queued or handed out and not yet
        # answered; (kind, key) -> that job, so one question is one job.
        self._jobs: dict[str, PageJob] = {}
        self._by_key: dict[tuple[str, str], PageJob] = {}
        # (kind, key) -> the checks waiting for its answer (one future each,
        # so a waiter that gives up cancels only its own).
        self._waiters: dict[tuple[str, str], list[asyncio.Future[PageResult]]] = {}
        self._results: dict[tuple[str, str], PageResult] = {}
        self._last_poll: Optional[float] = None
        self._worker: Optional[str] = None
        self._worker_kinds: frozenset[str] = frozenset({KIND_PAGE})
        self.delivered = 0
        self.timed_out = 0
        # What the worker last said about its device. A phone on a charger
        # reads "charging"; a reading that drops while NOT charging means the
        # charger fell out or the power went, and the door will close when the
        # phone dies — worth one message, not one per poll.
        self.battery: Optional[int] = None
        self.charging: Optional[bool] = None
        self._battery_alerted_at: Optional[int] = None

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

    @property
    def worker(self) -> Optional[str]:
        return self._worker

    @property
    def worker_kinds(self) -> frozenset[str]:
        """What the connected worker knows how to fetch. An older worker
        build fetches pages only; it is never handed a reel job."""
        return self._worker_kinds

    @property
    def pending(self) -> int:
        """Jobs queued or in the phone's hands, not yet answered."""
        return len(self._jobs)

    def describe(self) -> str:
        """One phrase for /status and /probe: who, how long ago, and the
        device's battery when it reports one."""
        seen = self.last_seen_seconds
        if seen is None:
            return "not connected (no worker has ever polled)"
        who = f"worker {self._worker}" if self._worker else "worker"
        battery = ""
        if self.battery is not None:
            state = (
                "" if self.charging is None
                else ", charging" if self.charging else ", not charging"
            )
            battery = f", battery {self.battery}%{state}"
        if self.connected:
            return f"connected ({who}, last poll {seen:.0f}s ago{battery})"
        minutes = seen / 60.0
        ago = f"{seen:.0f}s" if minutes < 1 else f"{minutes:.0f} min"
        return f"not connected ({who} last polled {ago} ago{battery})"

    def note_device(
        self,
        *,
        battery: Optional[int],
        charging: Optional[bool],
        threshold: int,
    ) -> Optional[str]:
        """Record the worker's battery reading; return an alert to send when
        it crossed a line, else None.

        One alert when the level is at or below `threshold` while not
        charging, one more at half the threshold, then silence until the
        phone is charging again (which is announced, since the owner was
        told to worry) or has climbed well clear of the threshold.
        `threshold` 0 disables the alerts; the reading is still shown.
        """
        self.battery, self.charging = battery, charging
        if battery is None or threshold <= 0:
            return None
        who = f"the home fetcher ({self._worker})" if self._worker else "the home fetcher"
        if charging or battery > threshold + 10:
            was_alerted = self._battery_alerted_at is not None
            self._battery_alerted_at = None
            if was_alerted and charging:
                return f"🔌 <b>{who}</b> is charging again ({battery}%)."
            return None
        if battery > threshold or charging is None:
            return None
        second_line = max(1, threshold // 2)
        if self._battery_alerted_at is None or (
            battery <= second_line and self._battery_alerted_at > second_line
        ):
            self._battery_alerted_at = battery
            return (
                f"🔋 <b>{who}</b> is at <b>{battery}%</b> and not charging — "
                "plug it in, or the profile-page door closes when it dies."
            )
        return None

    # ------------------------------------------------------ the bot side

    def cached(self, username: str) -> Optional[PageResult]:
        """A page the phone already delivered, if it is still fresh."""
        return self._cached((KIND_PAGE, username))

    def cached_reel(self, user_id: str) -> Optional[PageResult]:
        """A reel answer the phone already delivered, if it is still fresh."""
        return self._cached((KIND_REEL, str(user_id)))

    def prefetch(self, usernames: Iterable[str]) -> int:
        """Queue pages for a whole sweep at once. Returns how many were
        queued; a username already fresh in the cache or already in flight
        is not queued twice. Nothing is queued when no worker is connected —
        the sweep will find that out per check, quickly, as before."""
        return self._prefetch([(KIND_PAGE, u, u) for u in usernames])

    def prefetch_reels(self, users: Iterable[tuple[str, str]]) -> int:
        """Queue reel queries for a whole sweep: `users` is (numeric id,
        username) pairs. Only when the connected worker can fetch reels."""
        if KIND_REEL not in self._worker_kinds:
            return 0
        return self._prefetch([(KIND_REEL, str(uid), name) for uid, name in users])

    async def request_page(
        self, username: str, *, timeout: float = 30.0, fresh: bool = False
    ) -> Optional[PageResult]:
        """The page for `username`: from the cache when it is fresh and
        `fresh` is not demanded, else from the phone — joining a job already
        in flight for it, or queuing one. None at once when no worker is
        connected; None after `timeout` when the phone never answered."""
        return await self._request((KIND_PAGE, username), username, timeout, fresh)

    async def request_reel(
        self, user_id: str, username: str = "", *, timeout: float = 15.0,
        fresh: bool = False,
    ) -> Optional[PageResult]:
        """The reel query for `user_id`, from the phone — see request_page.
        None at once when the connected worker cannot fetch reels."""
        if KIND_REEL not in self._worker_kinds:
            return None
        return await self._request(
            (KIND_REEL, str(user_id)), username or str(user_id), timeout, fresh
        )

    # --------------------------------------------------- the worker side

    async def next_job(
        self,
        *,
        wait: float,
        worker: str,
        max_jobs: int = 1,
        kinds: Optional[Iterable[str]] = None,
    ) -> list[PageJob]:
        """Long-poll: up to `max_jobs` jobs the worker can do, or [] after
        `wait` seconds of nothing. Waits only for the first; the rest are
        whatever is queued. `kinds` is what this worker build fetches (pages
        only when unsaid); jobs of other kinds are left for a worker that can.

        Marks the worker as connected on the way in and again on the way out,
        so a job handed over just before the connected window would otherwise
        lapse keeps the worker counted as present.
        """
        self._last_poll = time.monotonic()
        self._worker = worker
        self._worker_kinds = frozenset(k for k in (kinds or (KIND_PAGE,)) if k in KINDS) or frozenset({KIND_PAGE})
        deadline = time.monotonic() + max(0.0, min(wait, POLL_WAIT_MAX_SECONDS))
        queue = self._get_queue()
        jobs: list[PageJob] = []
        limit = max(1, min(max_jobs, BATCH_MAX))
        passed_over: list[PageJob] = []
        seen: set[str] = set()
        try:
            while len(jobs) < limit:
                remaining = deadline - time.monotonic()
                if jobs:
                    # Already have one: take the rest without waiting.
                    try:
                        job = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                else:
                    if remaining <= 0:
                        break
                    try:
                        job = await asyncio.wait_for(queue.get(), remaining)
                    except asyncio.TimeoutError:
                        break
                if job.id in seen:
                    passed_over.append(job)  # cycled through everything left
                    break
                seen.add(job.id)
                if job.kind not in self._worker_kinds:
                    passed_over.append(job)
                    continue
                if self._take(job):
                    jobs.append(job)
        finally:
            for job in passed_over:
                queue.put_nowait(job)
        self._last_poll = time.monotonic()
        return jobs

    def deliver(self, job_id: str, result: PageResult) -> bool:
        """The worker's answer. Kept for the sweep even when no check is
        waiting for it any more (a prefetch, or a check that gave up but
        whose retry will ask again). False only for a job we never issued
        or already answered."""
        job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        key = (job.kind, job.key)
        if self._by_key.get(key) is job:
            self._by_key.pop(key, None)
        self._results[key] = result
        self.delivered += 1
        now = time.monotonic()
        pickup = (job.handed - job.created) if job.handed else 0.0
        deliver_seconds = (now - job.handed) if job.handed else (now - job.created)
        logger.info(
            "@{}: {} from the home fetcher — HTTP {}, {:.0f} KB; {:.1f}s until "
            "pickup, {:.1f}s to deliver{}",
            job.username, "reel data" if job.kind == KIND_REEL else "page",
            result.status, len(result.body) / 1024,
            pickup, deliver_seconds, " (prefetched)" if job.prefetch else "",
        )
        for future in self._waiters.pop(key, []):
            if not future.done():
                future.set_result(result)
        return True

    # ---------------------------------------------------------- internal

    def _cached(self, key: tuple[str, str]) -> Optional[PageResult]:
        result = self._results.get(key)
        if result is None:
            return None
        if time.monotonic() - result.fetched_at > RESULT_TTL_SECONDS:
            self._results.pop(key, None)
            return None
        return result

    def _prefetch(self, wanted: list[tuple[str, str, str]]) -> int:
        if not self.connected:
            return 0
        queue = self._get_queue()
        queued = 0
        for kind, key, username in wanted:
            if self._cached((kind, key)) is not None or (kind, key) in self._by_key:
                continue
            self._enqueue(queue, kind, key, username, prefetch=True)
            queued += 1
        return queued

    async def _request(
        self, key: tuple[str, str], username: str, timeout: float, fresh: bool
    ) -> Optional[PageResult]:
        if not fresh:
            hit = self._cached(key)
            if hit is not None:
                return hit
        if not self.connected:
            return None
        loop = asyncio.get_running_loop()
        if key not in self._by_key:
            self._enqueue(self._get_queue(), key[0], key[1], username, prefetch=False)
        future: asyncio.Future[PageResult] = loop.create_future()
        self._waiters.setdefault(key, []).append(future)
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self.timed_out += 1
            job = self._by_key.get(key)
            state = (
                "never picked up" if job is None or job.handed is None
                else f"picked up {time.monotonic() - job.handed:.0f}s ago, not delivered"
            )
            logger.info(
                "Home fetcher did not answer the {} for @{} within {:.0f}s ({})",
                key[0], username, timeout, state,
            )
            return None
        finally:
            waiters = self._waiters.get(key)
            if waiters:
                try:
                    waiters.remove(future)
                except ValueError:
                    pass
                if not waiters:
                    self._waiters.pop(key, None)

    def _enqueue(self, queue: asyncio.Queue[PageJob], kind: str, key: str,
                 username: str, *, prefetch: bool) -> PageJob:
        job = PageJob(id=uuid.uuid4().hex, username=username, kind=kind, key=key,
                      prefetch=prefetch)
        self._jobs[job.id] = job
        self._by_key[(kind, key)] = job
        queue.put_nowait(job)
        return job

    def _take(self, job: PageJob) -> bool:
        """Hand `job` out unless it was already answered or is stale."""
        if job.id not in self._jobs:
            return False
        if time.monotonic() - job.created > JOB_MAX_AGE_SECONDS:
            self._jobs.pop(job.id, None)
            if self._by_key.get((job.kind, job.key)) is job:
                self._by_key.pop((job.kind, job.key), None)
            return False
        job.handed = time.monotonic()
        return True

    def _get_queue(self) -> asyncio.Queue[PageJob]:
        # An asyncio.Queue binds to the loop that first uses it; a fresh loop
        # (tests, a restart of the server loop) gets a fresh queue. Pending
        # jobs belong to the old loop's checks and are already lost with it.
        loop = asyncio.get_running_loop()
        if self._queue is None or self._loop is not loop:
            self._queue = asyncio.Queue()
            self._loop = loop
            self._jobs.clear()
            self._by_key.clear()
        return self._queue


broker = HomeFetchBroker()
