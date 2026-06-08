"""High-level orchestration: fetch -> hash -> diff -> persist -> notify."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
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

# Shown when a story/highlight MEDIA download is requested but no anonymous
# source can currently serve it. The free anonymous API this used to rely on
# (storiesig.info) was shut down by Instagram's anti-scraping changes, and the
# bot is intentionally login-free, so media download is unavailable for now.
# Highlight names and story/live status still work anonymously via graphql.
_DOWNLOAD_UNAVAILABLE_MSG = (
    "Media download is currently unavailable — the anonymous source it relied on "
    "was shut down, and this bot stays 100% login-free. Highlight names and story "
    "status still work."
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

    async def check_username(
        self, username: str, *, notify_unchanged: bool = False
    ) -> dict:
        """Run one check by username. Returns a summary dict."""
        username = username.strip().lstrip("@").lower()
        async with get_session() as session:
            account = await crud.get_account(session, username)
            if account is None:
                return {"ok": False, "error": f"@{username} is not monitored"}
            account_id = account.id

        return await self._run_check(account_id, username, notify_unchanged=notify_unchanged)

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
            *(self._run_check(aid, uname) for aid, uname in targets),
            return_exceptions=True,
        )

        checked = 0
        changed = 0
        failed = 0
        failed_usernames: list[str] = []
        story_targets: list[tuple[int, str, Optional[str]]] = []
        for (target_account_id, uname), r in zip(targets, results):
            if isinstance(r, Exception):
                failed += 1
                failed_usernames.append(uname)
                logger.exception("Unhandled error during sweep: {}", r)
                continue
            checked += 1
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
            if r.get("changed"):
                changed += 1
            if not r.get("ok"):
                failed += 1
                failed_usernames.append(result_username)

        logger.info(
            "Sweep done: checked={}, changed={}, failed={}", checked, changed, failed
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
        # Try the mobile API first (returns hd_profile_pic_url_info, up to ~1440px).
        # Fall back to the web API's profile_pic_url_hd (~320px) if unavailable.
        pic_url = parsed.get("profile_pic_url")
        instagram_id = parsed.get("instagram_id")
        if instagram_id:
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

        # Build the raw_response with reel_data if available
        raw_response_with_reel = fetch.raw_response.copy() if fetch.raw_response else {}
        if reel_data_response:
            raw_response_with_reel["reel_data"] = reel_data_response

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
                raw_response=raw_response_with_reel,
            )

            # Diff first, persist only when something actually changed.
            changeset = detect_changes(previous, snapshot, new_pic_hash=new_pic_hash)
            if previous is None or changeset.has_changes:
                await crud.insert_snapshot(session, snapshot)
                # Keep only the latest 200 snapshots per account
                await crud.cleanup_old_snapshots(session, account_id, keep_count=200)
            else:
                previous.raw_response = fetch.raw_response
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
                parsed_instagram_id = parsed.get("instagram_id") or self._extract_instagram_id(
                    fetch.raw_response
                )
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
        if not changeset.has_changes:
            if notify_unchanged:
                await self.notifier.send_text(
                    f"<b>@{username}</b>\nNo changes detected.\n"
                    f"Checked at {fmt_timestamp(datetime.now(timezone.utc))}"
                )
            return

        # Send aggregated text message
        text = render_changes_message(changeset, first_seen=previous_snapshot_id is None)
        delivered = False
        if text:
            delivered = await self.notifier.send_text(text)

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
            ok = await self.notifier.send_document(new_pic_path, caption=caption)
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
        if instagram_id:
            reel_user = await self.instagram.fetch_reel_user(str(instagram_id))
            if reel_user is not None and "highlights" in reel_user:
                return dict(reel_user["highlights"])
        assert self.stories is not None
        return await self.stories.fetch_highlight_catalog(username)

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
                        }
                        logger.debug(
                            "Fetched reel data for @{} during story check (not in snapshot)",
                            username
                        )

                # Fetch the current highlight catalog (graphql reel query, anonymous).
                catalog = await self._fetch_highlight_catalog(username, instagram_id)

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
                        delivered = await self.notifier.send_text(msg)
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
                    
                    # Always notify about current story/live status
                    status_parts = []
                    
                    if is_live:
                        status_parts.append("🔴 LIVE NOW")
                    elif has_public_story:
                        status_parts.append("🎬 HAS STORY")
                    else:
                        status_parts.append("⭕ NO STORY")
                    
                    msg = f"<b>@{esc(username)}</b> — {' • '.join(status_parts)}"
                    delivered = await self.notifier.send_text(msg)
                    async with get_session() as session:
                        await crud.log_notification(
                            session,
                            account_id=account_id,
                            change_type="story_status",
                            payload={
                                "has_public_story": has_public_story,
                                "is_live": is_live,
                            },
                            message=msg,
                            delivered=delivered,
                        )
                    
                    # Also notify on changes (when not establishing baseline)
                    if not establishing_baseline:
                        # Notify if just went live
                        if is_live and not prev_is_live:
                            msg = f"🔴 <b>@{esc(username)}</b> just went live!"
                            delivered = await self.notifier.send_text(msg)
                            async with get_session() as session:
                                await crud.log_notification(
                                    session,
                                    account_id=account_id,
                                    change_type="going_live",
                                    payload={"is_live": is_live},
                                    message=msg,
                                    delivered=delivered,
                                )
                        
                        # Notify if just posted a story
                        if has_public_story and not prev_has_story:
                            msg = f"🎬 <b>@{esc(username)}</b> just posted a story!"
                            delivered = await self.notifier.send_text(msg)
                            async with get_session() as session:
                                await crud.log_notification(
                                    session,
                                    account_id=account_id,
                                    change_type="story_posted",
                                    payload={"has_public_story": has_public_story},
                                    message=msg,
                                    delivered=delivered,
                                )

                # Try to fetch the actual story items to download (anonymous, no
                # login). The legacy source is down, so this may yield nothing.
                stories = await self.stories.fetch_stories(username)
                new_stories = [s for s in stories if s.pk and s.pk not in seen_pks]

                if establishing_baseline:
                    highlight_items = await self.stories.fetch_highlights(username)
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
                    await self.notifier.send_text(alert)
                    await self._deliver_story_items(
                        account_id, username, new_stories, seen_pks
                    )

                highlight_items = await self.stories.fetch_highlights(username)
                new_highlight_items = [
                    i for i in highlight_items if i.pk and i.pk not in seen_pks
                ]
                if new_highlight_items:
                    await self._deliver_story_items(
                        account_id, username, new_highlight_items, seen_pks
                    )
            except Exception as exc:
                logger.exception(
                    "Story check failed for @{}: {}", username, exc
                )

    async def _deliver_story_items(
        self,
        account_id: int,
        username: str,
        items: list,
        seen_pks: set[str],
    ) -> int:
        """Download and send each item; record it as seen. Returns the number sent."""
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
            else:
                caption = f"📖 <b>@{esc(username)}</b> — new story"

            if item.media_type == "video":
                ok = await self.notifier.send_video(path, caption=caption)
            else:
                ok = await self.notifier.send_photo(path, caption=caption)

            if ok:
                sent += 1
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

    # ---------- On-demand actions (triggered from the account card) ----------

    async def fetch_and_send_stories(self, username: str) -> dict:
        """Download every current story item for a public account and send them now.

        Unlike the sweep path, this ignores the seen-deduplication set so the user
        always receives whatever is live at the moment they tap the button. Items
        are still recorded as seen afterwards so the next sweep won't re-send them.
        Returns {"ok": bool, "count": int, "error": Optional[str]}.
        """
        if self.stories is None:
            return {"ok": False, "count": 0, "error": "Stories client unavailable"}
        username = username.strip().lstrip("@").lower()
        async with get_session() as session:
            account = await crud.get_account(session, username)
            if account is None:
                return {"ok": False, "count": 0, "error": f"@{username} is not monitored"}
            account_id = account.id
            instagram_id = account.instagram_id

        # Distinguish "no active story" (a real, anonymous-knowable state) from
        # "there is a story but we can't fetch the media anonymously". The reel
        # query tells us has_public_story without any login.
        has_story: Optional[bool] = None
        if instagram_id:
            try:
                reel_user = await self.instagram.fetch_reel_user(str(instagram_id))
                if reel_user is not None:
                    has_story = bool(reel_user.get("has_public_story"))
            except Exception:  # pragma: no cover - network failure path
                has_story = None

        # Best-effort anonymous media fetch (no login). The legacy source is down,
        # so this typically yields nothing today, but we still try in case an
        # anonymous source is reachable.
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
        """Return the current highlight reels (id + title) for an account.

        Prefers the live storiesig catalog (its ids are the ones the download
        endpoint accepts) and refreshes the stored catalog; falls back to the
        last stored catalog if the live fetch yields nothing.
        Returns {"ok": bool, "items": list[(id, title)], "error": Optional[str]}.
        """
        username = username.strip().lstrip("@").lower()
        async with get_session() as session:
            account = await crud.get_account(session, username)
            if account is None:
                return {"ok": False, "items": [], "error": f"@{username} is not monitored"}
            account_id = account.id
            instagram_id = account.instagram_id

        # The highlight catalog (names + ids) comes from Instagram's own graphql
        # reel query, which still works anonymously. Only the media download needs
        # a cookie, so names are always available.
        catalog: dict[str, str] = {}
        try:
            catalog = await self._fetch_highlight_catalog(username, instagram_id)
        except Exception as exc:  # pragma: no cover - network failure path
            logger.warning("On-demand highlight catalog failed for @{}: {}", username, exc)
            catalog = {}

        if catalog:
            async with get_session() as session:
                await crud.replace_highlight_catalog(session, account_id, catalog)
        else:
            async with get_session() as session:
                catalog = await crud.get_highlight_catalog(session, account_id)

        items = sorted(catalog.items(), key=lambda kv: kv[0])
        return {"ok": True, "items": items, "error": None}

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
            if account is None:
                return {
                    "ok": False, "count": 0, "title": None,
                    "error": f"@{username} is not monitored",
                }
            account_id = account.id

        listing = await self.list_highlights(username)
        items = listing["items"]
        if index < 0 or index >= len(items):
            return {
                "ok": False, "count": 0, "title": None,
                "error": "That highlight is no longer available — refresh the list.",
            }

        highlight_id, title = items[index]
        # Best-effort anonymous media fetch (no login). The legacy source is down,
        # so this typically yields nothing today.
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
