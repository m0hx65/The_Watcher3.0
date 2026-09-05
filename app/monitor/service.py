"""High-level orchestration: fetch -> hash -> diff -> persist -> notify."""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.bot.notifications import (
    NotificationDispatcher,
    render_changes_message,
    render_digest,
    render_failure_message,
    render_highlight_catalog_changes,
    render_new_stories_alert,
    render_not_found_message,
    render_rename_message,
)
from app.config import settings
from app.database import crud
from app.database.models import AccountSnapshot, MonitoredAccount, ProfileMediaHash
from app.database.session import get_session
from app.monitor.analytics import classify_follower_change, render_follower_anomaly
from app.monitor.change_detector import (
    ChangeSet,
    detect_changes,
    pic_fingerprints_differ,
)
from app.monitor import home_fetch
from app.monitor.instagram import (
    IdProbe,
    InstagramClient,
    ProfileFetchResult,
    extract_instagram_id,
)
from app.monitor.media_hasher import (
    PHASH_PREFIX,
    HashedMedia,
    MediaHasher,
    pic_asset_id,
)
from app.monitor.stories import StoriesClient
from app.utils.formatting import esc, fmt_timestamp
from app.utils.logger import logger

# Shown when a story/highlight MEDIA download is requested but the anonymous
# source couldn't serve it this time (it's a third-party site that can rate-limit
# or briefly go down). The bot stays 100% login-free, so there's no cookie to
# fall back on. Highlight names and story/live status still work via graphql.
_DOWNLOAD_UNAVAILABLE_MSG = (
    "Couldn't retrieve the media right now — the anonymous source may be rate-"
    "limited or temporarily down. Try again shortly. Highlight names and story "
    "status still work."
)

# Seconds between sweep checks. Firing every account at once is the main 401
# trigger — Instagram rate-limits the proxy egress on bursts, and a blocked call
# retries inside the worker, snowballing the blocked traffic. The gap is counted
# from the END of a check (see _SweepThrottle.slot), so with the default
# concurrency of 1 the sweep produces the same request rhythm as a human
# pressing Recheck — the pattern Instagram answers reliably.
_SWEEP_STAGGER_SECONDS = 2.0
# First cooldown before re-checking accounts that hit a rate-limit block during
# the sweep; it doubles each round up to the max. Instagram's anonymous throttle
# windows are short, so a paced retry usually goes straight through.
_SWEEP_RETRY_COOLDOWN_SECONDS = 30.0
_SWEEP_RETRY_COOLDOWN_MAX_SECONDS = 120.0
# Gap between the individual re-checks inside a retry round, so the round keeps
# the unhurried one-at-a-time rhythm rather than replaying the burst that got
# these accounts blocked in the first place.
_SWEEP_RETRY_GAP_SECONDS = (2.0, 5.0)
# How many times one sweep may pause on the rate-limit guard before it gives up
# and defers the rest. A pause lets the throttle window clear and keeps the
# accounts in THIS sweep; only a block that survives every pause defers them.
_SWEEP_BREAKER_MAX_PAUSES = 2
# Fetch statuses worth another pass: rate-limit blocks and network timeouts.
# A 404 is a real answer (read against the numeric-id route in _do_check and
# surfaced by _handle_failure), not a block to retry.
_RETRIABLE_STATUSES = (401, 403, 429, 0)

# app_settings key holding when a sweep last found the username API refusing
# every lookup. Read by the next sweep (one knock instead of a threshold's
# worth) and by manual checks (skip it), cleared when a knock answers 200.
_USERNAME_API_DOOR_KEY = "username_api_closed_at"

# `record(api_status=...)` default: "not given" is not the same as None. None
# means the username API was deliberately not asked; not given means the
# caller only knows the overall status, which then stands for the API door.
_NOT_GIVEN: object = object()

# How many sweeps a private→public backlog grab may retry before giving up, so a
# genuinely empty (or permanently unreachable) account can't retry forever.
_PUBLIC_GRAB_MAX_ATTEMPTS = 3
# How many feed posts/reels one backlog grab lists — the same window the
# on-demand download-all panel uses.
_PUBLIC_GRAB_POST_LIMIT = 100


