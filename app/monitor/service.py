"""High-level orchestration: fetch -> hash -> diff -> persist -> notify."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.bot.notifications import (
    NotificationDispatcher,
    render_changes_message,
    render_failure_message,
    render_highlight_catalog_changes,
    render_new_stories_alert,
)
from app.config import settings
from app.database import crud
from app.database.models import AccountSnapshot, MonitoredAccount, ProfileMediaHash
from app.database.session import get_session
from app.monitor.change_detector import ChangeSet, detect_changes
from app.monitor.instagram import InstagramClient, ProfileFetchResult, extract_instagram_id
from app.monitor.media_hasher import HashedMedia, MediaHasher
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

# Seconds between sweep launch starts. Firing every account at once is the
# main 401 trigger — Instagram rate-limits the proxy egress on bursts, and a
# blocked call retries inside the worker, snowballing the blocked traffic.
_SWEEP_STAGGER_SECONDS = 2.0
# Cooldown before re-checking accounts that hit a rate-limit block during the
# sweep. Instagram's anonymous throttle windows are short — by the time the
# story phase plus this pause have run, a retry usually goes through.
_SWEEP_RETRY_COOLDOWN_SECONDS = 30.0
# Fetch statuses worth a second pass: rate-limit blocks and network timeouts.
# 404s are handled by the rename-recovery path, not by retrying.
_RETRIABLE_STATUSES = (401, 403, 429, 0)


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
        # account_id -> forum topic (message_thread_id). Resolved lazily and
        # cached so each account's alerts land in its own thread.
        self._topic_cache: dict[int, int] = {}
        # Latched True once topic creation fails (chat isn't a forum / no
        # manage-topics right) so we don't re-attempt on every message.
        self._topics_unavailable: bool = False

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

        result = await self._run_check(
            account_id, username, notify_unchanged=notify_unchanged
        )

        if self.stories is not None and result.get("ok"):
            meta = await self._load_account_story_meta(account_id)
            is_private = result.get("is_private")
            if is_private is None:
                is_private = meta["is_private"]
            if not is_private:
                await self._check_stories_and_highlights(
                    account_id,
                    result.get("username", username),
                    instagram_id=result.get("instagram_id") or meta["instagram_id"],
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
        noun = "profile" if len(targets) == 1 else "profiles"
        await self.notifier.send_text(
            f"👁 Sweep started — {len(targets)} {noun} queued."
        )
        results = await asyncio.gather(
            *(
                self._staggered_check(i, aid, uname)
                for i, (aid, uname) in enumerate(targets)
            ),
            return_exceptions=True,
        )

        # account_id -> (fallback username, result dict). Exceptions become
        # failure dicts (flagged "crashed") so the retry pass can rewrite any
        # entry and the final stats fall out of one structure.
        outcomes: list[tuple[int, str, dict]] = []
        story_targets: list[tuple[int, str, Optional[str]]] = []
        for (target_account_id, uname), r in zip(targets, results):
            if isinstance(r, Exception):
                logger.exception("Unhandled error during sweep: {}", r)
                r = {"ok": False, "username": uname, "error": repr(r), "crashed": True}
            outcomes.append((target_account_id, uname, r))
            if r.get("crashed"):
                continue
            result_username = r.get("username", uname)
            meta = await self._load_account_story_meta(target_account_id)
            is_private = r.get("is_private")
            if is_private is None:
                is_private = meta["is_private"]
            else:
                is_private = bool(is_private)
            instagram_id = r.get("instagram_id") or meta["instagram_id"]
            if not is_private:
                story_targets.append(
                    (target_account_id, result_username, instagram_id)
                )

        if self.stories is not None and story_targets:
            await asyncio.gather(
                *(
                    self._check_stories_and_highlights(
                        aid, uname, instagram_id=ig_id
                    )
                    for aid, uname, ig_id in story_targets
                ),
                return_exceptions=True,
            )

        # Second pass: accounts that hit a rate-limit block get one more
        # chance after a cooldown (the story phase above already added some).
        # The throttle is transient — a paced sequential retry usually
        # succeeds, so the sweep summary stops reporting phantom failures.
        retriable = [
            (idx, aid, uname)
            for idx, (aid, uname, r) in enumerate(outcomes)
            if not r.get("ok") and r.get("status") in _RETRIABLE_STATUSES
        ]
        if retriable:
            logger.info(
                "Retrying {} rate-limited account(s) after a {:.0f}s cooldown",
                len(retriable), _SWEEP_RETRY_COOLDOWN_SECONDS,
            )
            await asyncio.sleep(_SWEEP_RETRY_COOLDOWN_SECONDS)
            for idx, aid, uname in retriable:
                retry = await self._run_check(aid, uname)
                if retry.get("ok"):
                    outcomes[idx] = (aid, uname, retry)
                await asyncio.sleep(random.uniform(2.0, 5.0))

        checked = sum(1 for _, _, r in outcomes if not r.get("crashed"))
        changed = sum(1 for _, _, r in outcomes if r.get("changed"))
        failed_usernames = [
            r.get("username", uname)
            for _, uname, r in outcomes
            if not r.get("ok")
        ]
        failed = len(failed_usernames)

        logger.info(
            "Sweep done: checked={}, changed={}, failed={}", checked, changed, failed
        )

        # Went-dark radar: flag targets that have posted nothing for a while.
        # Runs after the story phase so this sweep's fresh activity is counted.
        try:
            await self._check_dark_radar()
        except Exception as exc:  # pragma: no cover - never sink a sweep on this
            logger.exception("Dark-radar check failed: {}", exc)

        noun = "profile" if checked == 1 else "profiles"
        summary = f"👁 Sweep complete — {checked} {noun} checked."
        if failed:
            names = ", ".join(f"@{u}" for u in failed_usernames)
            summary += f" {failed} failed: {names}"
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

        result = {"checked": checked, "changed": changed, "failed": failed}
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

        async with get_session() as session:
            accounts = await crud.list_accounts(session, only_active=True)

        for account in accounts:
            async with get_session() as session:
                last = await crud.last_activity_at(session, account.id)
                state_key = self._dark_state_key(account.id)
                currently_flagged = (
                    await crud.get_setting(session, state_key)
                ) is not None
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
            rows = []
            for account in accounts:
                last = await crud.last_activity_at(session, account.id)
                rows.append((account.username, last))
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

    async def _staggered_check(
        self, index: int, account_id: int, username: str
    ) -> dict:
        """Run one sweep check after a position-based delay.

        Spreads the sweep's Instagram traffic over ~2s per account instead of
        bursting everything at once — bursts are what trip Instagram's
        anonymous rate limiter into 401s on the shared proxy egress.
        """
        if index:
            await asyncio.sleep(
                index * _SWEEP_STAGGER_SECONDS + random.uniform(0.0, 0.8)
            )
        return await self._run_check(account_id, username)

    async def _run_check(
        self, account_id: int, username: str, *, notify_unchanged: bool = False
    ) -> dict:
        async with self._semaphore:
            try:
                return await self._do_check(account_id, username, notify_unchanged)
            except Exception as exc:
                logger.exception("Unhandled error checking @{}: {}", username, exc)
                return {"ok": False, "username": username, "error": repr(exc)}

    async def _do_check(
        self, account_id: int, username: str, notify_unchanged: bool
    ) -> dict:
        logger.info("Checking @{}", username)
        fetch = await self.instagram.fetch_profile(username)

        if not fetch.success:
            if fetch.http_status == 404:
                recovered = await self._recover_after_404(
                    account_id, username, notify_unchanged
                )
                if recovered is not None:
                    return recovered
            return await self._handle_failure(account_id, username, fetch)

        return await self._handle_success(account_id, username, fetch, notify_unchanged)

    async def _recover_after_404(
        self, account_id: int, username: str, notify_unchanged: bool
    ) -> Optional[dict]:
        async with get_session() as session:
            account = await session.get(MonitoredAccount, account_id)
            instagram_id = account.instagram_id if account else None
            if not instagram_id:
                previous = await crud.get_latest_snapshot(session, account_id)
                raw_response = previous.raw_response if previous else None
                instagram_id = self._extract_instagram_id(raw_response)
                if instagram_id and account is not None:
                    account.instagram_id = instagram_id
                    await session.flush()  # Persist extracted ID immediately
                    logger.info(
                        "Extracted and stored Instagram ID from previous snapshot for @{}: {}",
                        account.username,
                        instagram_id,
                    )

        if not instagram_id:
            logger.warning(
                "Cannot recover @{} after 404: no Instagram ID stored or found in snapshots",
                username,
            )
            return None

        logger.info(
            "Attempting to recover @{} using stored Instagram ID: {}",
            username,
            instagram_id,
        )
        new_username = await self.instagram.fetch_username_by_id(str(instagram_id))
        if not new_username:
            logger.warning(
                "Could not resolve current username for @{} using id={}",
                username,
                instagram_id,
            )
            return None
        if new_username == username:
            logger.info(
                "Username lookup for id={} still resolves to @{} after 404",
                instagram_id,
                username,
            )
            return None

        logger.info(
            "Successfully recovered renamed account (id={}): @{} -> @{}",
            instagram_id,
            username,
            new_username,
        )
        retry = await self.instagram.fetch_profile(new_username)
        if not retry.success:
            logger.warning(
                "Recovered username @{} for id={} but profile fetch failed: status={} error={}",
                new_username,
                instagram_id,
                retry.http_status,
                retry.error,
            )
            return None
        result = await self._handle_success(
            account_id, new_username, retry, notify_unchanged
        )
        result["recovered_from_username"] = username
        return result

    @staticmethod
    def _extract_instagram_id(raw_response: Optional[dict]) -> Optional[str]:
        return extract_instagram_id(raw_response)

    async def _handle_failure(
        self, account_id: int, username: str, fetch: ProfileFetchResult
    ) -> dict:
        logger.warning(
            "Fetch failed for @{}: status={} error={}",
            username, fetch.http_status, fetch.error,
        )

        async with get_session() as session:
            if fetch.http_status in (401, 404):
                return {
                    "ok": False,
                    "username": username,
                    "status": fetch.http_status,
                    "error": fetch.error,
                }

            # Only store a failure snapshot when transitioning from success
            # (i.e. the previous snapshot was OK). Repeated identical failures
            # are not stored — they add no information.
            previous = await crud.get_latest_snapshot(session, account_id, successful_only=False)
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
        should_notify = failure_count == 1 or failure_count % 5 == 0
        if should_notify:
            msg = render_failure_message(username, fetch)
            delivered = await self.notifier.send_text(msg)
            async with get_session() as session:
                await crud.log_notification(
                    session,
                    account_id=account_id,
                    change_type="fetch_failure",
                    payload={
                        "status": fetch.http_status,
                        "error": fetch.error,
                        "consecutive_failures": failure_count,
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
    ) -> dict:
        assert fetch.parsed is not None
        parsed = fetch.parsed

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

        hashed: Optional[HashedMedia] = None
        if pic_url:
            hashed = await self.hasher.hash_url(pic_url, username)

        new_pic_hash = hashed.sha256 if hashed else None

        # For public accounts with instagram_id, fetch reel data (stories/highlights/live status)
        # This will be stored in the snapshot for future reference
        reel_data_response = None
        if not parsed.get("is_private") and instagram_id:
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

        # Persist only what later reads actually consume: the numeric user id
        # (404 recovery / ID backfill) and reel_data (story/live status and the
        # highlight catalog). The full web_profile_info payload is 50–200 KB per
        # snapshot and was the main thing filling the database — this slim form
        # is a few hundred bytes, so the 0.5 GB free tier effectively never
        # fills. Everything diffable already lives in the snapshot's columns.
        parsed_instagram_id = parsed.get("instagram_id") or self._extract_instagram_id(
            fetch.raw_response
        )
        slim_raw: dict = {}
        if parsed_instagram_id:
            slim_raw["data"] = {"user": {"id": str(parsed_instagram_id)}}
        if reel_data_response:
            slim_raw["reel_data"] = reel_data_response

        async with get_session() as session:
            previous = await crud.get_latest_snapshot(session, account_id)

            snapshot = AccountSnapshot(
                account_id=account_id,
                username=parsed.get("username") or username,
                full_name=parsed.get("full_name"),
                biography=parsed.get("biography"),
                followers_count=parsed.get("followers_count"),
                following_count=parsed.get("following_count"),
                posts_count=parsed.get("posts_count"),
                reels_count=parsed.get("reels_count"),
                story_count=parsed.get("story_count"),
                is_private=parsed.get("is_private"),
                is_verified=parsed.get("is_verified"),
                is_business=parsed.get("is_business"),
                profile_pic_url=parsed.get("profile_pic_url"),
                profile_pic_hash=new_pic_hash,
                external_url=parsed.get("external_url"),
                http_status=200,
                raw_response=slim_raw or None,
            )

            # Diff first, persist only when something actually changed.
            changeset = detect_changes(previous, snapshot, new_pic_hash=new_pic_hash)
            if previous is None or changeset.has_changes:
                await crud.insert_snapshot(session, snapshot)
                # Keep only the latest 200 snapshots per account
                await crud.cleanup_old_snapshots(session, account_id, keep_count=200)
            else:
                # Refresh in place with the slim form — the old code stored the
                # full payload here WITHOUT reel_data, so every unchanged sweep
                # both bloated the row and wiped the stored story/highlight
                # state. The slim form keeps reel_data current instead.
                previous.raw_response = slim_raw or None
                previous.profile_pic_url = parsed.get("profile_pic_url")
                previous.profile_pic_hash = new_pic_hash
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

            # Update Instagram ID & last-checked
            account = await session.get(MonitoredAccount, account_id)
            if account is not None:
                parsed_username = (parsed.get("username") or username).lower()
                # Store Instagram ID if account doesn't have one yet
                if parsed_instagram_id and not account.instagram_id:
                    account.instagram_id = str(parsed_instagram_id)
                    await session.flush()  # Ensure ID is persisted immediately
                    logger.info(
                        "Stored Instagram ID for @{}: {}",
                        account.username,
                        parsed_instagram_id,
                    )
                if parsed_username and parsed_username != account.username:
                    existing = await crud.get_account(session, parsed_username)
                    if existing is None or existing.id == account.id:
                        account.username = parsed_username
                        logger.info(
                            "Updated @{} to @{} via parsed response",
                            username,
                            parsed_username,
                        )
                    else:
                        logger.warning(
                            "Could not update @{} to @{}: username already monitored by account_id={}",
                            account.username,
                            parsed_username,
                            existing.id,
                        )
            
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
        if self.stories is not None and not parsed.get("is_private"):
            await self._handle_new_posts(
                account_id, username, changeset, first_seen=previous is None
            )

        stored_id = None
        async with get_session() as session:
            account_row = await session.get(MonitoredAccount, account_id)
            if account_row is not None:
                stored_id = account_row.instagram_id

        return {
            "ok": True,
            "username": username,
            "status": 200,
            "changed": changeset.has_changes,
            "change_count": len(changeset.changes) + (1 if changeset.profile_pic_changed else 0),
            "first_seen": previous is None,
            "is_private": bool(parsed.get("is_private")),
            "instagram_id": stored_id or parsed.get("instagram_id"),
        }

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

        # Profile picture sent as a document to preserve full quality
        if changeset.profile_pic_changed and new_pic_path is not None:
            caption = (
                f"<b>@{username}</b> changed profile picture\n"
                f"Old hash: <code>{changeset.old_pic_hash}</code>\n"
                f"New hash: <code>{changeset.new_pic_hash}</code>"
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
        self, username: str, catalog: dict[str, str]
    ) -> list:
        """Download story items across every highlight reel in the catalog.

        The reel ids come from Instagram's graphql query (anonymous); the media
        itself comes from saveinsta.to per reel. Failures on individual reels are
        swallowed so one bad reel never sinks the rest.
        """
        if self.stories is None or not catalog:
            return []
        results = await asyncio.gather(
            *(
                self.stories.fetch_highlight_items(username, hid, title)
                for hid, title in catalog.items()
            ),
            return_exceptions=True,
        )
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

    async def _check_stories_and_highlights(
        self,
        account_id: int,
        username: str,
        *,
        instagram_id: Optional[str] = None,
    ) -> None:
        """Stories, highlight catalog changes, and new highlight media for public accounts.
        
        Supports two methods for checking public accounts:
        1. User_id API (reel query) - preferred for public accounts with known instagram_id
           Returns: has_public_story, is_live, highlight catalog
           Fetched during profile check and stored in snapshot's raw_response["reel_data"]
        2. Fallback to stories API - when reel data is unavailable
        """
        assert self.stories is not None
        async with self._semaphore:
            try:
                async with get_session() as session:
                    previous_catalog = await crud.get_highlight_catalog(
                        session, account_id
                    )
                    previous_snapshot = await crud.get_latest_snapshot(
                        session, account_id, successful_only=True
                    )
                    seen_pks = await crud.get_seen_story_pks(session, account_id)

                # Route everything in this account's check to its own topic.
                thread_id = await self.topic_for(account_id, username)

                # Extract reel data from the latest snapshot (fetched during profile check)
                # Reel data is used ONLY for story/live status detection, not for highlight catalog
                reel_data = None
                if previous_snapshot and previous_snapshot.raw_response:
                    reel_data = previous_snapshot.raw_response.get("reel_data")
                
                # If reel_data is not in snapshot, try to fetch it now for story/live status
                if not reel_data and instagram_id:
                    reel_user = await self.instagram.fetch_reel_user(str(instagram_id))
                    if reel_user:
                        reel_data = {
                            "has_public_story": reel_user.get("has_public_story", False),
                            "is_live": reel_user.get("is_live", False),
                            "highlights": reel_user.get("highlights", {}),
                        }
                        logger.debug(
                            "Fetched reel data for @{} during story check (not in snapshot)",
                            username
                        )

                # Highlight catalog: the profile check already fetched it (it
                # rides on the same reel query as story/live status), so reuse
                # it rather than asking Instagram again — every avoided call
                # lowers the 401 rate. Only fetch when the snapshot predates
                # highlights being stored in reel_data.
                catalog = (reel_data or {}).get("highlights")
                if catalog is None:
                    catalog = await self._fetch_highlight_catalog(
                        username, instagram_id
                    )

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

                # Check story/live status using reel data (user_id API)
                # Always report current status every time sweep runs
                if reel_data:
                    has_public_story = reel_data.get("has_public_story", False)
                    is_live = reel_data.get("is_live", False)
                    
                    # Extract previous story/live status from the previous snapshot
                    prev_has_story = False
                    prev_is_live = False
                    if previous_snapshot and previous_snapshot.id:
                        # Get the snapshot before the current one
                        async with get_session() as session:
                            from sqlalchemy import select, desc
                            prev_snapshot_query = select(AccountSnapshot).where(
                                AccountSnapshot.account_id == account_id,
                                AccountSnapshot.id != previous_snapshot.id
                            ).order_by(desc(AccountSnapshot.created_at)).limit(1)
                            prev_snapshot_result = await session.execute(prev_snapshot_query)
                            prev_snapshot_older = prev_snapshot_result.scalar()
                            if prev_snapshot_older and prev_snapshot_older.raw_response:
                                prev_reel = prev_snapshot_older.raw_response.get("reel_data", {})
                                prev_has_story = prev_reel.get("has_public_story", False)
                                prev_is_live = prev_reel.get("is_live", False)
                    
                    # One status message per sweep, upgraded to a "just went
                    # live" / "just posted a story" alert only when the status
                    # actually changed since the previous sweep. While
                    # establishing the baseline there is no real prior state,
                    # so the "just …" wording is never used then.
                    just_live = (
                        is_live and not prev_is_live and not establishing_baseline
                    )
                    just_story = (
                        has_public_story
                        and not prev_has_story
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

                    delivered = await self.notifier.send_text(
                        msg, message_thread_id=thread_id
                    )
                    async with get_session() as session:
                        await crud.log_notification(
                            session,
                            account_id=account_id,
                            change_type=change_type,
                            payload={
                                "has_public_story": has_public_story,
                                "is_live": is_live,
                            },
                            message=msg,
                            delivered=delivered,
                        )

                # Fetch the actual story items to download (anonymous, no login,
                # via saveinsta.to). A dead/rate-limited source just yields [].
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
                    logger.info(
                        "Established story/highlight baseline for @{} ({} reels, {} items)",
                        username,
                        len(catalog),
                        len(stories) + len(highlight_items),
                    )
                    return

                if new_stories:
                    alert = render_new_stories_alert(username, len(new_stories))
                    await self.notifier.send_text(alert, message_thread_id=thread_id)
                    await self._deliver_story_items(
                        account_id, username, new_stories, seen_pks,
                        message_thread_id=thread_id,
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
                highlight_items = await self._gather_highlight_items(
                    username, tracked_catalog
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

    async def _deliver_story_items(
        self,
        account_id: Optional[int],
        username: str,
        items: list,
        seen_pks: set[str],
        *,
        message_thread_id: Optional[int] = None,
    ) -> int:
        """Download and send each item; record it as seen. Returns the number sent.

        `account_id` is None for ad-hoc fetches of accounts that aren't monitored
        (e.g. /story for any username) — in that case nothing is persisted as
        seen, since there's no account row to dedup against on later sweeps.
        `message_thread_id` routes the media to a per-account forum topic when
        set (sweep path); on-demand callers leave it None for the General thread.
        """
        assert self.stories is not None
        sent = 0
        for item in items:
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

    # ---------- On-demand actions ----------
    # These work for ANY public username, monitored or not. When the account is
    # not monitored, account_id is None: media is still fetched and sent, but
    # nothing is persisted (no snapshot, no seen-dedup row).

    async def fetch_and_send_stories(
        self, username: str, *, instagram_id: Optional[str] = None
    ) -> dict:
        """Download every current story item for a public account and send them now.

        Works for any public username. Unlike the sweep path this ignores the
        seen-deduplication set so the user always receives whatever is live at
        the moment they ask. For monitored accounts the items are recorded as
        seen afterwards so the next sweep won't re-send them.
        Pass `instagram_id` when it's already known (e.g. from the bulk-download
        panel) to skip the profile fetch — Instagram's web API rate-limits to
        401 quickly on datacenter IPs, so every avoided call matters.
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
            sent = await self._deliver_story_items(account_id, username, stories, set())
            return {"ok": True, "count": sent, "error": None}

        if has_story is False:
            # Genuinely no active story right now.
            return {"ok": True, "count": 0, "error": None}
        # Either there IS a story we can't fetch anonymously, or status unknown.
        return {"ok": False, "count": 0, "error": _DOWNLOAD_UNAVAILABLE_MSG}

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

        sent = await self._deliver_story_items(account_id, username, story_items, set())
        return {"ok": True, "count": sent, "title": title, "error": None}

    async def download_all_highlights(self, username: str) -> dict:
        """Download and send every highlight reel for any public account at once.

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
        story_items = await self._gather_highlight_items(username, catalog)
        if not story_items:
            return {
                "ok": False, "count": 0, "reels": len(items),
                "error": _DOWNLOAD_UNAVAILABLE_MSG,
            }

        sent = await self._deliver_story_items(account_id, username, story_items, set())
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

        story_items = await self._gather_highlight_items(username, catalog)
        if not story_items:
            return {
                "ok": False, "count": 0, "reels": len(catalog),
                "error": _DOWNLOAD_UNAVAILABLE_MSG,
            }

        sent = await self._deliver_story_items(account_id, username, story_items, set())
        return {"ok": True, "count": sent, "reels": len(catalog), "error": None}

    async def download_posts(
        self,
        username: str,
        *,
        photos: bool = True,
        videos: bool = True,
        limit: int = 100,
    ) -> dict:
        """Download and send the account's feed grid media (login-free).

        saveinsta's profile listing serves the post/reel grid at full
        resolution; `photos` keeps image posts, `videos` keeps video posts and
        reels. Like the other on-demand paths this ignores seen-dedup so the
        user always gets the media, but monitored accounts still get the items
        marked seen so the sweep won't re-send them.
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
        if photo_items:
            sent_photos = await self._deliver_story_items(
                account_id, username, photo_items, set()
            )
        sent_videos = 0
        if video_items:
            sent_videos = await self._deliver_story_items(
                account_id, username, video_items, set()
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