def _parse_utc(raw: Optional[str]) -> Optional[datetime]:
    """An ISO timestamp from app_settings as an aware UTC datetime, or None."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class _SweepThrottle:
    """Concurrency gate + adaptive pacing + a 401 guard for one sweep.

    This never touches a request — it only decides WHEN the next account may be
    checked, and whether to keep going at all.

    `concurrency` lanes run at a time (1 by default: one account at a time, the
    same shape as a manual Recheck). The gap between checks is stamped when a
    slot is RELEASED, so it is a real gap between requests rather than between
    launches — a burst of launches that all sit waiting on a semaphore was the
    old behavior and it hit Instagram as one wave.

    As consecutive 401/403 blocks accumulate the gap widens (soft backoff). Once
    `breaker_threshold` blocks happen in a row the response depends on whether
    ANY account has answered this sweep:

    - some have (a partial throttle): pause for `cooldown` seconds so the short
      window can clear, then carry on — those accounts stay in THIS sweep. Only
      after `max_pauses` cooldowns fail does the breaker open and defer the rest.
    - none have (`gate_down`): the breaker opens immediately. Nothing is getting
      through, so there is no pace that helps; every extra request is blocked
      traffic that keeps the gate shut. One worker call is 6 upstream attempts,
      so walking the remaining list costs hundreds of blocked requests to learn
      what the first few already said.

    The two routes a check uses are booked separately (2026-09-05). The
    username route can be shut — a 401 login wall — while the numeric-id
    route answers: then `breaker_threshold` refused username lookups in a row
    with none answering close THAT door for the sweep (`username_door_closed`;
    the rest go by id only), and a check counts as blocked only when neither
    route answered. `gate_down` means Instagram answered nothing at all.
    """

    def __init__(
        self,
        *,
        base_stagger: float,
        max_stagger: float,
        breaker_threshold: int,
        concurrency: int = 1,
        cooldown: float = 0.0,
        max_pauses: int = _SWEEP_BREAKER_MAX_PAUSES,
        username_door_threshold: Optional[int] = None,
    ) -> None:
        self._base = max(0.0, base_stagger)
        self._max = max(self._base, max_stagger)
        self._threshold = breaker_threshold  # 0 disables the guard
        # How many refused username lookups in a row (none answering) close
        # that door for the sweep. Defaults to the breaker threshold; a sweep
        # that already knows the door was shut last time passes 1 — one knock.
        self._user_threshold = (
            breaker_threshold if username_door_threshold is None
            else username_door_threshold
        )
        # Sweep-wide bookkeeping the per-account check needs: the whole list
        # (so the first refusal can hand it to the home fetcher up front), and
        # two once-per-sweep latches.
        self.sweep_usernames: list[str] = []
        self.pages_prefetched = False
        self.door_recorded = False
        self._cooldown = max(0.0, cooldown)  # 0 = open immediately, never pause
        self._max_pauses = max(0, max_pauses)
        self._extra = 0.0
        self._consecutive_auth_fails = 0
        self._peak_consecutive = 0
        self._open = False
        self._gate_down = False
        self._successes = 0
        self._skipped = 0
        self._pauses = 0
        # The username door (web_profile_info) is booked on its own. It can
        # be shut while the numeric-id route still answers — the state
        # measured 2026-09-05 — and then the right move is to stop asking it,
        # not to stop the sweep.
        self._user_successes = 0
        self._consecutive_user_blocks = 0
        self._peak_user_blocks = 0
        self._username_door_closed = False
        self._gate = asyncio.Semaphore(max(1, concurrency))
        self._lock = asyncio.Lock()
        self._next_slot = 0.0  # monotonic time the next check may proceed
        self._pause_until = 0.0  # monotonic time the guard's cooldown ends

    def is_open(self) -> bool:
        return self._open

    @property
    def tripped(self) -> bool:
        return self._open

    @property
    def skipped(self) -> int:
        return self._skipped

    @property
    def pauses(self) -> int:
        return self._pauses

    @property
    def gate_down(self) -> bool:
        """True when this sweep hit its block threshold without a single 200.

        That is a different failure from a throttle: nothing is getting through
        at all, so there is no pace slow enough to help and no account worth
        retrying — every further request is blocked traffic that keeps the gate
        shut. The sweep stops and waits for the next one.
        """
        return self._gate_down

    @property
    def peak_consecutive_blocks(self) -> int:
        return self._peak_consecutive

    @property
    def peak_consecutive_user_blocks(self) -> int:
        return self._peak_user_blocks

    @property
    def username_door_closed(self) -> bool:
        """True once `breaker_threshold` username lookups in a row were
        refused with none answering this sweep. From then on the remaining
        accounts are checked by numeric id only: that door is shut, and every
        further knock is blocked traffic for an answer already known."""
        return self._username_door_closed

    @property
    def answered(self) -> int:
        """Checks this sweep where Instagram answered on at least one route."""
        return self._successes

    @property
    def username_door_answered(self) -> bool:
        """True once the username API itself answered 200 this sweep."""
        return self._user_successes > 0

    @property
    def username_door_threshold(self) -> int:
        return self._user_threshold

    @property
    def current_stagger(self) -> float:
        return min(self._max, self._base + self._extra)

    def note_skip(self) -> None:
        self._skipped += 1

    def record(
        self,
        status: Optional[int],
        *,
        id_status: Optional[int] = None,
        api_status: Any = _NOT_GIVEN,
    ) -> None:
        """Book one check. `status` is the username side's overall answer
        (the API or, failing that, a page door — None when nothing on that
        side was asked); `id_status` the numeric-id route's (None when the
        account has no stored id); `api_status` what the username API ITSELF
        said (None when it was skipped), so a page that answered after the
        API refused still counts as the API door being shut.

        A check is BLOCKED only when nothing answered anywhere. A check where
        any route answered is a success for pacing and for the gate. The gate
        is down when Instagram answers NOTHING — not when one of its doors is
        shut, which since 2026-09-05 is the normal state of the username API.
        """
        door = status if api_status is _NOT_GIVEN else api_status
        user_blocked = door in (401, 403)
        user_ok = door == 200
        # A 404 by id is an answer (the id is gone), not a block.
        id_answered = id_status in (200, 404)

        if user_ok:
            self._user_successes += 1
            self._consecutive_user_blocks = 0
        elif user_blocked:
            self._consecutive_user_blocks += 1
            self._peak_user_blocks = max(
                self._peak_user_blocks, self._consecutive_user_blocks
            )
            if (
                self._user_threshold
                and not self._username_door_closed
                and self._user_successes == 0
                and self._consecutive_user_blocks >= self._user_threshold
            ):
                self._username_door_closed = True
                logger.warning(
                    "Instagram refused {} username lookups in a row and "
                    "answered none — checking the rest of the sweep by "
                    "numeric id only", self._consecutive_user_blocks,
                )

        answered = user_ok or id_answered
        asked = status is not None or id_status is not None
        blocked = (
            asked
            and not answered
            and (user_blocked or id_status in (401, 403))
        )

        if answered:
            self._successes += 1
            self._consecutive_auth_fails = 0
            # Relax gradually back toward the base stagger on success.
            self._extra = max(0.0, self._extra - self._base)
        elif blocked:
            self._consecutive_auth_fails += 1
            self._peak_consecutive = max(
                self._peak_consecutive, self._consecutive_auth_fails
            )
            # Widen the gap by one base step per consecutive block.
            self._extra = min(self._max - self._base, self._extra + self._base)
            if self._threshold and self._consecutive_auth_fails >= self._threshold:
                streak = self._consecutive_auth_fails
                # A fresh streak starts after the pause — the cooldown is the
                # remedy being tried, so it deserves its own window to work.
                self._consecutive_auth_fails = 0
                if self._successes == 0:
                    # Not one account has answered on any route. This is the
                    # gate being shut to us, not a pace we can tune our way
                    # out of — stop now rather than spend the rest of the
                    # sweep proving it.
                    self._gate_down = True
                    self._open = True
                    logger.warning(
                        "Instagram's gate is blocking every request ({} in a "
                        "row, 0 answered) — stopping the sweep instead of "
                        "walking the rest of the list", streak,
                    )
                elif self._cooldown > 0 and self._pauses < self._max_pauses:
                    self._pauses += 1
                    # A deadline, not a flag: every lane waits it out, so the
                    # pause is a real gap in outbound traffic rather than one
                    # delayed check with the others sliding past it.
                    self._pause_until = time.monotonic() + self._cooldown
                    logger.warning(
                        "Rate-limit guard: pausing the sweep for {:.0f}s after "
                        "{} blocks in a row (pause {}/{})",
                        self._cooldown, streak, self._pauses, self._max_pauses,
                    )
                else:
                    self._open = True
        # A 404/429/0 on the username route with nothing else asked leaves
        # pacing unchanged — that isn't the datacenter block.

    @contextlib.asynccontextmanager
    async def slot(self):
        """Hold one sweep lane for the duration of a check.

        Waits for a free lane, then for the pacing gap left by the previous
        check (and for any rate-limit cooldown still running), and stamps the
        next gap on the way out so the spacing is measured between requests,
        not launches.
        """
        async with self._gate:
            async with self._lock:
                now = time.monotonic()
                wait = max(0.0, self._next_slot - now, self._pause_until - now)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                yield
            finally:
                async with self._lock:
                    self._next_slot = (
                        time.monotonic()
                        + self.current_stagger
                        + random.uniform(0.0, 0.8)
                    )


class MonitorService:
    """Coordinates a single account check or a fan-out across all accounts."""

    def __init__(
        self,
        instagram: InstagramClient,
        hasher: MediaHasher,
        notifier: NotificationDispatcher,
        stories: Optional[StoriesClient] = None,
    ) -> None:
        self.instagram = instagram
        self.hasher = hasher
        self.notifier = notifier
        self.stories = stories
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_fetches)
        # When a sweep last found the username API refusing every lookup
        # (None = it was answering). Loaded from app_settings on first use so
        # the memory survives a restart; see username_api_known_closed().
        self._username_api_closed_at: Optional[datetime] = None
        self._username_api_door_loaded = False
        # account_id -> forum topic (message_thread_id). Resolved lazily and
        # cached so each account's alerts land in its own thread.
        self._topic_cache: dict[int, int] = {}
        # Latched True once topic creation fails (chat isn't a forum / no
        # manage-topics right) so we don't re-attempt on every message.
        self._topics_unavailable: bool = False
        # /kill cooperative cancellation for ON-DEMAND downloads. A huge
        # highlight grab can take minutes; _download_cancel lets the user abort
        # it mid-flight. _active_downloads counts the on-demand operations
        # currently running so /kill knows whether there's anything to stop
        # (and so a sweep's auto-download is never affected). The delivery and
        # gather loops poll the event between items, so already-sent media stays
        # and nothing is left half-written.
        self._active_downloads: int = 0
        self._download_cancel = asyncio.Event()
        # Accounts whose private→public backlog grab is running right now. A
        # manual Recheck landing mid-sweep (or two Rechecks back to back) must
        # not start a second grab of the same account: both would load the
        # seen-set before either had marked anything, and the chat would get
        # every item twice.
        self._public_grabs_in_flight: set[int] = set()

    # ---------- On-demand download cancellation (/kill) ----------

    @contextlib.asynccontextmanager
    async def download_scope(self):
        """Mark an on-demand download as active for the duration of the block.

        Nests safely (the bundle download wraps several inner downloads in one
        outer scope): the cancel flag is cleared when the FIRST scope opens — so
        a stale /kill from a finished download can't abort a fresh one — and
        again when the LAST scope closes. While any scope is open, /kill knows a
        download is running and the loops honor the cancel signal.
        """
        if self._active_downloads == 0:
            self._download_cancel.clear()
        self._active_downloads += 1
        try:
            yield
        finally:
            self._active_downloads -= 1
            if self._active_downloads <= 0:
                self._active_downloads = 0
                self._download_cancel.clear()

    def request_kill(self) -> bool:
        """Signal every in-flight on-demand download to stop. Returns True when
        something was actually running (so the caller can say so)."""
        if self._active_downloads > 0:
            self._download_cancel.set()
            logger.info(
                "/kill — cancelling {} active download op(s)", self._active_downloads
            )
            return True
        return False

    @property
    def download_active(self) -> bool:
        return self._active_downloads > 0

    def is_cancelling(self) -> bool:
        """True once /kill has been requested while a download is running."""
        return self._download_cancel.is_set()

    async def topic_for(
        self, account_id: Optional[int], username: str
    ) -> Optional[int]:
        """Resolve the forum topic id for an account, creating it on first use.

        Returns None — meaning post to the General thread — when forum topics
        are disabled, the account isn't monitored, or the chat isn't a forum.
        One topic per account: results are cached and persisted, and a single
        creation failure latches the feature off for this process so a non-forum
        chat never gets hammered with create attempts.
        """
        if (
            not settings.telegram_forum_topics
            or account_id is None
            or self._topics_unavailable
        ):
            return None
        if account_id in self._topic_cache:
            return self._topic_cache[account_id]
        async with get_session() as session:
            stored = await crud.get_account_topic(session, account_id)
        if stored is not None:
            self._topic_cache[account_id] = stored
            return stored
        thread = await self.notifier.create_forum_topic(f"@{username}")
        if thread is None:
            self._topics_unavailable = True
            logger.info(
                "Forum topics unavailable (chat isn't a forum or bot lacks "
                "manage-topics) — routing everything to General."
            )
            return None
        async with get_session() as session:
            await crud.set_account_topic(session, account_id, thread)
        self._topic_cache[account_id] = thread
        logger.info("Created forum topic for @{} (thread {})", username, thread)
        return thread

    async def sync_topics(self) -> dict:
        """Create a forum topic for every monitored account that lacks one.

        Backfills all existing accounts at once (including private ones, which
        otherwise only get a topic the first time they change). Returns
        {ok, created, existing, error}."""
        if not settings.telegram_forum_topics:
            return {
                "ok": False, "created": 0, "existing": 0,
                "error": "Forum topics are off — set TELEGRAM_FORUM_TOPICS=true and redeploy.",
            }
        # Let an explicit sync retry even if a prior auto-attempt latched off.
        self._topics_unavailable = False
        async with get_session() as session:
            accounts = await crud.list_accounts(session, only_active=False)
        created = 0
        existing = 0
        for account in accounts:
            async with get_session() as session:
                stored = await crud.get_account_topic(session, account.id)
            if stored is not None:
                self._topic_cache[account.id] = stored
                existing += 1
                continue
            thread = await self.topic_for(account.id, account.username)
            if thread is None:
                return {
                    "ok": False, "created": created, "existing": existing,
                    "error": (
                        "Couldn't create a topic — make sure the chat is a forum "
                        "(Topics enabled) and the bot is an admin with the "
                        "'Manage topics' right."
                    ),
                }
            created += 1
        return {"ok": True, "created": created, "existing": existing, "error": None}

    @property
    def username_api_closed_since(self) -> Optional[datetime]:
        """When the username API was last found shut, or None. Loaded lazily —
        None before the first check of the process may simply mean unread."""
        return self._username_api_closed_at

    async def username_api_known_closed(self) -> bool:
        """True while the last sweep's verdict — the username API refused
        every lookup — is recent enough to trust (USERNAME_API_RECHECK_SECONDS).

        The verdict is what turns a fifty-second wait at the start of every
        sweep and a thirty-second wait on every manual check into nothing:
        the door is knocked once per sweep and left alone otherwise, until
        the knock answers.
        """
        if not self._username_api_door_loaded:
            try:
                async with get_session() as session:
                    raw = await crud.get_setting(session, _USERNAME_API_DOOR_KEY)
            except Exception as exc:  # a DB hiccup must not stop a check
                logger.warning("Could not read the username API verdict: {}", exc)
                raw = None
            self._username_api_closed_at = _parse_utc(raw)
            self._username_api_door_loaded = True
        closed_at = self._username_api_closed_at
        if closed_at is None:
            return False
        age = datetime.now(timezone.utc) - closed_at
        return age < timedelta(seconds=max(0, settings.username_api_recheck_seconds))

    async def _remember_username_api_door(
        self, *, closed: bool, answered: bool
    ) -> None:
        """Persist a sweep's verdict on the username API: shut (refreshes the
        timestamp) or answering (forgets it). A sweep that neither closed the
        door nor got an answer from it — nothing asked — leaves it as it was."""
        self._username_api_door_loaded = True
        try:
            if closed:
                now = datetime.now(timezone.utc)
                self._username_api_closed_at = now
                async with get_session() as session:
                    await crud.set_setting(
                        session, _USERNAME_API_DOOR_KEY, now.isoformat()
                    )
                return
            if answered and self._username_api_closed_at is not None:
                self._username_api_closed_at = None
                async with get_session() as session:
                    await crud.delete_setting(session, _USERNAME_API_DOOR_KEY)
                logger.info("The username API answered again — the door is open")
        except Exception as exc:  # the in-memory verdict still stands
            logger.warning("Could not persist the username API verdict: {}", exc)

    async def check_username(
        self, username: str, *, notify_unchanged: bool = False
    ) -> dict:
        """Run one FULL check by username. Returns a summary dict.

        Covers exactly what a scheduled sweep covers: the profile diff +
        new-post/reel delivery (inside _run_check), then the same
        story/highlight phase check_all runs — story & live status, highlight
        catalog diff, and new story/highlight media delivery. A manual
        Recheck (button, /recheck, REST) must never see less than the sweep.
        """
        username = username.strip().lstrip("@").lower()
        async with get_session() as session:
            account = await crud.get_account(session, username)
            if account is None:
                return {"ok": False, "error": f"@{username} is not monitored"}
            account_id = account.id

        # A door the last sweep found shut is not knocked on here: someone is
        # waiting, and each knock is a ten-second Worker call for a known
        # answer. The sweep re-tests it once per pass.
        result = await self._run_check(
            account_id, username, notify_unchanged=notify_unchanged,
            skip_username_api=await self.username_api_known_closed(),
        )

        if self.stories is not None and result.get("ok"):
            meta = await self._load_account_story_meta(account_id)
            is_private = result.get("is_private")
            if is_private is None:
                is_private = meta["is_private"]
            if not is_private:
                result_username = result.get("username", username)
                instagram_id = result.get("instagram_id") or meta["instagram_id"]
                # A private→public flip (or a pending backlog grab) takes over
                # this account's media for the sweep — the full grab replaces the
                # normal story phase, which would otherwise silently baseline and
                # lose the backlog.
                handled = await self._handle_public_backlog(
                    account_id,
                    result_username,
                    instagram_id,
                    went_public=bool(result.get("went_public")),
                )
                if not handled:
                    await self._check_stories_and_highlights(
                        account_id,
                        result_username,
                        instagram_id=instagram_id,
                        reel_data=result.get("reel_data"),
                        always_report=True,
                    )
        return result

    async def backfill_instagram_ids(self) -> dict:
        """Resolve and store instagram_id for accounts that do not have one yet."""
        async with get_session() as session:
            accounts = await crud.list_accounts(session, only_active=True)
            missing = [a for a in accounts if not a.instagram_id]

        if not missing:
            return {
                "attempted": 0,
                "resolved": 0,
                "from_snapshot": 0,
                "from_reel_query": 0,
                "from_stories_api": 0,
                "from_fetch": 0,
                "failed": 0,
            }

        resolved = 0
        from_snapshot = 0
        from_reel_query = 0
        from_stories_api = 0
        from_fetch = 0
        failed = 0

        for account in missing:
            instagram_id: Optional[str] = None
            resolved_username: Optional[str] = None

            async with get_session() as session:
                current = await session.get(MonitoredAccount, account.id)
                if current is None or current.instagram_id:
                    continue
                snapshot = await crud.get_latest_snapshot(
                    session, account.id, successful_only=False
                )
                instagram_id = self._extract_instagram_id(
                    snapshot.raw_response if snapshot else None
                )
                if instagram_id:
                    current.instagram_id = instagram_id
                    from_snapshot += 1
                    resolved += 1
                    logger.info(
                        "Backfilled Instagram ID for @{} from snapshot: {}",
                        current.username,
                        instagram_id,
                    )
                    continue

            if self.stories is not None:
                async with self._semaphore:
                    pk = await self.stories.resolve_user_id(account.username)
                if pk:
                    async with self._semaphore:
                        reel_user = await self.instagram.fetch_reel_user(str(pk))
                    if reel_user:
                        instagram_id = reel_user.get("instagram_id") or str(pk)
                        resolved_username = reel_user.get("username")
                    else:
                        instagram_id = str(pk)
                    async with get_session() as session:
                        current = await session.get(MonitoredAccount, account.id)
                        if current is not None and not current.instagram_id:
                            current.instagram_id = instagram_id
                            if resolved_username and resolved_username != current.username:
                                existing = await crud.get_account(
                                    session, resolved_username
                                )
                                if existing is None or existing.id == current.id:
                                    current.username = resolved_username
                            from_stories_api += 1
                            if reel_user:
                                from_reel_query += 1
                            resolved += 1
                            logger.info(
                                "Backfilled Instagram ID for @{} via stories/reel query: {}",
                                current.username,
                                instagram_id,
                            )
                    continue

            async with self._semaphore:
                fetch = await self.instagram.fetch_profile(account.username)

            if fetch.success and fetch.parsed:
                instagram_id = fetch.parsed.get("instagram_id")
            if not instagram_id:
                instagram_id = self._extract_instagram_id(fetch.raw_response)

            if instagram_id:
                async with get_session() as session:
                    current = await session.get(MonitoredAccount, account.id)
                    if current is not None and not current.instagram_id:
                        current.instagram_id = str(instagram_id)
                        from_fetch += 1
                        resolved += 1
                        logger.info(
                            "Backfilled Instagram ID for @{} from profile fetch: {}",
                            current.username,
                            instagram_id,
                        )
                continue

            failed += 1
            logger.warning(
                "Could not backfill Instagram ID for @{}", account.username
            )

        return {
            "attempted": len(missing),
            "resolved": resolved,
            "from_snapshot": from_snapshot,
            "from_reel_query": from_reel_query,
            "from_stories_api": from_stories_api,
            "from_fetch": from_fetch,
            "failed": failed,
        }

    async def check_all(self, *, backfill_ids: bool = False) -> dict:
        """Fan out checks across all active accounts."""
        id_backfill: Optional[dict] = None
        if backfill_ids:
            id_backfill = await self.backfill_instagram_ids()
            if id_backfill["resolved"]:
                logger.info(
                    "Instagram ID backfill before sweep: resolved={} (snapshot={}, fetch={})",
                    id_backfill["resolved"],
                    id_backfill["from_snapshot"],
                    id_backfill["from_fetch"],
                )

        async with get_session() as session:
            accounts = await crud.list_accounts(session, only_active=True)
            targets = [(a.id, a.username) for a in accounts]

        if not targets:
            logger.info("No active accounts to check.")
            result: dict = {"checked": 0, "changed": 0, "failed": 0}
            if id_backfill is not None:
                result["id_backfill"] = id_backfill
            return result

        logger.info("Starting scheduled sweep across {} accounts", len(targets))
        # Randomize the order every sweep. Instagram's throttle hits whoever is
        # in the window when it closes, so a fixed order made the same tail of
        # accounts absorb every block sweep after sweep.
        random.shuffle(targets)
        noun = "profile" if len(targets) == 1 else "profiles"
        await self.notifier.send_text(
            f"👁 Sweep started — {len(targets)} {noun} queued."
        )
        # One knock per sweep on a door the last sweep found shut: it reopens
        # the moment the API answers, and costs ten seconds a sweep instead of
        # a threshold's worth of blocked Worker calls.
        known_closed = await self.username_api_known_closed()
        throttle = _SweepThrottle(
            base_stagger=_SWEEP_STAGGER_SECONDS,
            max_stagger=settings.sweep_stagger_max_seconds,
            breaker_threshold=settings.sweep_breaker_threshold,
            concurrency=settings.sweep_concurrency,
            cooldown=settings.sweep_breaker_cooldown_seconds,
            username_door_threshold=1 if known_closed else None,
        )
        throttle.sweep_usernames = [uname for _, uname in targets]
        if known_closed:
            # Every account will need its page: hand the phone the whole list
            # now, so its round trips overlap the sweep instead of gating
            # each check. (When the verdict is not yet known, the first
            # refusal does the same — see _staggered_check.)
            throttle.pages_prefetched = True
            self._prefetch_pages(throttle.sweep_usernames)
        results = await asyncio.gather(
            *(
                self._staggered_check(throttle, aid, uname)
                for aid, uname in targets
            ),
            return_exceptions=True,
        )
        if throttle.tripped:
            logger.warning(
                "Sweep circuit breaker tripped after {} consecutive 401/403s — "
                "{} account(s) deferred to the retry pass / next sweep",
                throttle.peak_consecutive_blocks, throttle.skipped,
            )
        await self._remember_username_api_door(
            closed=throttle.username_door_closed,
            answered=throttle.username_door_answered,
        )

        # account_id -> (fallback username, result dict). Exceptions become
        # failure dicts (flagged "crashed") so the retry pass can rewrite any
        # entry and the final stats fall out of one structure.
        outcomes: list[tuple[int, str, dict]] = []
        for (target_account_id, uname), r in zip(targets, results):
            if isinstance(r, Exception):
                logger.exception("Unhandled error during sweep: {}", r)
                r = {"ok": False, "username": uname, "error": repr(r), "crashed": True}
            outcomes.append((target_account_id, uname, r))

        # Second pass: accounts that hit a rate-limit block get several more
        # chances, each after a longer cooldown. The block is transient — a
        # paced sequential retry usually succeeds, so the sweep summary stops
        # reporting failures the owner can't reproduce by hand. This runs BEFORE
        # the story phase so a recovered account's story status comes from its
        # fresh reel data; when it ran after, the sweep could announce "story
        # status unavailable" for an account the very same sweep went on to
        # report as checked.
        #
        # Skipped outright when the gate is down: with nothing getting through,
        # a retry round is 200+ more blocked requests that deny Instagram the
        # very quiet it needs to let us back in. The next sweep is the retry.
        #
        # With the username door shut the rounds still run, by id only: the
        # id route is refused per colo too (the last three accounts of a sweep
        # were, back to back), and a paced re-ask from a different colo is the
        # cheapest recovery there is — one Worker call each.
        recovered = (
            0 if throttle.gate_down
            else await self._retry_blocked(
                outcomes, skip_username_api=throttle.username_door_closed
            )
        )

        # One batched read of every pending backlog-grab flag, so the per-account
        # decision below costs no extra query on the hot path.
        async with get_session() as session:
            public_grab_flags = await crud.get_settings_by_prefix(
                session, "public_grab_pending:"
            )

        # (account_id, username, instagram_id, this check's reel_data or None)
        story_targets: list[tuple[int, str, Optional[str], Optional[dict]]] = []
        # Accounts that flipped private→public (or have a pending grab): each
        # gets its whole backlog delivered instead of the normal story phase.
        public_grab_targets: list[tuple[int, str, Optional[str], bool]] = []
        for target_account_id, uname, r in outcomes:
            if r.get("crashed"):
                continue
            result_username = r.get("username", uname)
            # A successful check already knows privacy and the numeric id —
            # only fall back to the two-query DB lookup when the result
            # doesn't (failed fetches), instead of paying it for every
            # account on every sweep.
            is_private = r.get("is_private")
            instagram_id = r.get("instagram_id")
            if is_private is None or not instagram_id:
                meta = await self._load_account_story_meta(target_account_id)
                if is_private is None:
                    is_private = meta["is_private"]
                instagram_id = instagram_id or meta["instagram_id"]
            is_private = bool(is_private)
            if is_private:
                continue
            went_public = bool(r.get("went_public"))
            has_pending = (
                self._public_grab_key(target_account_id) in public_grab_flags
            )
            if (
                self.stories is not None
                and settings.auto_grab_on_public
                and (went_public or has_pending)
            ):
                public_grab_targets.append(
                    (target_account_id, result_username, instagram_id, went_public)
                )
            else:
                story_targets.append(
                    (target_account_id, result_username, instagram_id,
                     r.get("reel_data"))
                )

        if self.stories is not None and story_targets:
            # With the gate down, the per-account fallback reel query is 8 more
            # blocked upstream attempts each for an answer we already know we
            # can't get. Stories still run: saveinsta is a different source and
            # is usually up when Instagram's own gate isn't, so media keeps
            # flowing — only the live status goes unknown, and it says so.
            await asyncio.gather(
                *(
                    self._check_stories_and_highlights(
                        aid, uname, instagram_id=ig_id, reel_data=reel,
                        skip_reel_fallback=throttle.gate_down,
                    )
                    for aid, uname, ig_id, reel in story_targets
                ),
                return_exceptions=True,
            )

        # Backlog grabs are heavy (a whole account each) — run them sequentially
        # after the story phase so they never flood the chat or the source all at
        # once. Rare in practice (only on a private→public flip).
        for aid, uname, ig_id, went_public in public_grab_targets:
            try:
                await self._handle_public_backlog(
                    aid, uname, ig_id, went_public=went_public
                )
            except Exception as exc:  # pragma: no cover - never sink a sweep on this
                logger.exception(
                    "Public backlog grab failed for @{}: {}", uname, exc
                )

        # A deferred (breaker-skipped) account was never checked — it used to
        # be counted here, which is how a sweep that stopped after 5 accounts
        # reported "17 profiles did get through" over a list of 17 failures.
        checked = sum(
            1 for _, _, r in outcomes
            if not r.get("crashed") and not r.get("skipped")
        )
        answered = sum(1 for _, _, r in outcomes if r.get("ok"))
        id_only = sum(
            1 for _, _, r in outcomes
            if r.get("ok") and r.get("partial") == "id_probe"
        )
        page_only = sum(
            1 for _, _, r in outcomes
            if r.get("ok") and r.get("partial") == "public_page"
        )
        deferred = sum(1 for _, _, r in outcomes if r.get("skipped"))
        changed = sum(1 for _, _, r in outcomes if r.get("changed"))
        failed_usernames = [
            r.get("username", uname)
            for _, uname, r in outcomes
            if not r.get("ok") and not r.get("skipped")
        ]
        failed = len(failed_usernames)

        logger.info(
            "Sweep done: checked={}, answered={}, id_only={}, page_only={}, "
            "changed={}, failed={}, deferred={}",
            checked, answered, id_only, page_only, changed, failed, deferred,
        )

        # Went-dark radar: flag targets that have posted nothing for a while.
        # Runs after the story phase so this sweep's fresh activity is counted.
        try:
            await self._check_dark_radar()
        except Exception as exc:  # pragma: no cover - never sink a sweep on this
            logger.exception("Dark-radar check failed: {}", exc)

        noun = "profile" if checked == 1 else "profiles"
        if throttle.gate_down:
            # Naming 13 accounts implies 13 separate problems. There was one:
            # Instagram answered nothing, so no per-account detail is real.
            summary = (
                "👁 Sweep stopped — Instagram is blocking every request right "
                f"now.\n🚫 {throttle.peak_consecutive_blocks} checks in a row "
                "came back blocked on every route and none succeeded, so the "
                f"sweep stopped early and left {deferred} account(s) "
                "unchecked instead of hammering a shut door."
            )
            if answered:
                answered_noun = "profile" if answered == 1 else "profiles"
                summary += (
                    f"\n✅ {answered} {answered_noun} did get through before that."
                )
            summary += "\nNothing to do — the next sweep tries again."
        else:
            summary = f"👁 Sweep complete — {checked} {noun} checked."
            if recovered:
                summary += f" {recovered} recovered on retry."
            if failed:
                names = ", ".join(f"@{u}" for u in sorted(failed_usernames))
                summary += f" {failed} failed: {names}"
            if page_only:
                summary += (
                    f"\n📄 {page_only} read from the profile page — followers, "
                    "following, bio and privacy are live; reel and highlight "
                    "counts carried forward."
                )
            if id_only:
                summary += (
                    f"\n🪪 {id_only} checked by Instagram ID only — username, "
                    "picture and story status are live; followers, bio and "
                    "counts couldn't be read this time and were not guessed."
                )
            if throttle.username_door_closed and known_closed:
                summary += (
                    "\n🚪 Instagram's profile API is still refusing username "
                    "lookups (checked once this sweep), so the sweep used the "
                    "ID route and the profile page."
                )
            elif throttle.username_door_closed:
                summary += (
                    "\n🚪 Instagram's profile API refused every username "
                    f"lookup ({throttle.peak_consecutive_user_blocks} in a "
                    "row), so the rest of the sweep skipped it and used the "
                    "ID route and the profile page."
                )
            elif known_closed and throttle.username_door_answered:
                summary += (
                    "\n🔓 Instagram's profile API is answering username "
                    "lookups again — full readings are back."
                )
            if throttle.pauses:
                summary += (
                    f"\n⏸ Paused {throttle.pauses}× mid-sweep to let Instagram's "
                    f"rate-limit window clear ({throttle.peak_consecutive_blocks} "
                    "blocks in a row)."
                )
            if throttle.tripped:
                summary += (
                    f"\n⚡ Rate-limit guard tripped after "
                    f"{throttle.peak_consecutive_blocks} blocks in a row — "
                    f"{deferred} account(s) deferred to avoid making it "
                    "worse. They'll retry shortly / next sweep."
                )
        if backfill_ids:
            async with get_session() as session:
                accounts_after = await crud.list_accounts(session, only_active=True)
                still_missing = sum(1 for a in accounts_after if not a.instagram_id)
            pre_resolved = id_backfill["resolved"] if id_backfill else 0
            if pre_resolved:
                summary += (
                    f"\n{pre_resolved} Instagram ID"
                    f"{'s' if pre_resolved != 1 else ''} backfilled before sweep"
                )
            if still_missing:
                summary += (
                    f"\n{still_missing} account"
                    f"{'s' if still_missing != 1 else ''} still missing an ID"
                )
        await self.notifier.send_text(summary)

        result = {
            "checked": checked,
            "changed": changed,
            "failed": failed,
            "answered": answered,
            "id_only": id_only,
            "page_only": page_only,
            "deferred": deferred,
        }
        if id_backfill is not None:
            result["id_backfill"] = id_backfill
        return result

    # ---------- Went-dark radar ----------

    @staticmethod
    def _dark_state_key(account_id: int) -> str:
        return f"dark_state:{account_id}"

    @staticmethod
    def _humanize_silence(delta: timedelta) -> str:
        days = delta.days
        if days >= 1:
            return f"{days} day{'s' if days != 1 else ''}"
        hours = delta.seconds // 3600
        if hours >= 1:
            return f"{hours} hour{'s' if hours != 1 else ''}"
        minutes = max(1, delta.seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"

    async def _check_dark_radar(self) -> None:
        """Alert when a monitored account goes quiet, and when it returns.

        "Activity" = a delivered story, post, or highlight (seen_stories).
        Accounts with no activity on record yet are skipped — there's no
        baseline to call them dark from. State is one app_settings flag per
        account so an account is announced dark/back exactly once per spell.
        """
        threshold_days = settings.dark_radar_days
        if threshold_days <= 0:
            return
        threshold = timedelta(days=threshold_days)
        now = datetime.now(timezone.utc)

        # Three batched queries for the whole radar instead of two per account
        # per sweep — activity times and dark flags come back as maps.
        async with get_session() as session:
            accounts = await crud.list_accounts(session, only_active=True)
            activity = await crud.last_activity_map(session)
            flagged_keys = await crud.get_settings_by_prefix(session, "dark_state:")

        for account in accounts:
            last = activity.get(account.id)
            state_key = self._dark_state_key(account.id)
            currently_flagged = state_key in flagged_keys
            if last is None:
                continue  # no activity baseline — can't judge
            silent = now - last
            if silent >= threshold and not currently_flagged:
                async with get_session() as session:
                    await crud.set_setting(session, state_key, last.isoformat())
                msg = (
                    f"🌑 <b>@{esc(account.username)}</b> has gone dark — no new "
                    f"story, post, or reel in <b>{self._humanize_silence(silent)}</b>.\n"
                    f"Last activity: <code>{fmt_timestamp(last)}</code>"
                )
                thread_id = await self.topic_for(account.id, account.username)
                delivered = await self.notifier.send_text(msg, message_thread_id=thread_id)
                async with get_session() as session:
                    await crud.log_notification(
                        session,
                        account_id=account.id,
                        change_type="went_dark",
                        payload={"last_activity": last.isoformat(),
                                 "silent_seconds": int(silent.total_seconds())},
                        message=msg,
                        delivered=delivered,
                    )
            elif silent < threshold and currently_flagged:
                async with get_session() as session:
                    await crud.delete_setting(session, state_key)
                msg = (
                    f"☀️ <b>@{esc(account.username)}</b> is active again — "
                    "posted after a quiet spell."
                )
                thread_id = await self.topic_for(account.id, account.username)
                delivered = await self.notifier.send_text(msg, message_thread_id=thread_id)
                async with get_session() as session:
                    await crud.log_notification(
                        session,
                        account_id=account.id,
                        change_type="back_active",
                        payload={"last_activity": last.isoformat()},
                        message=msg,
                        delivered=delivered,
                    )

    async def dark_radar_report(self) -> dict:
        """On-demand snapshot of silence per monitored account, quietest first.

        Returns {"threshold_days", "accounts": [{username, last, silent_days,
        dark, never}]}. `never` marks accounts with no activity on record.
        """
        now = datetime.now(timezone.utc)
        async with get_session() as session:
            accounts = await crud.list_accounts(session, only_active=True)
            activity = await crud.last_activity_map(session)
        rows = [(a.username, activity.get(a.id)) for a in accounts]
        threshold_days = settings.dark_radar_days
        report = []
        for username, last in rows:
            if last is None:
                report.append({
                    "username": username, "last": None,
                    "silent_days": None, "dark": False, "never": True,
                })
                continue
            silent = now - last
            report.append({
                "username": username,
                "last": last,
                "silent_days": silent.days,
                "silent": silent,
                "dark": threshold_days > 0 and silent >= timedelta(days=threshold_days),
                "never": False,
            })
        # Quietest first; "never seen" accounts sort to the end.
        report.sort(
            key=lambda r: (r["never"], -(r["silent_days"] or 0)),
        )
        return {"threshold_days": threshold_days, "accounts": report}

    async def compose_digest(self, since: datetime) -> tuple[str, int, int]:
        """Build the digest text for everything logged since `since`.

        Reads the already-stored NotificationLog (no extra tracking) and rolls it
        up per account. Returns (text, event_count, account_count) so callers can
        log/announce what went out.
        """
        async with get_session() as session:
            rows = await crud.notifications_since(session, since)
        text = render_digest(rows, since=since)
        accounts = len({username for _, username in rows})
        return text, len(rows), accounts

    @staticmethod
    def _prefetch_pages(usernames: list[str]) -> None:
        """Ask the home fetcher for every page a sweep will need, up front."""
        if not settings.home_fetch_token:
            return
        queued = home_fetch.broker.prefetch(usernames)
        if queued:
            logger.info(
                "Handed the home fetcher {} profile page(s) up front", queued
            )
        elif not home_fetch.broker.connected:
            logger.info(
                "Home fetcher {} — pages will be asked for per check",
                home_fetch.broker.describe(),
            )

    @staticmethod
    def _breaker_skipped_result(username: str) -> dict:
        """A deferred account looks like a retriable failure, so the existing
        retry pass (sequential, after a cooldown) or the next sweep picks it up
        without any special-casing downstream."""
        return {
            "ok": False,
            "username": username,
            "status": 401,
            "error": "deferred — sweep circuit breaker open (too many 401s)",
            "skipped": True,
        }

    async def _staggered_check(
        self, throttle: "_SweepThrottle", account_id: int, username: str
    ) -> dict:
        """Run one sweep check inside a slot of the shared throttle.

        The throttle limits how many checks run at once, spaces them (widening
        the gap as 401s accumulate, pausing outright on a run of blocks) and,
        once the breaker is open, skips the remaining accounts so a burst never
        snowballs — bursts are what trip Instagram's anonymous rate limiter into
        401s on the shared proxy egress. The request itself is unchanged.
        """
        if throttle.is_open():
            throttle.note_skip()
            return self._breaker_skipped_result(username)
        async with throttle.slot():
            if throttle.is_open():  # tripped while this one waited its turn
                throttle.note_skip()
                return self._breaker_skipped_result(username)
            result = await self._run_check(
                account_id, username, thorough=False,
                skip_username_api=throttle.username_door_closed,
            )
            # Recorded inside the slot so the next account's pacing — and any
            # cooldown this block just triggered — already accounts for it.
            throttle.record(
                result.get("status"),
                id_status=result.get("id_status"),
                api_status=result.get("api_status", _NOT_GIVEN),
            )
            # The first refusal of the username API is the moment to hand the
            # home fetcher the rest of the list: every account after this one
            # will need its page.
            if not throttle.pages_prefetched and (
                throttle.username_door_closed
                or result.get("api_status") in (401, 403)
            ):
                throttle.pages_prefetched = True
                self._prefetch_pages(throttle.sweep_usernames)
            # And the verdict is written the moment it is reached, not at the
            # end of the sweep — a restart mid-sweep must not forget it.
            if throttle.username_door_closed and not throttle.door_recorded:
                throttle.door_recorded = True
                await self._remember_username_api_door(closed=True, answered=False)
            return result

    @staticmethod
    def _is_retriable(result: dict) -> bool:
        """A block on EITHER route earns another paced ask — the gate blocks a
        request, not an account. A check that asked nothing (no stored id while
        the username door was shut) has both statuses None and is left alone."""
        return (
            result.get("status") in _RETRIABLE_STATUSES
            or result.get("id_status") in _RETRIABLE_STATUSES
        )

    async def _retry_blocked(
        self,
        outcomes: list[tuple[int, str, dict]],
        *,
        skip_username_api: bool = False,
    ) -> int:
        """Re-check rate-limit-blocked accounts in paced rounds, in place.

        `skip_username_api` carries the sweep's door state into the rounds:
        once the username API has refused every lookup, a retry asks the id
        route and the page doors only — instead of re-knocking on the door
        already known to be shut.

        Instagram's anonymous gate blocks a REQUEST, not an account: the same
        username that 401s inside a sweep answers 200 a minute later on a manual
        recheck. That is exactly the gap the owner kept seeing — a sweep naming
        nine "failures" that all check fine by hand. One retry pass wasn't
        enough, so each round waits out the throttle window (the cooldown
        doubles per round) and re-checks the survivors one at a time, the same
        shape as a manual recheck.

        Bounded by `sweep_retry_budget_seconds` so a genuine hard block can't
        stretch the sweep indefinitely. Returns how many accounts recovered.
        """
        rounds = max(0, settings.sweep_retry_rounds)
        if not rounds:
            return 0
        deadline = time.monotonic() + max(0, settings.sweep_retry_budget_seconds)
        cooldown = _SWEEP_RETRY_COOLDOWN_SECONDS
        recovered = 0

        for round_no in range(1, rounds + 1):
            retriable = [
                (idx, aid, uname)
                for idx, (aid, uname, r) in enumerate(outcomes)
                if not r.get("ok") and self._is_retriable(r)
            ]
            if not retriable:
                break
            if time.monotonic() + cooldown >= deadline:
                logger.info(
                    "Retry budget spent — leaving {} blocked account(s) to the "
                    "next sweep", len(retriable),
                )
                break
            logger.info(
                "Retry round {}/{}: {} blocked account(s) after a {:.0f}s cooldown",
                round_no, rounds, len(retriable), cooldown,
            )
            await asyncio.sleep(cooldown)
            for pos, (idx, aid, uname) in enumerate(retriable):
                if time.monotonic() >= deadline:
                    logger.info("Retry budget spent mid-round — stopping")
                    break
                retry = await self._run_check(
                    aid, uname, thorough=False,
                    skip_username_api=skip_username_api,
                )
                if retry.get("ok"):
                    outcomes[idx] = (aid, uname, retry)
                    recovered += 1
                if pos < len(retriable) - 1:
                    await asyncio.sleep(random.uniform(*_SWEEP_RETRY_GAP_SECONDS))
            cooldown = min(_SWEEP_RETRY_COOLDOWN_MAX_SECONDS, cooldown * 2)

        if recovered:
            logger.info("{} account(s) recovered on retry", recovered)
        return recovered

    async def _run_check(
        self,
        account_id: int,
        username: str,
        *,
        notify_unchanged: bool = False,
        thorough: bool = True,
        skip_username_api: bool = False,
    ) -> dict:
        """One full check. `thorough` (the default) lets a blocked fetch try
        every colo it can — right for on-demand checks, which are one account
        with someone waiting. Sweeps pass False: there, extra attempts are
        multiplied by every account into the blocked traffic that keeps
        Instagram's gate shut, and the paced retry rounds are the second
        chance instead. `skip_username_api` leaves the username API alone
        (the id route and the page doors still run) — a sweep sets it once
        that API has refused every lookup so far."""
        async with self._semaphore:
            try:
                started = time.monotonic()
                result = await self._do_check(
                    account_id, username, notify_unchanged,
                    thorough=thorough, skip_username_api=skip_username_api,
                )
                self._log_check_timing(username, result, time.monotonic() - started)
                return result
            except Exception as exc:
                logger.exception("Unhandled error checking @{}: {}", username, exc)
                return {"ok": False, "username": username, "error": repr(exc)}

    @staticmethod
    def _log_check_timing(username: str, result: dict, total: float) -> None:
        """One line per check saying where its seconds went — by door."""
        timings = result.get("timings")
        if not timings:
            return
        parts = ", ".join(
            f"{name} {seconds:.1f}s" for name, seconds in timings.items()
            if seconds >= 0.05
        )
        logger.info("@{} took {:.1f}s ({})", username, total, parts or "no waits")

    async def _do_check(
        self,
        account_id: int,
        username: str,
        notify_unchanged: bool,
        *,
        thorough: bool = True,
        skip_username_api: bool = False,
    ) -> dict:
        logger.info("Checking @{}", username)
        timings: dict[str, float] = {}

        # Ask by NUMERIC ID first. The id is the key that survives a rename,
        # and since 2026-09-05 it is also the route Instagram still answers
        # anonymously: web_profile_info (by username) returns a 401 login
        # wall from every network measured, residential ones included, while
        # the graphql reel query by id keeps answering through the Worker.
        # One call: current username, avatar URL, story/live status and the
        # highlight catalog — the story phase reuses it, so nothing below
        # asks the reel question twice.
        instagram_id = await self._stored_instagram_id(account_id)
        probe: Optional[IdProbe] = None
        if instagram_id:
            clock = time.monotonic()
            probe = await self.instagram.probe_by_id(instagram_id)
            timings["id"] = time.monotonic() - clock
            if probe.gone:
                # The id itself no longer resolves: deactivated, deleted or
                # banned. Not a rename — a rename keeps the id.
                gone = ProfileFetchResult(
                    username=username, http_status=404,
                    error="the stored Instagram ID no longer resolves",
                )
                result = await self._handle_failure(
                    account_id, username, gone, id_probe=probe
                )
                result["id_status"] = probe.status
                result["api_status"] = None
                result["timings"] = timings
                return result
            if probe.answered and probe.username and probe.username != username:
                username = await self._apply_rename(
                    account_id, username, probe.username
                )

        # Then the username side: the profile API — unless this sweep has
        # already found it refusing every lookup — and, when that is blocked,
        # the page doors: this host's own request, then the home fetcher when
        # one is configured. The page carries the counts, the bio and the
        # privacy flag the id route does not.
        clock = time.monotonic()
        fetch = await self.instagram.fetch_profile(
            username,
            auth_attempts=(
                settings.ig_manual_auth_attempts
                if thorough
                else settings.ig_sweep_auth_attempts
            ),
            api=not skip_username_api,
            # A sweep may use the page the phone already delivered for it;
            # someone waiting on a manual check gets a fresh one.
            cached_page_ok=not thorough,
        )
        timings["username side"] = time.monotonic() - clock
        timings.update(fetch.timings)
        id_status = probe.status if probe is not None else None
        if fetch.success:
            clock = time.monotonic()
            result = await self._handle_success(
                account_id, username, fetch, notify_unchanged,
                reel_data=probe.reel_data if probe is not None else None,
            )
            timings["diff+store"] = time.monotonic() - clock
            result["id_status"] = id_status
            result["api_status"] = fetch.api_status
            result["timings"] = timings
            return result

        if probe is not None and probe.answered:
            # Every username-side door is shut but the id route answered: a
            # LIVE, PARTIAL reading. It knows the username and the avatar; it
            # does not know the counts, the bio or the flags, and those carry
            # forward from the last full reading rather than being invented —
            # see _handle_success.
            partial = ProfileFetchResult(
                username=username,
                http_status=200,
                parsed={
                    "username": probe.username or username,
                    "profile_pic_url": probe.profile_pic_url,
                    "instagram_id": str(instagram_id),
                },
                source="id_probe",
            )
            logger.info(
                "@{} answered by numeric id after the username side returned "
                "{} — partial reading (username, picture, story status)",
                username, fetch.http_status,
            )
            clock = time.monotonic()
            result = await self._handle_success(
                account_id, username, partial, notify_unchanged,
                reel_data=probe.reel_data,
            )
            timings["diff+store"] = time.monotonic() - clock
            # The username side's answer, for the sweep guard. The check
            # itself is ok.
            result["status"] = fetch.http_status
            result["id_status"] = id_status
            result["api_status"] = fetch.api_status
            result["timings"] = timings
            return result

        result = await self._handle_failure(
            account_id, username, fetch, id_probe=probe
        )
        result["id_status"] = id_status
        result["api_status"] = fetch.api_status
        result["timings"] = timings
        return result

    async def _stored_instagram_id(self, account_id: int) -> Optional[str]:
        """The account's numeric id — the stored one, or one recovered from
        the newest snapshot and stored on the way out."""
        async with get_session() as session:
            account = await session.get(MonitoredAccount, account_id)
            if account is None:
                return None
            if account.instagram_id:
                return str(account.instagram_id)
            previous = await crud.get_latest_snapshot(session, account_id)
            recovered = self._extract_instagram_id(
                previous.raw_response if previous else None
            )
            if recovered:
                account.instagram_id = str(recovered)
                await session.flush()
                logger.info(
                    "Recovered @{}'s Instagram ID from its newest snapshot: {}",
                    account.username, recovered,
                )
            return str(recovered) if recovered else None

    async def _apply_rename(self, account_id: int, old: str, new: str) -> str:
        """Persist and announce a username change, found through the numeric
        id or a fetched profile. Returns the username to continue with.

        Announced HERE and only here — the snapshot diff drops its own
        username entry (see _handle_success) so a rename is one message
        however many sources go on to notice it. Persisted even when nothing
        else about the check succeeds: the point of keying on the id is that
        a rename is never thrown away because the profile fetch after it was
        blocked — which is exactly what happened to a target renamed while
        the username route was shut.
        """
        new = (new or "").strip().lstrip("@").lower()
        old = (old or "").strip().lstrip("@").lower()
        if not new or new == old:
            return old
        async with get_session() as session:
            account = await session.get(MonitoredAccount, account_id)
            if account is None:
                return new
            if account.username == new:
                return new  # already applied — another path got there first
            existing = await crud.get_account(session, new)
            collided = existing is not None and existing.id != account_id
            if collided:
                logger.warning(
                    "@{} is now @{}, but that username is already monitored as "
                    "account_id={} — keeping this entry under its old name",
                    account.username, new, existing.id,
                )
            else:
                account.username = new
                await session.flush()
        logger.info(
            "Username changed: @{} -> @{} (account_id={})", old, new, account_id
        )
        msg = render_rename_message(old, new, collided=collided)
        thread_id = await self.topic_for(account_id, new)
        delivered = await self.notifier.send_text(msg, message_thread_id=thread_id)
        async with get_session() as session:
            await crud.log_notification(
                session,
                account_id=account_id,
                change_type="username",
                payload={
                    "field": "username", "label": "username",
                    "old": old, "new": new,
                },
                message=msg,
                delivered=delivered,
            )
        return new

    @staticmethod
    def _extract_instagram_id(raw_response: Optional[dict]) -> Optional[str]:
        return extract_instagram_id(raw_response)

    async def _handle_failure(
        self,
        account_id: int,
        username: str,
        fetch: ProfileFetchResult,
        *,
        id_probe: Optional[IdProbe] = None,
    ) -> dict:
        logger.warning(
            "Fetch failed for @{}: status={} error={}",
            username, fetch.http_status, fetch.error,
        )

        # A 401 is the anonymous gate, which comes and goes per colo, and a
        # single 404 can be that same gate misfiring — so neither gets a
        # snapshot row (it would bury the real history), and a 401 gets no
        # per-account alert (the sweep summary names the block). A 404 IS
        # surfaced, on the cadence below. The check still HAPPENED and still
        # FAILED, though, so the last-checked bookkeeping runs for every status.
        # It used to be skipped here, which froze last_checked_at /
        # last_status_code / consecutive_failures at the last SUCCESS: an
        # account Instagram had been blocking for days still showed
        # "Last check: <3 days ago> · HTTP 200" and zero failures on its card,
        # i.e. a stale check presented as a healthy one.
        gate_status = fetch.http_status in (401, 404)

        async with get_session() as session:
            if not gate_status:
                # Only store a failure snapshot when transitioning from success
                # (i.e. the previous snapshot was OK). Repeated identical failures
                # are not stored — they add no information.
                previous = await crud.get_latest_snapshot(
                    session, account_id, successful_only=False
                )
                is_new_failure = previous is None or previous.http_status == 200
                if is_new_failure:
                    snapshot = AccountSnapshot(
                        account_id=account_id,
                        username=username,
                        http_status=fetch.http_status,
                        raw_response=fetch.raw_response,
                        error=fetch.error,
                    )
                    await crud.insert_snapshot(session, snapshot)
                    # Keep only the latest 200 snapshots per account
                    await crud.cleanup_old_snapshots(session, account_id, keep_count=200)
            failure_count = await crud.mark_checked(
                session, account_id, fetch.http_status, success=False
            )

        # Only notify on the first failure or every 5th consecutive failure
        should_notify = not gate_status and (
            failure_count == 1 or failure_count % 5 == 0
        )
        change_type = "fetch_failure"
        if fetch.http_status == 404:
            # "No such user" used to be swallowed with the 401s as a flaky
            # gate answer — which is how a renamed target failed eight checks
            # without a word. With the id route there to test it, it is a
            # real statement: say so on the second consecutive miss (one can
            # still be a flake), or at once when the id itself no longer
            # resolves (two routes then agree the account is gone), and every
            # 5th check after that.
            first_alert = 1 if (id_probe is not None and id_probe.gone) else 2
            should_notify = failure_count == first_alert or (
                failure_count > first_alert and failure_count % 5 == 0
            )
            change_type = "not_found"
        if should_notify:
            if fetch.http_status == 404:
                msg = render_not_found_message(username, failure_count, id_probe)
            else:
                msg = render_failure_message(username, fetch)
            thread_id = await self.topic_for(account_id, username)
            delivered = await self.notifier.send_text(
                msg, message_thread_id=thread_id
            )
            async with get_session() as session:
                await crud.log_notification(
                    session,
                    account_id=account_id,
                    change_type=change_type,
                    payload={
                        "status": fetch.http_status,
                        "error": fetch.error,
                        "consecutive_failures": failure_count,
                        "id_status": id_probe.status if id_probe else None,
                    },
                    message=msg,
                    delivered=delivered,
                )

        return {
            "ok": False,
            "username": username,
            "status": fetch.http_status,
            "error": fetch.error,
        }

    async def _handle_success(
        self,
        account_id: int,
        username: str,
        fetch: ProfileFetchResult,
        notify_unchanged: bool,
        *,
        reel_data: Optional[dict] = None,
    ) -> dict:
        """Diff, persist and announce one successful reading.

        `reel_data` is this check's answer from the numeric-id probe, when it
        ran — handed in so the reel question is asked once per check, not
        once per phase. A partial reading (source "public_page" or
        "id_probe") carries forward what it could not see and alerts only on
        what it did.
        """
        assert fetch.parsed is not None
        parsed = fetch.parsed

        # A username the source reports that differs from the one on file is
        # a rename: persisted and announced once, here, before anything in
        # this reading is compared. (This used to happen inline at the end of
        # the method, for the API path only — a partial reading that found
        # the same fact threw it away.)
        seen_username = (parsed.get("username") or "").strip().lstrip("@").lower()
        if seen_username and seen_username != username.strip().lstrip("@").lower():
            username = await self._apply_rename(account_id, username, seen_username)

        # Resolve the best available profile picture URL.
        # The mobile API's hd_profile_pic_url_info (~1440px) only exists for
        # logged-in sessions — in the anonymous setup that call NEVER yields a
        # URL, so making it once per account per sweep was pure wasted traffic
        # (and a 401 driver). Only ask when a session cookie is configured;
        # otherwise the web API's profile_pic_url_hd (~320px) is the ceiling.
        pic_url = parsed.get("profile_pic_url")
        instagram_id = parsed.get("instagram_id")
        if instagram_id and settings.ig_session_cookie:
            hd_url = await self.instagram.fetch_hd_pic_url(str(instagram_id))
            if hd_url:
                pic_url = hd_url

        # Skip the download entirely when the CDN URL proves it is the same
        # upload we already fingerprinted. An avatar URL's numeric asset id
        # changes if and ONLY IF a new picture was set (see pic_asset_id), and
        # _pic_changed can never report a change without perceptual evidence —
        # so re-downloading a byte-identical-by-construction image can't alter
        # any outcome. It was the bot's single largest egress line item: a
        # full-resolution JPEG (incompressible, often 100 KB+) per account per
        # sweep, ~48 times a day each, to re-learn a fingerprint we already had.
        # Any of the escape hatches below (no stored fingerprint, unparseable
        # id on either side, first sighting) still downloads.
        baseline_hash, baseline_url = (None, None)
        if pic_url:
            async with get_session() as session:
                baseline_hash, baseline_url = await crud.get_latest_pic_baseline(
                    session, account_id
                )
        current_asset = pic_asset_id(pic_url)
        pic_unchanged = bool(
            pic_url
            and current_asset
            and baseline_hash
            and baseline_hash.startswith(PHASH_PREFIX)
            and current_asset == pic_asset_id(baseline_url)
        )

        hashed: Optional[HashedMedia] = None
        if pic_url and not pic_unchanged:
            hashed = await self.hasher.hash_url(pic_url, username)
        elif pic_unchanged:
            logger.debug(
                "Avatar for @{} is the same upload (asset id {}) — skipping the "
                "download, the stored fingerprint still applies",
                username, current_asset,
            )

        # The direct CDN download can fail (datacenter egress gets blocked) or
        # return an unhashable payload; without a fingerprint the pic check
        # silently skips the whole sweep. Fall back to saveinsta's login-free
        # HD avatar so the check still runs this sweep instead of never. Only
        # when a download was actually ATTEMPTED and failed — a deliberate skip
        # above already has its fingerprint and must not trigger the fallback.
        if (
            not pic_unchanged
            and (hashed is None or hashed.phash is None)
            and pic_url
            and self.stories is not None
        ):
            try:
                fallback_url = await self.stories.fetch_profile_pic_url(username)
            except Exception as exc:  # pragma: no cover - network failure path
                fallback_url = None
                logger.debug(
                    "Fallback avatar URL lookup failed for @{}: {}", username, exc
                )
            if fallback_url:
                fallback = await self.hasher.hash_url(fallback_url, username)
                if fallback is not None and fallback.phash:
                    hashed = fallback
                    logger.info(
                        "Fingerprinted @{}'s avatar via the saveinsta fallback "
                        "(direct CDN download failed)",
                        username,
                    )

        # Change detection runs on the PERCEPTUAL hash, not sha256: the CDN
        # serves byte-different re-encodes of the same avatar on every signed
        # URL, so sha256 flip-flops each sweep and raised a false "profile
        # picture changed" alert every time. phash is stable across re-encodes.
        # (The sha256 + file still feed the ProfileMediaHash dedup ledger below.)
        new_pic_hash = hashed.phash if hashed else None

        # Confirmation pass — check the pic AGAIN before it can alert. A
        # tentative change (fresh fingerprint differs from the stored baseline)
        # must be confirmed by a SECOND independent download whose fingerprint
        # (a) also differs from the baseline and (b) agrees with the first —
        # i.e. two downloads both saw the same NEW picture. A one-off corrupt/
        # truncated payload or a mid-swap CDN flicker fails (b) and is
        # suppressed; a real change that got suppressed re-confirms and alerts
        # on the very next sweep, so nothing is ever lost — only delayed past
        # the glitch. Costs one extra CDN download only when a change is
        # tentatively detected, i.e. almost never. The stored/current avatar
        # URLs ride along so the asset-id signal (see _pic_changed) applies to
        # the tentative and confirmation comparisons alike.
        web_pic_url = parsed.get("profile_pic_url")
        if new_pic_hash and hashed is not None:
            # Baseline already loaded above for the skip decision — same row.
            if baseline_hash and pic_fingerprints_differ(
                baseline_hash, new_pic_hash,
                old_url=baseline_url, new_url=web_pic_url,
            ):
                second = await self.hasher.hash_url(hashed.source_url, username)
                second_hash = second.phash if second else None
                confirmed = bool(
                    second_hash
                    and pic_fingerprints_differ(
                        baseline_hash, second_hash,
                        old_url=baseline_url, new_url=web_pic_url,
                    )
                    and not pic_fingerprints_differ(new_pic_hash, second_hash)
                )
                if confirmed:
                    logger.info(
                        "Profile-pic change for @{} confirmed by second download",
                        username,
                    )
                else:
                    logger.warning(
                        "Profile-pic change for @{} NOT confirmed on re-download "
                        "(second={}) — suppressing this sweep; a real change "
                        "re-confirms next sweep",
                        username,
                        "unhashable" if not second_hash else "disagrees",
                    )
                    # Treat as if this fetch produced no usable fingerprint:
                    # the stored baseline carries forward and no alert fires.
                    new_pic_hash = None

        # For public accounts with instagram_id, fetch reel data (stories/highlights/live status)
        # This will be stored in the snapshot for future reference
        reel_data_response = None
        if reel_data is not None:
            # The numeric-id probe already asked this check's reel question.
            reel_data_response = {
                "has_public_story": bool(reel_data.get("has_public_story")),
                "is_live": bool(reel_data.get("is_live")),
                "highlights": reel_data.get("highlights") or {},
            }
        elif fetch.partial:
            # A partial result with no probe answer means the id route did
            # not answer either (or the account has no stored id) — the reel
            # query IS that route, so asking again is one more blocked call
            # for an answer already known. The story phase treats it as
            # unknown and falls back to saveinsta, which is reachable.
            logger.debug(
                "Skipping the reel query for @{} — the id route did not answer "
                "and the {} supplied this check", username, fetch.source,
            )
        elif not parsed.get("is_private") and instagram_id:
            try:
                reel_user = await self.instagram.fetch_reel_user(str(instagram_id))
                if reel_user:
                    reel_data_response = {
                        "has_public_story": reel_user.get("has_public_story", False),
                        "is_live": reel_user.get("is_live", False),
                        "highlights": reel_user.get("highlights", {}),
                    }
                    logger.debug(
                        "Fetched reel data for @{} during profile check: story={}, live={}",
                        username,
                        reel_user.get("has_public_story"),
                        reel_user.get("is_live"),
                    )
            except Exception as exc:
                logger.debug(
                    "Could not fetch reel data for @{} during profile check: {}",
                    username, exc
                )

        # Persist a slim form: the numeric user id (404 recovery / ID backfill)
        # and this check's reel_data as a record of what the reel query said at
        # this timestamp. Nothing reads reel_data back as CURRENT status — that
        # is always a live fetch — so a stale row can't be mistaken for now.
        # The full web_profile_info payload is 50–200 KB per snapshot and was
        # the main thing filling the database; this form is a few hundred bytes,
        # so the 0.5 GB free tier effectively never fills. Everything diffable
        # already lives in the snapshot's columns.
        parsed_instagram_id = parsed.get("instagram_id") or self._extract_instagram_id(
            fetch.raw_response
        )
        slim_raw: dict = {}
        if parsed_instagram_id:
            slim_raw["data"] = {"user": {"id": str(parsed_instagram_id)}}
        if reel_data_response:
            slim_raw["reel_data"] = reel_data_response
        if fetch.partial:
            # Which door answered, on the row itself — so a snapshot whose bio
            # was carried forward is identifiable later as a partial reading
            # rather than a full one.
            slim_raw["source"] = fetch.source

        async with get_session() as session:
            # Two different reads: the newest snapshot from ANY door (what the
            # account was last known to look like — the right thing to carry
            # forward) and the newest from THIS door (the only thing safe to
            # diff against, since the API and the public page disagree about
            # the same account at the same moment).
            last_known = await crud.get_latest_snapshot(session, account_id)
            previous = await crud.get_latest_snapshot_by_source(
                session, account_id, source=fetch.source if fetch.partial else None
            )

            # If this fetch produced no usable fingerprint (download failed,
            # non-image payload, or the confirmation pass suppressed a
            # tentative change), carry the last known hash AND its URL forward
            # instead of nulling/overwriting the baseline. Losing the hash
            # would make the next good fetch look like a first observation and
            # skip the legitimate change alert; absorbing the new URL without
            # a verified fingerprint would swallow a new upload's asset id and
            # permanently hide the change from the URL-identity signal.
            stored_pic_hash = new_pic_hash or (
                last_known.profile_pic_hash if last_known else None
            )
            # `pic_unchanged` means this URL carries the SAME asset id as the
            # stored one, so absorbing its fresh signature can't hide an upload.
            stored_pic_url = (
                web_pic_url
                if new_pic_hash or pic_unchanged or last_known is None
                else last_known.profile_pic_url
            )

            # The public-page fallback sees the counts, the name, the bio and
            # the privacy/verification flags; it does not see reels_count,
            # story_count or is_business. An id-only reading sees even less:
            # just the username and the avatar. For everything a partial
            # source did not observe — and for anything the payload happened
            # to omit — carry the last known value forward rather than
            # writing a None:
            #  - the card would otherwise show a real bio as newly empty, which
            #    is a wrong "current" value, not a missing one;
            #  - and diffing the next full API check against None would silently
            #    swallow a bio change that happened while the API was blocked,
            #    whereas against the carried value it still reports.
            # Fields the source DID observe always win, so nothing fresh is
            # overwritten by a stale value.
            def observed(field: str):
                if not fetch.partial or field in parsed:
                    return parsed.get(field)
                # Carry forward from the LAST KNOWN value whatever door saw it
                # last — this is "what we last knew", not "what to diff".
                return getattr(last_known, field, None) if last_known else None

            snapshot = AccountSnapshot(
                account_id=account_id,
                username=parsed.get("username") or username,
                full_name=observed("full_name"),
                biography=observed("biography"),
                followers_count=observed("followers_count"),
                following_count=observed("following_count"),
                posts_count=observed("posts_count"),
                reels_count=observed("reels_count"),
                story_count=observed("story_count"),
                is_private=observed("is_private"),
                is_verified=observed("is_verified"),
                is_business=observed("is_business"),
                profile_pic_url=stored_pic_url,
                profile_pic_hash=stored_pic_hash,
                external_url=observed("external_url"),
                http_status=200,
                raw_response=slim_raw or None,
            )
            # What this account IS, as best we know — this reading's flag, or
            # the last known one carried into it by `observed`. Used for every
            # public-only decision below (story phase, post delivery).
            # Unknown is not public: with no flag from this reading and none
            # on record (an account whose first-ever reading was id-only),
            # treat it as private — the same default the story phase uses.
            effective_private = (
                True if snapshot.is_private is None else bool(snapshot.is_private)
            )

            # Diff first, persist only when something actually changed. The
            # baseline is this source's own history, so the first reading from
            # a new door establishes a baseline silently instead of reporting
            # every field it disagrees with as a change.
            # A partial reading may only alert on what it actually observed —
            # the carried-forward fields above came from the other door and
            # would otherwise re-report a change the API already announced.
            changeset = detect_changes(
                previous,
                snapshot,
                new_pic_hash=new_pic_hash,
                observed_fields=set(parsed) if fetch.partial else None,
            )
            # The rename (if any) was announced by _apply_rename above; drop
            # the diff's own copy so one rename is one message whichever
            # source noticed it. The row is still written, so the history
            # shows the name at each reading.
            renamed_in_diff = changeset.find("username") is not None
            if renamed_in_diff:
                changeset.changes = [
                    c for c in changeset.changes if c.field != "username"
                ]
            if previous is None or changeset.has_changes or renamed_in_diff:
                await crud.insert_snapshot(session, snapshot)
                # Keep only the latest 200 snapshots per account
                await crud.cleanup_old_snapshots(session, account_id, keep_count=200)
            else:
                # Refresh in place with the slim form — the old code stored the
                # full payload here WITHOUT reel_data, so every unchanged sweep
                # both bloated the row and wiped the stored story/highlight
                # state. The slim form keeps reel_data current instead.
                previous.raw_response = slim_raw or None
                previous.profile_pic_url = stored_pic_url
                previous.profile_pic_hash = stored_pic_hash
                previous.error = None
                logger.debug(
                    "@{} - no changes detected; refreshed latest 200 response",
                    username,
                )

            # Persist profile picture hash if new.
            if hashed is not None:
                existing = await crud.find_media_hash(session, account_id, hashed.sha256)
                if existing is None:
                    await crud.insert_media_hash(
                        session,
                        ProfileMediaHash(
                            account_id=account_id,
                            sha256=hashed.sha256,
                            source_url=hashed.source_url,
                            local_path=str(hashed.local_path),
                            byte_size=hashed.byte_size,
                            content_type=hashed.content_type,
                        ),
                    )

            # Update Instagram ID & last-checked (the username was already
            # brought up to date by _apply_rename at the top)
            stored_id: Optional[str] = None
            account = await session.get(MonitoredAccount, account_id)
            if account is not None:
                # Store Instagram ID if account doesn't have one yet
                if parsed_instagram_id and not account.instagram_id:
                    account.instagram_id = str(parsed_instagram_id)
                    await session.flush()  # Ensure ID is persisted immediately
                    logger.info(
                        "Stored Instagram ID for @{}: {}",
                        account.username,
                        parsed_instagram_id,
                    )
                stored_id = account.instagram_id

            await crud.mark_checked(session, account_id, 200, success=True)

        await self._dispatch_changes(
            account_id,
            username,
            changeset,
            previous_snapshot_id=previous.id if previous else None,
            new_pic_path=hashed.local_path if hashed else None,
            notify_unchanged=notify_unchanged,
        )

        # New post/reel auto-download for public accounts (login-free via
        # saveinsta). On the first observation we just baseline what's already
        # there; afterwards a rise in the post/reel count delivers the new media.
        if self.stories is not None and not effective_private:
            await self._handle_new_posts(
                account_id, username, changeset, first_seen=last_known is None
            )

        return {
            "ok": True,
            "username": username,
            "status": 200,
            "changed": changeset.has_changes or renamed_in_diff,
            "change_count": (
                len(changeset.changes)
                + (1 if changeset.profile_pic_changed else 0)
                + (1 if renamed_in_diff else 0)
            ),
            "first_seen": last_known is None,
            # Which partial door supplied this reading (None for the full
            # API reading) — the sweep summary counts id-only checks by it.
            "partial": fetch.source if fetch.partial else None,
            # The PARTIAL public-page reading cannot see the privacy flag, and
            # `bool(None)` would call a private account public — which is how
            # private targets ended up in the story phase getting a "NO STORY"
            # line. Report what the snapshot actually holds (this reading, or
            # the last known value carried into it), so a missing flag falls
            # back to what we knew rather than to False.
            "is_private": bool(effective_private),
            "went_public": self._went_public(changeset),
            "instagram_id": stored_id or parsed.get("instagram_id"),
            # This check's live reel query (None when it didn't answer). The
            # story phase reports status from THIS, never from the snapshot.
            "reel_data": reel_data_response,
        }

    @staticmethod
    def _went_public(changeset: ChangeSet) -> bool:
        """True when this change set marks a private -> public transition.

        Uses truthiness (not ``is True``/``is False``) so a DB that hands back
        1/0 instead of bools still classifies correctly: old private (truthy) →
        new public (falsy).
        """
        change = changeset.find("is_private")
        return bool(change is not None and change.old and not change.new)

    async def _dispatch_changes(
        self,
        account_id: int,
        username: str,
        changeset: ChangeSet,
        *,
        previous_snapshot_id: Optional[int],
        new_pic_path,
        notify_unchanged: bool,
    ) -> None:
        thread_id = await self.topic_for(account_id, username)
        if not changeset.has_changes:
            if notify_unchanged:
                await self.notifier.send_text(
                    f"<b>@{username}</b>\nNo changes detected.\n"
                    f"Checked at {fmt_timestamp(datetime.now(timezone.utc))}",
                    message_thread_id=thread_id,
                )
            return

        # Send aggregated text message
        text = render_changes_message(changeset, first_seen=previous_snapshot_id is None)
        delivered = False
        if text:
            delivered = await self.notifier.send_text(text, message_thread_id=thread_id)

        async with get_session() as session:
            for change in changeset.changes:
                await crud.log_notification(
                    session,
                    account_id=account_id,
                    change_type=change.field,
                    payload=change.as_dict(),
                    message=text,
                    delivered=delivered,
                )

        # Follower anomaly: when the follower change is unusually large for this
        # account's size, send a separate high-visibility alert and log it under
        # its own change_type (so it stands out in /history and the digest).
        follower_change = changeset.find("followers_count")
        if follower_change is not None:
            anomaly = classify_follower_change(
                follower_change.old,
                follower_change.new,
                abs_min=settings.follower_anomaly_abs_min,
                pct_min=settings.follower_anomaly_pct_min,
            )
            if anomaly is not None:
                alert = render_follower_anomaly(username, anomaly)
                a_delivered = await self.notifier.send_text(
                    alert, message_thread_id=thread_id
                )
                async with get_session() as session:
                    await crud.log_notification(
                        session,
                        account_id=account_id,
                        change_type="follower_anomaly",
                        payload={
                            "direction": anomaly.direction,
                            "old": anomaly.old,
                            "new": anomaly.new,
                            "delta": anomaly.delta,
                            "pct": round(anomaly.pct, 4),
                        },
                        message=alert,
                        delivered=a_delivered,
                    )

        # Profile picture sent as a document to preserve full quality
        if changeset.profile_pic_changed and new_pic_path is not None:
            caption = (
                f"<b>@{username}</b> changed profile picture\n"
                f"Old fingerprint: <code>{changeset.old_pic_hash}</code>\n"
                f"New fingerprint: <code>{changeset.new_pic_hash}</code>"
            )
            ok = await self.notifier.send_document(
                new_pic_path, caption=caption, message_thread_id=thread_id
            )
            async with get_session() as session:
                await crud.log_notification(
                    session,
                    account_id=account_id,
                    change_type="profile_picture",
                    payload={
                        "old": changeset.old_pic_hash,
                        "new": changeset.new_pic_hash,
                    },
                    message=caption,
                    delivered=ok,
                )

    async def _load_account_story_meta(self, account_id: int) -> dict:
        async with get_session() as session:
            account = await session.get(MonitoredAccount, account_id)
            snapshot = await crud.get_latest_snapshot(
                session, account_id, successful_only=True
            )
        is_private = True
        if snapshot is not None and snapshot.is_private is not None:
            is_private = bool(snapshot.is_private)
        return {
            "is_private": is_private,
            "instagram_id": account.instagram_id if account else None,
        }

    async def _fetch_highlight_catalog(
        self, username: str, instagram_id: Optional[str]
    ) -> dict[str, str]:
        """Highlight reel id -> title via Instagram's graphql reel query (anonymous).

        The reel query needs the numeric user id, so resolve it from the username
        when it isn't stored yet — otherwise we'd skip the working path entirely.
        The old storiesig fallback is gone (that API was discontinued).
        """
        if not instagram_id:
            fetch = await self.instagram.fetch_profile(username)
            if fetch.success and fetch.parsed:
                instagram_id = fetch.parsed.get("instagram_id")
        if instagram_id:
            reel_user = await self.instagram.fetch_reel_user(str(instagram_id))
            if reel_user is not None and "highlights" in reel_user:
                return dict(reel_user["highlights"])
        return {}

    async def _gather_highlight_items(
        self, username: str, catalog: dict[str, str], *, cancellable: bool = False
    ) -> list:
        """Download story items across every highlight reel in the catalog.

        The reel ids come from Instagram's graphql query (anonymous); the media
        itself comes from saveinsta.to per reel. Failures on individual reels are
        swallowed so one bad reel never sinks the rest. When `cancellable`, a
        /kill aborts the in-flight reel fetches and returns nothing — so a huge
        catalog can be stopped before the (longer) delivery phase even begins.
        """
        if self.stories is None or not catalog:
            return []
        if cancellable and self._download_cancel.is_set():
            return []

        tasks = [
            asyncio.ensure_future(
                self.stories.fetch_highlight_items(username, hid, title)
            )
            for hid, title in catalog.items()
        ]
        gather = asyncio.gather(*tasks, return_exceptions=True)

        if cancellable:
            cancel_wait = asyncio.ensure_future(self._download_cancel.wait())
            done, _ = await asyncio.wait(
                {gather, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if gather not in done:
                # /kill won the race — drop the outstanding reel fetches.
                for t in tasks:
                    t.cancel()
                gather.cancel()
                # Awaiting a cancelled gather re-raises CancelledError (a
                # BaseException, so plain `suppress(Exception)` misses it) —
                # swallow it; we're intentionally tearing the gather down.
                with contextlib.suppress(asyncio.CancelledError):
                    await gather
                logger.info("/kill — aborted highlight gather for @{}", username)
                return []
            cancel_wait.cancel()
            results = gather.result()
        else:
            results = await gather

        items: list = []
        for r in results:
            if isinstance(r, list):
                items.extend(r)
            elif isinstance(r, Exception):
                logger.debug("Highlight item fetch failed for @{}: {}", username, r)
        return items

    @staticmethod
    def _diff_highlight_catalog(
        previous: dict[str, str], current: dict[str, str]
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str, str]]]:
        prev_ids = set(previous)
        curr_ids = set(current)
        added = [(hid, current[hid]) for hid in sorted(curr_ids - prev_ids)]
        removed = [(hid, previous[hid]) for hid in sorted(prev_ids - curr_ids)]
        renamed = [
            (hid, previous[hid], current[hid])
            for hid in sorted(prev_ids & curr_ids)
            if previous[hid] != current[hid]
        ]
        return added, removed, renamed

    @staticmethod
    def _story_state_key(account_id: int) -> str:
        """Last OBSERVED story status — the transition baseline. Written only
        from a live reel query, never from a blocked check."""
        return f"story_state:{account_id}"

    @staticmethod
    def _story_announce_key(account_id: int) -> str:
        """Last story status ANNOUNCED to the chat, which is a different thing
        from the observed one: it advances even when the message was suppressed
        as a duplicate (delivered media already said it). Quiet mode compares
        against this, so one story yields one message however many sweeps it
        survives. See _announce_story_status."""
        return f"story_announced:{account_id}"

    @staticmethod
    def _highlight_scan_key(account_id: int) -> str:
        return f"highlight_scan:{account_id}"

    async def _due_highlight_scan(
        self,
        account_id: int,
        tracked_catalog: dict[str, str],
        previous_catalog: dict[str, str],
    ) -> tuple[dict[str, str], bool]:
        """Which highlight reels to list this sweep, and whether it's a full scan.

        A reel's media listing costs one third-party round-trip each, every
        sweep, to re-discover items that (almost always) haven't changed. So a
        full re-list runs at most once per `highlight_scan_interval`; between
        those, only reels that are NEW to the catalog are listed — a reel can't
        gain items without its owner adding a story to it, and that story was
        already delivered live by the story phase above. Returns the catalog to
        scan plus True when this is the full pass (so the caller can stamp the
        clock only after the work actually happened).
        """
        interval = settings.highlight_scan_interval
        if interval <= 0 or not tracked_catalog:
            return tracked_catalog, True
        async with get_session() as session:
            raw = await crud.get_setting(session, self._highlight_scan_key(account_id))
        try:
            last = float(raw) if raw else None
        except ValueError:
            last = None
        if last is None or (time.time() - last) >= interval:
            return tracked_catalog, True
        fresh = {
            hid: title
            for hid, title in tracked_catalog.items()
            if hid not in previous_catalog
        }
        if fresh:
            logger.debug(
                "Listing only {} new highlight reel(s) for account {} — full "
                "re-scan is not due for another {:.0f}s",
                len(fresh), account_id, interval - (time.time() - last),
            )
        return fresh, False

    async def _check_stories_and_highlights(
        self,
        account_id: int,
        username: str,
        *,
        instagram_id: Optional[str] = None,
        reel_data: Optional[dict] = None,
        always_report: bool = False,
        skip_reel_fallback: bool = False,
    ) -> None:
        """Stories, highlight catalog changes, and new highlight media for public accounts.

        `reel_data` is THIS check's reel query (has_public_story / is_live /
        highlight catalog), handed down by the caller so the same fetch serves
        both phases. It is None when the profile check failed or never ran, and
        then one live fetch is attempted here. If that fails too, the story/live
        status is reported as unavailable — never re-read from a stored
        snapshot, whose reel_data is only as current as the last SUCCESSFUL
        check (days old for an account Instagram is currently blocking).

        `always_report` forces the story/live status line out even when nothing
        changed. Sweeps leave it False (see `_announce_story_status`); a manual
        Recheck sets it, because someone who just asked for a check is owed an
        answer either way.

        `skip_reel_fallback` suppresses that one live fetch when the sweep has
        already established that Instagram is blocking everything — the status
        is reported as unavailable without spending 8 more blocked upstream
        attempts to confirm it. The saveinsta story fetch below still runs.
        """
        assert self.stories is not None
        async with self._semaphore:
            try:
                async with get_session() as session:
                    previous_catalog = await crud.get_highlight_catalog(
                        session, account_id
                    )
                    seen_pks = await crud.get_seen_story_pks(session, account_id)

                # Route everything in this account's check to its own topic.
                thread_id = await self.topic_for(account_id, username)

                # The profile check already ran the reel query and passed the
                # result down, so this costs nothing on a healthy check. Only a
                # failed/absent profile check reaches Instagram again here.
                attempted_reel = False
                if reel_data is None and instagram_id and not skip_reel_fallback:
                    attempted_reel = True
                    reel_user = await self.instagram.fetch_reel_user(str(instagram_id))
                    if reel_user:
                        reel_data = {
                            "has_public_story": reel_user.get("has_public_story", False),
                            "is_live": reel_user.get("is_live", False),
                            "highlights": reel_user.get("highlights", {}),
                        }
                        logger.debug(
                            "Fetched reel data for @{} during story check "
                            "(profile check didn't supply it)",
                            username
                        )

                # Highlight catalog rides on the same reel query as story/live
                # status — reuse it rather than asking Instagram again; every
                # avoided call lowers the 401 rate. Only reach for a separate
                # fetch when no reel query has been attempted at all (e.g. the
                # numeric id isn't stored yet, which that path resolves).
                catalog = (reel_data or {}).get("highlights")
                if catalog is None and not attempted_reel and not skip_reel_fallback:
                    catalog = await self._fetch_highlight_catalog(
                        username, instagram_id
                    )
                if catalog is None:
                    # Reel query unavailable this check — "unknown", not "empty".
                    # The guard below keeps the stored catalog untouched.
                    catalog = {}

                establishing_baseline = not previous_catalog and bool(catalog)

                # An EMPTY result almost always means the anonymous fetch failed or
                # was rate-limited (the reel query intermittently omits highlight
                # edges) — NOT that the user deleted every reel. Diffing empty
                # against a stored catalog would wrongly report all reels as
                # "removed" and then overwrite the stored catalog with nothing.
                # So only diff/notify/persist when we actually got a catalog back;
                # otherwise keep the last known-good catalog untouched.
                if catalog:
                    added, removed, renamed = self._diff_highlight_catalog(
                        previous_catalog, catalog
                    )
                    if previous_catalog and (added or removed or renamed):
                        msg = render_highlight_catalog_changes(
                            username,
                            added=added,
                            removed=removed,
                            renamed=renamed,
                            total=len(catalog),
                        )
                        delivered = await self.notifier.send_text(
                            msg, message_thread_id=thread_id
                        )
                        async with get_session() as session:
                            await crud.log_notification(
                                session,
                                account_id=account_id,
                                change_type="highlight_catalog",
                                payload={
                                    "added": added,
                                    "removed": removed,
                                    "renamed": renamed,
                                    "total": len(catalog),
                                },
                                message=msg,
                                delivered=delivered,
                            )

                    async with get_session() as session:
                        await crud.replace_highlight_catalog(
                            session, account_id, catalog
                        )
                elif previous_catalog:
                    logger.debug(
                        "Empty highlight catalog for @{} — keeping {} previously "
                        "stored reel(s) (likely a transient/rate-limited fetch)",
                        username,
                        len(previous_catalog),
                    )

                # Story/live status — derived ONLY from this check's live reel
                # data. When Instagram didn't answer, say so instead of dressing
                # the last known status up as the current one: that's how an
                # account whose profile fetch had been 401ing for days kept
                # getting "🎬 HAS STORY" out of a stale snapshot while its Story
                # button (a live saveinsta fetch) correctly said there was none.
                #
                # The message is only BUILT here. Sending it waits until the
                # story media has been dealt with, because whether the media
                # went out decides whether this line is news or a duplicate of
                # it — see _announce_story_status.
                state_key = self._story_state_key(account_id)
                has_public_story = False
                if reel_data is not None:
                    has_public_story = bool(reel_data.get("has_public_story"))
                    is_live = bool(reel_data.get("is_live"))

                    # The previous status comes from the last OBSERVED one, kept
                    # in app_settings — not from snapshot rows, which are only
                    # written/refreshed when the profile itself changes, so they
                    # can be weeks apart and would fire "just posted a story!"
                    # on every sweep of one long-lived story (or never at all).
                    async with get_session() as session:
                        prev_state = await crud.get_setting(session, state_key)
                    prev_is_live = prev_state == "live"
                    prev_has_story = prev_state == "story"

                    # The status line is upgraded to a "just went live" / "just
                    # posted a story" alert only when the status actually
                    # changed since the last observation. With no prior
                    # observation (or while establishing the highlight baseline)
                    # there is no real prior state, so the "just …" wording is
                    # never used then.
                    first_observation = prev_state is None
                    just_live = (
                        is_live
                        and not prev_is_live
                        and not first_observation
                        and not establishing_baseline
                    )
                    just_story = (
                        has_public_story
                        and not prev_has_story
                        and not first_observation
                        and not establishing_baseline
                    )

                    if just_live:
                        msg = f"🔴 <b>@{esc(username)}</b> just went live!"
                        change_type = "going_live"
                    elif is_live:
                        msg = f"<b>@{esc(username)}</b> — 🔴 LIVE NOW"
                        change_type = "story_status"
                    elif just_story:
                        msg = f"🎬 <b>@{esc(username)}</b> just posted a story!"
                        change_type = "story_posted"
                    elif has_public_story:
                        msg = f"<b>@{esc(username)}</b> — 🎬 HAS STORY"
                        change_type = "story_status"
                    else:
                        msg = f"<b>@{esc(username)}</b> — ⭕ NO STORY"
                        change_type = "story_status"

                    status = (
                        "live" if is_live else "story" if has_public_story else "none"
                    )
                    status_payload = {
                        "has_public_story": has_public_story,
                        "is_live": is_live,
                    }
                    async with get_session() as session:
                        # Only an OBSERVED status updates the baseline, so a
                        # blocked sweep can never manufacture a transition.
                        await crud.set_setting(session, state_key, status)
                else:
                    status = "unknown"
                    msg = (
                        f"<b>@{esc(username)}</b> — ⚠️ story status unavailable\n"
                        "<i>Instagram didn't answer the live check — not "
                        "repeating the last known status.</i>"
                    )
                    change_type = "story_status_unknown"
                    status_payload = {"reason": "reel query unavailable"}
                    logger.warning(
                        "No live reel data for @{} this check — story/live status "
                        "reported as unavailable rather than from stored data",
                        username,
                    )

                # Fetch the actual story items to download (anonymous, no login,
                # via saveinsta.to). A dead/rate-limited source just yields [].
                # Skipped when Instagram's own live flag says there is no story
                # to fetch: it's the same signal the sweep just reported as
                # "⭕ NO STORY", and the media listing behind it would come back
                # empty. Only a status we actually observed can gate this — when
                # the reel query didn't answer, saveinsta IS the story oracle
                # and must still be asked.
                if reel_data is not None and not has_public_story:
                    stories = []
                    logger.debug(
                        "@{} has no active story — skipping the story media "
                        "listing this sweep", username,
                    )
                else:
                    stories = await self.stories.fetch_stories(username)
                new_stories = [s for s in stories if s.pk and s.pk not in seen_pks]

                if establishing_baseline:
                    highlight_items = await self._gather_highlight_items(
                        username, catalog
                    )
                    async with get_session() as session:
                        await crud.mark_story_items_seen(
                            session, account_id, stories + highlight_items
                        )
                        # Baseline counts as a full reel scan — start the
                        # re-scan clock here instead of listing them all again
                        # on the very next sweep.
                        await crud.set_setting(
                            session,
                            self._highlight_scan_key(account_id),
                            str(time.time()),
                        )
                    logger.info(
                        "Established story/highlight baseline for @{} ({} reels, {} items)",
                        username,
                        len(catalog),
                        len(stories) + len(highlight_items),
                    )
                    # Nothing is delivered on a baseline (that's the point), so
                    # the status line is the only word this account gets.
                    await self._announce_story_status(
                        account_id, username,
                        thread_id=thread_id, status=status, message=msg,
                        change_type=change_type, payload=status_payload,
                        always=always_report,
                    )
                    return

                sent = 0
                if new_stories:
                    sent = await self._deliver_story_items(
                        account_id, username, new_stories, seen_pks,
                        message_thread_id=thread_id,
                    )
                    if not sent:
                        # Every download failed, so the media messages that would
                        # have announced the story never went out. Say it in
                        # text rather than let a real story pass in silence.
                        alert = render_new_stories_alert(username, len(new_stories))
                        await self.notifier.send_text(
                            alert, message_thread_id=thread_id
                        )

                # Last, now that it's known whether the story already spoke for
                # itself. `new_stories` covers both ways it can have: the media
                # messages, or the text alert that replaced them.
                await self._announce_story_status(
                    account_id, username,
                    thread_id=thread_id, status=status, message=msg,
                    change_type=change_type, payload=status_payload,
                    already_announced=bool(new_stories), always=always_report,
                )

                # Auto-download honors per-highlight mutes: untracked reels are
                # skipped entirely (not even fetched). Unmuting re-baselines the
                # reel, so the skipped items never flood in later.
                async with get_session() as session:
                    untracked = await crud.get_untracked_highlight_ids(
                        session, account_id
                    )
                tracked_catalog = {
                    hid: title
                    for hid, title in catalog.items()
                    if hid not in untracked
                }

                # Re-listing every reel's media on every sweep was the second
                # biggest egress line item and it almost never finds anything: a
                # reel changes only when its owner adds a story to it, and that
                # story was already detected and delivered live minutes earlier.
                # Reels that are NEW to the catalog are listed immediately; the
                # rest are re-listed once per highlight_scan_interval (0 = every
                # sweep, the old behavior).
                scan_catalog, full_scan = await self._due_highlight_scan(
                    account_id, tracked_catalog, previous_catalog
                )
                highlight_items = await self._gather_highlight_items(
                    username, scan_catalog
                )
                if full_scan and scan_catalog:
                    async with get_session() as session:
                        await crud.set_setting(
                            session,
                            self._highlight_scan_key(account_id),
                            str(time.time()),
                        )
                new_highlight_items = [
                    i for i in highlight_items if i.pk and i.pk not in seen_pks
                ]
                if new_highlight_items:
                    await self._deliver_story_items(
                        account_id, username, new_highlight_items, seen_pks,
                        message_thread_id=thread_id,
                    )
            except Exception as exc:
                logger.exception(
                    "Story check failed for @{}: {}", username, exc
                )

    async def _announce_story_status(
        self,
        account_id: int,
        username: str,
        *,
        thread_id: Optional[int],
        status: str,
        message: str,
        change_type: str,
        payload: dict,
        already_announced: bool = False,
        always: bool = False,
    ) -> None:
        """Send the story/live status line for this check.

        `status` is this check's observed state: "live" / "story" / "none" /
        "unknown". The ordering below is the whole design:

        - `already_announced` wins over everything. This check has already sent
          a message about the story — the media itself ("📖 @user — new story"),
          or the text alert that stands in when the downloads fail. A "HAS
          STORY" line after it is a second copy of the same news, which is the
          noise that made the per-sweep line unbearable in the first place. The
          baseline still advances, so nothing is lost.
        - otherwise the default (STORY_STATUS_HEARTBEAT) answers for EVERY
          check, including "⭕ NO STORY". Silence is not an answer: with
          change-only announcements, "nothing is up right now" and "the bot
          never got to this account" looked exactly the same from the outside.
        - `always` (manual Recheck) answers even with the heartbeat off, since
          someone is waiting on it.
        - with the heartbeat off, the line goes out only when the status DIFFERS
          from the one last announced, and "unknown" is never announced (the
          sweep summary already names every account Instagram wouldn't answer
          for).

        An unobserved status never becomes the baseline either way, so a blocked
        sweep can't manufacture a transition on recovery. Every status is logged,
        so the digest and history see every check regardless of what was sent.
        """
        announce_key = self._story_announce_key(account_id)
        async with get_session() as session:
            last_announced = await crud.get_setting(session, announce_key)

        if already_announced:
            send = False                    # the media already said it
        elif settings.story_status_heartbeat:
            send = True                     # default: answer for every check
        elif always:
            send = True                     # a Recheck is owed an answer
        elif status == "unknown":
            send = False                    # the sweep summary covers this
        else:
            send = status != last_announced

        delivered = False
        if send:
            delivered = await self.notifier.send_text(
                message, message_thread_id=thread_id
            )
        else:
            logger.debug(
                "Story status for @{} unchanged ({}) — logged, not sent",
                username, status,
            )

        async with get_session() as session:
            # An unobserved status is not a status: leaving the baseline alone
            # keeps a blocked sweep from announcing a "change" on recovery.
            if status != "unknown":
                await crud.set_setting(session, announce_key, status)
            await crud.log_notification(
                session,
                account_id=account_id,
                change_type=change_type,
                payload=payload,
                message=message,
                delivered=delivered,
            )

    async def _deliver_story_items(
        self,
        account_id: Optional[int],
        username: str,
        items: list,
        seen_pks: set[str],
        *,
        message_thread_id: Optional[int] = None,
        cancellable: bool = False,
    ) -> int:
        """Download and send each item; record it as seen. Returns the number sent.

        `account_id` is None for ad-hoc fetches of accounts that aren't monitored
        (e.g. /story for any username) — in that case nothing is persisted as
        seen, since there's no account row to dedup against on later sweeps.
        `message_thread_id` routes the media to a per-account forum topic when
        set (sweep path); on-demand callers leave it None for the General thread.
        `cancellable` (on-demand downloads only) makes the loop stop between
        items as soon as /kill is requested — already-sent media stays, the rest
        is skipped. Sweep deliveries leave it False so /kill never touches them.
        """
        assert self.stories is not None
        sent = 0
        for item in items:
            if cancellable and self._download_cancel.is_set():
                logger.info(
                    "/kill — stopped delivering to @{} after {} item(s)",
                    username, sent,
                )
                break
            if not item.pk or item.pk in seen_pks:
                continue
            path = await self.stories.download(item, username)
            if path is None:
                logger.warning(
                    "Could not download story {} for @{}", item.pk, username
                )
                if account_id is not None:
                    async with get_session() as session:
                        await crud.mark_story_seen(
                            session,
                            account_id=account_id,
                            story_pk=item.pk,
                            source=item.source,
                            highlight_id=item.highlight_id,
                            highlight_title=item.highlight_title,
                            media_type=item.media_type,
                            taken_at=item.taken_at,
                        )
                seen_pks.add(item.pk)
                continue

            if item.source == "highlight":
                caption = (
                    f"✨ <b>@{esc(username)}</b> — highlight: "
                    f"<b>{esc(item.highlight_title or '')}</b>"
                )
            elif item.source == "post":
                caption = f"🖼 <b>@{esc(username)}</b> — new post"
            else:
                caption = f"📖 <b>@{esc(username)}</b> — new story"

            if item.media_type == "video":
                ok = await self.notifier.send_video(
                    path, caption=caption, message_thread_id=message_thread_id
                )
            else:
                ok = await self.notifier.send_photo(
                    path, caption=caption, message_thread_id=message_thread_id
                )

            if ok:
                sent += 1
                if account_id is not None:
                    async with get_session() as session:
                        await crud.mark_story_seen(
                            session,
                            account_id=account_id,
                            story_pk=item.pk,
                            source=item.source,
                            highlight_id=item.highlight_id,
                            highlight_title=item.highlight_title,
                            media_type=item.media_type,
                            taken_at=item.taken_at,
                        )
                seen_pks.add(item.pk)
        return sent

    async def _handle_new_posts(
        self,
        account_id: int,
        username: str,
        changeset: ChangeSet,
        *,
        first_seen: bool,
    ) -> None:
        """Download and send new feed posts/reels when the post/reel count rises.

        On the first observation we baseline the current grid (mark seen, don't
        send) so we don't dump a backlog; afterwards each increase delivers the
        new media. Login-free via saveinsta; degrades to nothing on failure.
        """
        if self.stories is None:
            return
        posts_change = changeset.find("posts_count")
        reels_change = changeset.find("reels_count")
        increased = bool(
            (posts_change and posts_change.new is not None
             and posts_change.old is not None and posts_change.new > posts_change.old)
            or (reels_change and reels_change.new is not None
                and reels_change.old is not None and reels_change.new > reels_change.old)
        )
        if not first_seen and not increased:
            return

        try:
            posts = await self.stories.fetch_posts(username)
        except Exception as exc:  # pragma: no cover - network failure path
            logger.warning("Post fetch failed for @{}: {}", username, exc)
            return
        if not posts:
            return

        if first_seen:
            async with get_session() as session:
                await crud.mark_story_items_seen(session, account_id, posts)
            logger.info(
                "Baselined {} post(s) for @{} (first observation)",
                len(posts), username,
            )
            return

        async with get_session() as session:
            seen_pks = await crud.get_seen_story_pks(session, account_id)
        new_posts = [p for p in posts if p.pk and p.pk not in seen_pks]
        if not new_posts:
            return
        new_posts = new_posts[:5]  # cap so a big jump never floods the chat
        noun = "post" if len(new_posts) == 1 else "posts"
        thread_id = await self.topic_for(account_id, username)
        await self.notifier.send_text(
            f"🖼 <b>@{esc(username)}</b> shared {len(new_posts)} new {noun}",
            message_thread_id=thread_id,
        )
        await self._deliver_story_items(
            account_id, username, new_posts, seen_pks, message_thread_id=thread_id
        )

    # ---------- Private → public backlog grab ----------

    @staticmethod
    def _public_grab_key(account_id: int) -> str:
        return f"public_grab_pending:{account_id}"

    async def _handle_public_backlog(
        self,
        account_id: int,
        username: str,
        instagram_id: Optional[str],
        *,
        went_public: bool,
    ) -> bool:
        """Drive the private→public backlog grab, with a small retry ledger.

        Returns True when it OWNS this account's media for this sweep (so the
        caller skips the normal story/highlight baseline, which would otherwise
        silently mark the backlog seen and lose it). Returns False when there's
        nothing to grab — no pending flag and not a transition — so the caller
        runs its normal phase.

        The transition is a one-shot event, but the grab can fail transiently
        (saveinsta rate-limited at that moment). A per-account pending flag,
        persisted BEFORE the long grab so an interruption is never lost, retries
        it on later sweeps until the sources answer — bounded by
        _PUBLIC_GRAB_MAX_ATTEMPTS so a genuinely empty account can't retry
        forever. "Answered" is the bar, not "delivered": a grab that listed the
        account and found nothing the chat hasn't already received is a
        success, not a reason to come back and list it again next sweep.
        """
        if self.stories is None or not settings.auto_grab_on_public:
            return False

        key = self._public_grab_key(account_id)
        async with get_session() as session:
            raw = await crud.get_setting(session, key)
        attempts = int(raw) if raw and raw.isdigit() else 0
        if went_public and attempts == 0:
            attempts = 1
        if attempts == 0:
            return False  # not pending and not a transition — normal phase

        if account_id in self._public_grabs_in_flight:
            # Another check (a manual Recheck mid-sweep, say) is already
            # grabbing this account. It owns the media; a second grab would
            # send everything twice.
            logger.info(
                "Backlog grab for @{} already in flight — not starting another",
                username,
            )
            return True

        # Persist the pending state before the (long, cancellable) grab so a
        # mid-grab cancellation or sweep timeout is retried, not silently lost.
        async with get_session() as session:
            await crud.set_setting(session, key, str(attempts))

        self._public_grabs_in_flight.add(account_id)
        try:
            result = await self.grab_public_backlog(
                account_id, username, instagram_id=instagram_id,
                final_attempt=attempts >= _PUBLIC_GRAB_MAX_ATTEMPTS,
                # A real transition always rides in on a change set that
                # contains is_private, so the profile card has already told the
                # chat it went public. A pending RETRY has no such card this
                # sweep, so there the grab is the only thing that can speak.
                transition_announced=went_public,
            )
        finally:
            self._public_grabs_in_flight.discard(account_id)

        # The sources answering at all — even with nothing new to send — is
        # what settles the transition. Only a grab that came back empty-handed
        # (rate-limited source, or an account with nothing on it) is retried.
        answered = result.get("fetched", 0) > 0 or result.get("total", 0) > 0
        cleared = answered or attempts >= _PUBLIC_GRAB_MAX_ATTEMPTS
        async with get_session() as session:
            if cleared:
                await crud.delete_setting(session, key)
            else:
                await crud.set_setting(session, key, str(attempts + 1))
        return True

    async def grab_public_backlog(
        self,
        account_id: int,
        username: str,
        *,
        instagram_id: Optional[str] = None,
        final_attempt: bool = False,
        transition_announced: bool = False,
    ) -> dict:
        """Send a newly-public account's backlog — everything the chat hasn't had.

        Posts/reels, highlight items and the current story are LISTED first,
        filtered against the account's seen-set, and only then delivered. So
        the first time an account opens up, all of it comes through; if it then
        flips private and public again (and again), only what is genuinely new
        since the last grab goes out — never the whole account over again. The
        on-demand download buttons deliberately ignore the seen-set, because
        someone pressing them is asking for a re-send; this path deliberately
        honors it, because nobody asked.

        Routed to the account's forum topic when topics are enabled, and
        cancellable with /kill. `final_attempt` only changes the wording when
        nothing could be listed (no "it'll retry" promise on the last try).
        `transition_announced` says the profile card has already reported the
        flip to the chat, so a grab with nothing new to send says nothing at
        all: an account toggling private/public costs ONE line, not two. It
        still speaks when something is delivered, or when the sources didn't
        answer and a retry is coming — silence there would hide real news.

        Returns {"posts", "highlights", "stories", "total", "fetched",
        "skipped"}: the three counts and `total` are what was SENT; `fetched`
        is how many items the sources listed (0 means nothing came back — an
        empty account, or the anonymous source is rate-limited — which is the
        caller's retry signal); `skipped` is how many listed items had already
        been delivered or baselined.
        """
        empty = {
            "posts": 0, "highlights": 0, "stories": 0,
            "total": 0, "fetched": 0, "skipped": 0,
        }
        if self.stories is None:
            return dict(empty)
        username = username.strip().lstrip("@").lower()
        thread_id = await self.topic_for(account_id, username)

        # One outer scope so /kill can stop the whole sequence between phases,
        # not just within one download (the scope nests safely).
        async with self.download_scope():
            # ---- list everything first; nothing is sent yet ----
            try:
                posts = await self.stories.fetch_posts(
                    username, limit=_PUBLIC_GRAB_POST_LIMIT
                )
            except Exception as exc:  # pragma: no cover - network failure path
                logger.warning(
                    "Backlog post listing failed for @{}: {}", username, exc
                )
                posts = []

            # list_highlights persists the catalog for monitored accounts, so
            # the normal story phase's next diff starts from what was seen here
            # instead of re-baselining it.
            listing = await self.list_highlights(username)
            catalog = {hid: title for hid, title in listing.get("items", [])}
            highlight_items = await self._gather_highlight_items(
                username, catalog, cancellable=True
            )
            if highlight_items:
                # Every reel was just listed — that IS the full re-scan, so
                # the normal phase needn't list them all again next sweep.
                async with get_session() as session:
                    await crud.set_setting(
                        session,
                        self._highlight_scan_key(account_id),
                        str(time.time()),
                    )

            try:
                stories = await self.stories.fetch_stories(username)
            except Exception as exc:  # pragma: no cover - network failure path
                logger.warning(
                    "Backlog story listing failed for @{}: {}", username, exc
                )
                stories = []

            fetched = sum(
                1 for item in (*posts, *highlight_items, *stories) if item.pk
            )

            if self._download_cancel.is_set():
                # /kill landed during the listing. The user stopped it on
                # purpose; report what was listed so the ledger doesn't
                # schedule a retry they didn't ask for.
                logger.info(
                    "/kill — backlog grab for @{} stopped before delivery",
                    username,
                )
                return {**empty, "fetched": fetched}

            # ---- keep only what the chat hasn't received ----
            async with get_session() as session:
                seen_pks = await crud.get_seen_story_pks(session, account_id)

            def unseen(items: list) -> list:
                return [i for i in items if i.pk and i.pk not in seen_pks]

            new_posts = unseen(posts)
            new_highlights = unseen(highlight_items)
            new_stories = unseen(stories)
            new_total = len(new_posts) + len(new_highlights) + len(new_stories)
            skipped = fetched - new_total

            if not fetched:
                if final_attempt:
                    msg = (
                        f"🔓 <b>@{esc(username)}</b> is public now, but nothing "
                        "could be grabbed (empty account, or the anonymous "
                        "source stayed rate-limited) — giving up on the "
                        "automatic grab; the Download all panel still works."
                    )
                else:
                    msg = (
                        f"🔓 <b>@{esc(username)}</b> is public now, but nothing "
                        "could be grabbed this time (empty account, or the "
                        "anonymous source is rate-limited — it'll retry)."
                    )
                await self.notifier.send_text(msg, message_thread_id=thread_id)
                logger.info("Public backlog for @{}: nothing listed", username)
                return dict(empty)

            if not new_total:
                # The flap case: it was public before, everything it has was
                # already delivered (or baselined) then. Nothing to send — and
                # when the profile card already announced the flip, nothing to
                # SAY either, so a rapid toggle costs one line per flip instead
                # of two. Only a flip nobody has reported yet gets a line here.
                if not transition_announced:
                    await self.notifier.send_text(
                        f"🔓 <b>@{esc(username)}</b> is PUBLIC again — nothing "
                        f"new to send: all {fetched} item(s) it has were "
                        "already delivered here (or baselined).",
                        message_thread_id=thread_id,
                    )
                logger.info(
                    "Public backlog for @{}: {} item(s) listed, all already "
                    "seen — {}",
                    username, fetched,
                    "staying quiet (the profile card announced the flip)"
                    if transition_announced else "reported to the chat",
                )
                return {**empty, "fetched": fetched, "skipped": skipped}

            # ---- deliver just the new items ----
            breakdown = (
                f"{len(new_posts)} post/reel, {len(new_highlights)} highlight, "
                f"{len(new_stories)} story item(s)"
            )
            if skipped:
                banner = (
                    f"🔓 <b>@{esc(username)}</b> is PUBLIC again — grabbing "
                    f"what's new since last time: {breakdown} "
                    f"({skipped} already delivered)…"
                )
            else:
                banner = (
                    f"🔓 <b>@{esc(username)}</b> just went PUBLIC — grabbing "
                    f"the whole account: {breakdown}…"
                )
            await self.notifier.send_text(banner, message_thread_id=thread_id)

            p = h = s = 0
            if new_posts:
                p = await self._deliver_story_items(
                    account_id, username, new_posts, seen_pks,
                    message_thread_id=thread_id, cancellable=True,
                )
            if new_highlights:
                h = await self._deliver_story_items(
                    account_id, username, new_highlights, seen_pks,
                    message_thread_id=thread_id, cancellable=True,
                )
            if new_stories:
                s = await self._deliver_story_items(
                    account_id, username, new_stories, seen_pks,
                    message_thread_id=thread_id, cancellable=True,
                )

        total = p + h + s
        summary = (
            f"✅ <b>@{esc(username)}</b> backlog grabbed — "
            f"{p} post/reel, {h} highlight, {s} story item(s)."
        )
        missed = new_total - total
        if missed:
            summary += f" {missed} item(s) not sent (download failed or stopped)."
        await self.notifier.send_text(summary, message_thread_id=thread_id)
        logger.info(
            "Public backlog for @{}: {} post/reel, {} highlight, {} story "
            "item(s) sent; {} listed, {} already seen, {} not sent",
            username, p, h, s, fetched, skipped, missed,
        )
        return {
            "posts": p, "highlights": h, "stories": s,
            "total": total, "fetched": fetched, "skipped": skipped,
        }

    # ---------- On-demand actions ----------
    # These work for ANY public username, monitored or not. When the account is
    # not monitored, account_id is None: media is still fetched and sent, but
    # nothing is persisted (no snapshot, no seen-dedup row).

    async def fetch_and_send_stories(
        self,
        username: str,
        *,
        instagram_id: Optional[str] = None,
        message_thread_id: Optional[int] = None,
    ) -> dict:
        """Download every current story item for a public account and send them now.

        Works for any public username. Unlike the sweep path this ignores the
        seen-deduplication set so the user always receives whatever is live at
        the moment they ask. For monitored accounts the items are recorded as
        seen afterwards so the next sweep won't re-send them.
        Pass `instagram_id` when it's already known (e.g. from the bulk-download
        panel) to skip the profile fetch — Instagram's web API rate-limits to
        401 quickly on datacenter IPs, so every avoided call matters.
        `message_thread_id` routes media to a per-account forum topic (used by
        the private→public backlog grab); on-demand callers leave it None.
        Returns {"ok": bool, "count": int, "error": Optional[str]}.
        """
        if self.stories is None:
            return {"ok": False, "count": 0, "error": "Stories client unavailable"}
        username = username.strip().lstrip("@").lower()
        async with get_session() as session:
            account = await crud.get_account(session, username)
        account_id = account.id if account else None
        instagram_id = instagram_id or (account.instagram_id if account else None)

        # Distinguish "no active story" (a real, anonymous-knowable state) from
        # "there is a story but we can't fetch the media". The reel query tells us
        # has_public_story without any login; resolve the id on the fly for
        # non-monitored usernames that don't have one stored.
        if not instagram_id:
            fetch = await self.instagram.fetch_profile(username)
            if fetch.success and fetch.parsed:
                instagram_id = fetch.parsed.get("instagram_id")
        has_story: Optional[bool] = None
        if instagram_id:
            try:
                reel_user = await self.instagram.fetch_reel_user(str(instagram_id))
                if reel_user is not None:
                    has_story = bool(reel_user.get("has_public_story"))
            except Exception:  # pragma: no cover - network failure path
                has_story = None

        # Anonymous media fetch (no login) via saveinsta.to.
        try:
            stories = await self.stories.fetch_stories(username)
        except Exception as exc:  # pragma: no cover - network failure path
            logger.warning("On-demand story fetch failed for @{}: {}", username, exc)
            stories = []

        if stories:
            async with self.download_scope():
                sent = await self._deliver_story_items(
                    account_id, username, stories, set(),
                    message_thread_id=message_thread_id, cancellable=True,
                )
            return {"ok": True, "count": sent, "error": None}

        if has_story is False:
            # Genuinely no active story right now.
            return {"ok": True, "count": 0, "error": None}
        # Either there IS a story we can't fetch anonymously, or status unknown.
        return {"ok": False, "count": 0, "error": _DOWNLOAD_UNAVAILABLE_MSG}

    async def fetch_and_send_story_url(
        self, username: str, story_url: str, *, pk: Optional[str] = None
    ) -> dict:
        """Download the single story item behind a direct story link and send it.

        `story_url` is a full instagram.com/stories/<username>/<pk>/ permalink.
        The exact URL is handed to saveinsta so only that item comes back; when
        the source returns more than one and the URL's numeric `pk` matches a
        returned item, the result is narrowed to just that story. Works for any
        public account; monitored accounts still record the item as seen so the
        next sweep won't re-send it.
        Returns {"ok": bool, "count": int, "error": Optional[str]}.
        """
        if self.stories is None:
            return {"ok": False, "count": 0, "error": "Stories client unavailable"}
        username = username.strip().lstrip("@").lower()
        async with get_session() as session:
            account = await crud.get_account(session, username)
        account_id = account.id if account else None

        try:
            items = await self.stories.fetch_story_by_url(story_url)
        except Exception as exc:  # pragma: no cover - network failure path
            logger.warning("Direct story fetch failed for {}: {}", story_url, exc)
            items = []

        # Narrow to the requested item when its pk is present; otherwise deliver
        # whatever the permalink resolved to (already just that story in the
        # common case). saveinsta derives pk from the CDN filename's media id,
        # which matches the pk in the story URL when both are available.
        if pk and items:
            matched = [it for it in items if it.pk == pk]
            if matched:
                items = matched

        if not items:
            return {"ok": False, "count": 0, "error": _DOWNLOAD_UNAVAILABLE_MSG}

        async with self.download_scope():
            sent = await self._deliver_story_items(
                account_id, username, items, set(), cancellable=True
            )
        return {"ok": True, "count": sent, "error": None}

    async def list_highlights(self, username: str) -> dict:
        """Return the current highlight reels (id + title) for any public account.

        Pulls the live catalog from Instagram's anonymous graphql reel query. For
        monitored accounts it also refreshes the stored catalog and falls back to
        the last stored catalog if the live fetch yields nothing.
        Returns {"ok": bool, "items": list[(id, title)], "error": Optional[str]}.
        """
        username = username.strip().lstrip("@").lower()
        async with get_session() as session:
            account = await crud.get_account(session, username)
        account_id = account.id if account else None
        instagram_id = account.instagram_id if account else None

        # The highlight catalog (names + ids) comes from Instagram's own graphql
        # reel query, which works anonymously (the id is resolved from the
        # username inside _fetch_highlight_catalog when not already known).
        catalog: dict[str, str] = {}
        try:
            catalog = await self._fetch_highlight_catalog(username, instagram_id)
        except Exception as exc:  # pragma: no cover - network failure path
            logger.warning("On-demand highlight catalog failed for @{}: {}", username, exc)
            catalog = {}

        # Persist/fallback only makes sense for monitored accounts.
        if catalog and account_id is not None:
            async with get_session() as session:
                await crud.replace_highlight_catalog(session, account_id, catalog)
        elif not catalog and account_id is not None:
            async with get_session() as session:
                catalog = await crud.get_highlight_catalog(session, account_id)

        # Mute state only exists for monitored accounts (it lives on the
        # stored catalog rows, which non-monitored lookups don't have).
        untracked: set[str] = set()
        if account_id is not None:
            async with get_session() as session:
                untracked = await crud.get_untracked_highlight_ids(
                    session, account_id
                )

        items = sorted(catalog.items(), key=lambda kv: kv[0])
        return {
            "ok": True,
            "items": items,
            "untracked": untracked,
            "monitored": account_id is not None,
            "error": None,
        }

    async def download_highlight(self, username: str, index: int) -> dict:
        """Download and send one highlight reel, identified by its list index.

        The index refers to the ordering returned by `list_highlights`, which is
        recomputed here so the bot doesn't have to pack a (colon-containing)
        highlight id into Telegram's 64-byte callback budget.
        Returns {"ok": bool, "count": int, "title": Optional[str], "error": Optional[str]}.
        """
        if self.stories is None:
            return {"ok": False, "count": 0, "title": None, "error": "Stories client unavailable"}
        username = username.strip().lstrip("@").lower()
        async with get_session() as session:
            account = await crud.get_account(session, username)
        account_id = account.id if account else None

        listing = await self.list_highlights(username)
        items = listing["items"]
        if index < 0 or index >= len(items):
            return {
                "ok": False, "count": 0, "title": None,
                "error": "That highlight is no longer available — refresh the list.",
            }

        highlight_id, title = items[index]
        # Anonymous media fetch (no login) via saveinsta.to.
        try:
            story_items = await self.stories.fetch_highlight_items(
                username, highlight_id, title
            )
        except Exception as exc:  # pragma: no cover - network failure path
            logger.warning(
                "On-demand highlight download failed for @{} ({}): {}",
                username, highlight_id, exc,
            )
            story_items = []

        if not story_items:
            return {"ok": False, "count": 0, "title": title, "error": _DOWNLOAD_UNAVAILABLE_MSG}

        async with self.download_scope():
            sent = await self._deliver_story_items(
                account_id, username, story_items, set(), cancellable=True
            )
        return {"ok": True, "count": sent, "title": title, "error": None}

    async def download_all_highlights(
        self, username: str, *, message_thread_id: Optional[int] = None
    ) -> dict:
        """Download and send every highlight reel for any public account at once.

        `message_thread_id` routes media to a per-account forum topic (used by
        the private→public backlog grab); on-demand callers leave it None.
        Returns {"ok": bool, "count": int, "reels": int, "error": Optional[str]}
        where count is the total media items sent and reels the number of reels.
        """
        if self.stories is None:
            return {"ok": False, "count": 0, "reels": 0, "error": "Stories client unavailable"}
        username = username.strip().lstrip("@").lower()
        async with get_session() as session:
            account = await crud.get_account(session, username)
        account_id = account.id if account else None

        listing = await self.list_highlights(username)
        items = listing.get("items", [])
        if not items:
            return {"ok": True, "count": 0, "reels": 0, "error": None}

        catalog = {hid: title for hid, title in items}
        async with self.download_scope():
            story_items = await self._gather_highlight_items(
                username, catalog, cancellable=True
            )
            if not story_items:
                return {
                    "ok": False, "count": 0, "reels": len(items),
                    "error": _DOWNLOAD_UNAVAILABLE_MSG,
                }
            sent = await self._deliver_story_items(
                account_id, username, story_items, set(),
                message_thread_id=message_thread_id, cancellable=True,
            )
        return {"ok": True, "count": sent, "reels": len(items), "error": None}

    async def download_highlights_from_catalog(
        self, username: str, catalog: dict[str, str]
    ) -> dict:
        """Download and send specific highlight reels from a known catalog.

        `catalog` is {highlight_id: title}, e.g. the (id, title) pairs the
        bulk-download panel already fetched and showed the user. The media
        comes straight from saveinsta by highlight id — no Instagram web/graphql
        call happens here, so this still works when Instagram is 401-blocking
        the datacenter IP (which is what list-based re-resolution dies on).
        Returns {"ok", "count", "reels", "error"}.
        """
        if self.stories is None:
            return {"ok": False, "count": 0, "reels": 0, "error": "Stories client unavailable"}
        if not catalog:
            return {
                "ok": False, "count": 0, "reels": 0,
                "error": "Those highlights are no longer available — refresh the list.",
            }
        username = username.strip().lstrip("@").lower()
        async with get_session() as session:
            account = await crud.get_account(session, username)
        account_id = account.id if account else None

        async with self.download_scope():
            story_items = await self._gather_highlight_items(
                username, catalog, cancellable=True
            )
            if not story_items:
                return {
                    "ok": False, "count": 0, "reels": len(catalog),
                    "error": _DOWNLOAD_UNAVAILABLE_MSG,
                }
            sent = await self._deliver_story_items(
                account_id, username, story_items, set(), cancellable=True
            )
        return {"ok": True, "count": sent, "reels": len(catalog), "error": None}

    async def download_posts(
        self,
        username: str,
        *,
        photos: bool = True,
        videos: bool = True,
        limit: int = 100,
        message_thread_id: Optional[int] = None,
    ) -> dict:
        """Download and send the account's feed grid media (login-free).

        saveinsta's profile listing serves the post/reel grid at full
        resolution; `photos` keeps image posts, `videos` keeps video posts and
        reels. Like the other on-demand paths this ignores seen-dedup so the
        user always gets the media, but monitored accounts still get the items
        marked seen so the sweep won't re-send them.
        `message_thread_id` routes media to a per-account forum topic (used by
        the private→public backlog grab); on-demand callers leave it None.
        Returns {"ok", "count", "photos", "videos", "error"}.
        """
        if self.stories is None:
            return {
                "ok": False, "count": 0, "photos": 0, "videos": 0,
                "error": "Stories client unavailable",
            }
        username = username.strip().lstrip("@").lower()
        async with get_session() as session:
            account = await crud.get_account(session, username)
        account_id = account.id if account else None

        try:
            posts = await self.stories.fetch_posts(username, limit=limit)
        except Exception as exc:  # pragma: no cover - network failure path
            logger.warning("On-demand post fetch failed for @{}: {}", username, exc)
            posts = []
        if not posts:
            return {
                "ok": False, "count": 0, "photos": 0, "videos": 0,
                "error": (
                    "No posts found — the account may have none, be private, "
                    "or the anonymous source is rate-limited."
                ),
            }

        photo_items = [p for p in posts if p.media_type != "video"] if photos else []
        video_items = [p for p in posts if p.media_type == "video"] if videos else []

        sent_photos = 0
        sent_videos = 0
        async with self.download_scope():
            if photo_items:
                sent_photos = await self._deliver_story_items(
                    account_id, username, photo_items, set(),
                    message_thread_id=message_thread_id, cancellable=True,
                )
            if video_items:
                sent_videos = await self._deliver_story_items(
                    account_id, username, video_items, set(),
                    message_thread_id=message_thread_id, cancellable=True,
                )

        return {
            "ok": True,
            "count": sent_photos + sent_videos,
            "photos": sent_photos,
            "videos": sent_videos,
            "error": None,
        }

    async def fetch_and_send_profile_picture(self, username: str) -> dict:
        """Fetch the current profile picture (best quality) and send it now.

        Same fetch path as fetch_profile_picture, but delivery happens here via
        the notifier so bulk flows don't have to handle the file themselves.
        Returns {"ok", "hd", "error"}.
        """
        username = username.strip().lstrip("@").lower()
        result = await self.fetch_profile_picture(username)
        if not result.get("ok"):
            return {"ok": False, "hd": False, "error": result.get("error")}
        quality = "HD" if result.get("hd") else "320px (anonymous max)"
        caption = (
            f"👤 <b>@{esc(username)}</b> — profile picture · {quality}\n"
            f"SHA256: <code>{esc(result['sha256'])}</code>"
        )
        ok = await self.notifier.send_document(result["path"], caption=caption)
        return {
            "ok": ok,
            "hd": bool(result.get("hd")),
            "error": None if ok else "Telegram send failed",
        }

    async def get_download_overview(self, username: str) -> dict:
        """Profile basics + highlight catalog for the bulk-download panel.

        One profile fetch (existence, privacy, post count, numeric id, and the
        highlight *count*) plus the anonymous highlight catalog. Mirrors
        list_highlights' persist/fallback behavior for monitored accounts so the
        two stay consistent — the items ordering here matches what the
        download-by-index methods recompute.

        `highlight_count` comes from web_profile_info (which works even on
        datacenter IPs), so we can tell the user how many highlights exist even
        when the catalog itself (ids + titles) can't be listed because the
        graphql reel query is 401-blocked from this server. Returns
        {"ok", "items", "monitored", "is_private", "posts_count",
        "instagram_id", "highlight_count", "error"}.
        """
        username = username.strip().lstrip("@").lower()
        async with get_session() as session:
            account = await crud.get_account(session, username)
        account_id = account.id if account else None
        instagram_id = account.instagram_id if account else None

        is_private: Optional[bool] = None
        posts_count: Optional[int] = None
        highlight_count: Optional[int] = None
        fetch = await self.instagram.fetch_profile(username)
        if fetch.success and fetch.parsed:
            parsed = fetch.parsed
            is_private = bool(parsed.get("is_private"))
            posts_count = parsed.get("posts_count")
            highlight_count = parsed.get("story_count")  # = highlight_reel_count
            instagram_id = instagram_id or parsed.get("instagram_id")
        elif fetch.http_status == 404:
            return {
                "ok": False, "items": [], "monitored": account_id is not None,
                "is_private": None, "posts_count": None, "instagram_id": None,
                "highlight_count": None,
                "error": f"@{username} doesn't exist (HTTP 404).",
            }
        # Other fetch failures are non-fatal: the panel still works, we just
        # don't know privacy/post count.

        catalog: dict[str, str] = {}
        try:
            catalog = await self._fetch_highlight_catalog(username, instagram_id)
        except Exception as exc:  # pragma: no cover - network failure path
            logger.warning(
                "Bulk-download highlight catalog failed for @{}: {}", username, exc
            )
            catalog = {}
        if catalog and account_id is not None:
            async with get_session() as session:
                await crud.replace_highlight_catalog(session, account_id, catalog)
        elif not catalog and account_id is not None:
            async with get_session() as session:
                catalog = await crud.get_highlight_catalog(session, account_id)

        items = sorted(catalog.items(), key=lambda kv: kv[0])
        # If we couldn't list the catalog but know the count, surface the count
        # (capped to ≥ the listed items, which can't exceed the real total).
        if highlight_count is None or highlight_count < len(items):
            highlight_count = len(items)
        return {
            "ok": True,
            "items": items,
            "monitored": account_id is not None,
            "is_private": is_private,
            "posts_count": posts_count,
            "instagram_id": instagram_id,
            "highlight_count": highlight_count,
            "error": None,
        }

    async def toggle_highlight_tracking(self, username: str, index: int) -> dict:
        """Flip the sweep auto-download mute for one highlight (by list index).

        Muting keeps the highlight in the catalog (renames/removals still get
        detected) but the sweep stops fetching its media. Unmuting first marks
        the highlight's current items as seen WITHOUT sending them, so tracking
        resumes from now instead of dumping everything posted while muted.
        Returns {"ok", "title", "tracked", "error"}.
        """
        username = username.strip().lstrip("@").lower()
        async with get_session() as session:
            account = await crud.get_account(session, username)
        if account is None:
            return {
                "ok": False, "title": None, "tracked": None,
                "error": "Only monitored accounts can mute highlights.",
            }

        listing = await self.list_highlights(username)
        items = listing["items"]
        if index < 0 or index >= len(items):
            return {
                "ok": False, "title": None, "tracked": None,
                "error": "That highlight is no longer available — refresh the list.",
            }
        highlight_id, title = items[index]
        tracked = highlight_id in listing["untracked"]  # flip: muted -> track

        if tracked and self.stories is not None:
            try:
                story_items = await self.stories.fetch_highlight_items(
                    username, highlight_id, title
                )
                async with get_session() as session:
                    await crud.mark_story_items_seen(
                        session, account.id, story_items
                    )
            except Exception as exc:  # pragma: no cover - network failure path
                logger.debug(
                    "Unmute re-baseline failed for @{} ({}): {}",
                    username, highlight_id, exc,
                )

        async with get_session() as session:
            ok = await crud.set_highlight_tracked(
                session, account.id, highlight_id, tracked
            )
        if not ok:
            return {
                "ok": False, "title": title, "tracked": None,
                "error": "Highlight not stored yet — refresh the list and retry.",
            }
        return {"ok": True, "title": title, "tracked": tracked, "error": None}

    async def set_all_highlight_tracking(self, username: str, tracked: bool) -> dict:
        """Mute or unmute sweep auto-download for ALL of an account's highlights.

        Unmuting re-baselines every reel first (items posted while muted are
        marked seen, not sent). Returns {"ok", "count", "error"}.
        """
        username = username.strip().lstrip("@").lower()
        async with get_session() as session:
            account = await crud.get_account(session, username)
        if account is None:
            return {
                "ok": False, "count": 0,
                "error": "Only monitored accounts can mute highlights.",
            }

        if tracked and self.stories is not None:
            async with get_session() as session:
                catalog = await crud.get_highlight_catalog(session, account.id)
            if catalog:
                story_items = await self._gather_highlight_items(username, catalog)
                async with get_session() as session:
                    await crud.mark_story_items_seen(
                        session, account.id, story_items
                    )

        async with get_session() as session:
            count = await crud.set_all_highlights_tracked(
                session, account.id, tracked
            )
        return {"ok": True, "count": count, "error": None}

    async def fetch_profile_picture(self, username: str) -> dict:
        """Download the CURRENT profile picture at the best available quality.

        Login-free, works for any username. Prefers the HD (up to 1080px) avatar
        from saveinsta; falls back to the web profile_pic_url_hd (320px, the
        anonymous ceiling for accounts saveinsta can't reach, e.g. private ones).
        Returns {"ok", "path", "sha256", "byte_size", "hd", "error"}.
        """
        username = username.strip().lstrip("@").lower()

        # Try the login-free HD avatar (saveinsta) FIRST — it needs no Instagram
        # call at all. The Instagram web fetch only happens as a fallback, since
        # datacenter IPs get 401-rate-limited after a handful of requests and a
        # bulk download must not burn one of those on a picture saveinsta serves.
        hd_url: Optional[str] = None
        if self.stories is not None:
            try:
                hd_url = await self.stories.fetch_profile_pic_url(username)
            except Exception as exc:  # pragma: no cover - network failure path
                logger.debug("HD profile pic fetch failed for @{}: {}", username, exc)
                hd_url = None

        hashed: Optional[HashedMedia] = None
        if hd_url:
            hashed = await self.hasher.hash_url(hd_url, username)

        if hashed is None:
            # No HD avatar (private account / saveinsta down) or its download
            # failed mid-flight — fall back to the web profile_pic_url_hd (320px).
            fetch = await self.instagram.fetch_profile(username)
            if not fetch.success or not fetch.parsed:
                return {
                    "ok": False, "path": None,
                    "error": fetch.error or f"HTTP {fetch.http_status}",
                }
            web_url = fetch.parsed.get("profile_pic_url")  # already hd(320) or 150
            if not web_url:
                return {"ok": False, "path": None, "error": "No profile picture available"}
            hd_url = None  # what we deliver below is the web fallback, not HD
            hashed = await self.hasher.hash_url(web_url, username)

        if hashed is None:
            return {"ok": False, "path": None, "error": "Failed to download profile picture"}

        return {
            "ok": True,
            "path": hashed.local_path,
            "sha256": hashed.sha256,
            "byte_size": hashed.byte_size,
            "hd": bool(hd_url),
            "error": None,
        }
