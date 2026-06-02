"""High-level orchestration: fetch -> hash -> diff -> persist -> notify."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from app.bot.notifications import (
    NotificationDispatcher,
    render_changes_message,
    render_failure_message,
)
from app.config import settings
from app.database import crud
from app.database.models import AccountSnapshot, MonitoredAccount, ProfileMediaHash
from app.database.session import get_session
from app.monitor.change_detector import ChangeSet, detect_changes
from app.monitor.instagram import InstagramClient, ProfileFetchResult
from app.monitor.media_hasher import HashedMedia, MediaHasher
from app.monitor.stories import StoriesClient
from app.utils.formatting import esc, fmt_timestamp
from app.utils.logger import logger


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

    async def check_all(self) -> dict:
        """Fan out checks across all active accounts."""
        async with get_session() as session:
            accounts = await crud.list_accounts(session, only_active=True)
            targets = [(a.id, a.username) for a in accounts]

        if not targets:
            logger.info("No active accounts to check.")
            return {"checked": 0, "changed": 0, "failed": 0}

        logger.info("Starting scheduled sweep across {} accounts", len(targets))
        noun = "profile" if len(targets) == 1 else "profiles"
        await self.notifier.send_text(
            f"ðŸ‘ Sweep started â€” {len(targets)} {noun} queued."
        )
        results = await asyncio.gather(
            *(self._run_check(aid, uname) for aid, uname in targets),
            return_exceptions=True,
        )

        checked = 0
        changed = 0
        failed = 0
        failed_usernames: list[str] = []
        story_targets: list[tuple[int, str]] = []
        for (target_account_id, uname), r in zip(targets, results):
            if isinstance(r, Exception):
                failed += 1
                failed_usernames.append(uname)
                logger.exception("Unhandled error during sweep: {}", r)
                continue
            checked += 1
            result_username = r.get("username", uname)
            story_targets.append((target_account_id, result_username))
            if r.get("changed"):
                changed += 1
            if not r.get("ok"):
                failed += 1
                failed_usernames.append(result_username)

        logger.info(
            "Sweep done: checked={}, changed={}, failed={}", checked, changed, failed
        )

        if self.stories is not None:
            await asyncio.gather(
                *(
                    self._check_stories_and_highlights(aid, uname)
                    for aid, uname in story_targets
                ),
                return_exceptions=True,
            )

        noun = "profile" if checked == 1 else "profiles"
        summary = f"ðŸ‘ Sweep complete â€” {checked} {noun} checked."
        if failed:
            names = ", ".join(f"@{u}" for u in failed_usernames)
            summary += f" {failed} failed: {names}"
        await self.notifier.send_text(summary)

        return {"checked": checked, "changed": changed, "failed": failed}

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

        if not instagram_id:
            logger.info("Cannot recover @{} after 404: no Instagram ID stored", username)
            return None

        new_username = await self.instagram.fetch_username_by_id(str(instagram_id))
        if not new_username:
            logger.info(
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
            "Recovered renamed account id={}: @{} -> @{}",
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
        if not isinstance(raw_response, dict):
            return None
        try:
            user = raw_response["data"]["user"]
        except (KeyError, TypeError):
            return None
        if not isinstance(user, dict):
            return None
        instagram_id = user.get("id")
        return str(instagram_id) if instagram_id else None

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
            # are not stored â€” they add no information.
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
                raw_response=fetch.raw_response,
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
                parsed_instagram_id = parsed.get("instagram_id")
                # Store Instagram ID if account doesn't have one yet
                if parsed_instagram_id and not account.instagram_id:
                    account.instagram_id = str(parsed_instagram_id)
                    logger.info(
                        "Stored Instagram ID for @{}: {}",
                        account.username,
                        parsed_instagram_id,
                    )
                if parsed_username and parsed_username != account.username:
                    existing = await crud.get_account(session, parsed_username)
                    if existing is None or existing.id == account.id:
                        account.username = parsed_username
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

        return {
            "ok": True,
            "username": username,
            "status": 200,
            "changed": changeset.has_changes,
            "change_count": len(changeset.changes) + (1 if changeset.profile_pic_changed else 0),
            "first_seen": previous is None,
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

    async def _check_stories_and_highlights(
        self, account_id: int, username: str
    ) -> None:
        """Fetch new story/highlight items for one account and deliver them."""
        assert self.stories is not None
        async with self._semaphore:
            try:
                stories, highlights = await asyncio.gather(
                    self.stories.fetch_stories(username),
                    self.stories.fetch_highlights(username),
                )
                all_items = stories + highlights
                if not all_items:
                    return

                async with get_session() as session:
                    seen_pks = await crud.get_seen_story_pks(session, account_id)

                new_items = [i for i in all_items if i.pk and i.pk not in seen_pks]
                if not new_items:
                    return

                logger.info(
                    "Found {} new story item(s) for @{}", len(new_items), username
                )

                for item in new_items:
                    path = await self.stories.download(item, username)
                    if path is None:
                        logger.warning(
                            "Could not download story {} for @{}", item.pk, username
                        )
                        # Mark seen anyway so expired items don't loop forever.
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
                        continue

                    if item.source == "highlight":
                        caption = (
                            f"âœ¨ <b>@{esc(username)}</b> â€” highlight: "
                            f"<b>{esc(item.highlight_title or '')}</b>"
                        )
                    else:
                        caption = f"ðŸ“– <b>@{esc(username)}</b> â€” new story"

                    if item.media_type == "video":
                        ok = await self.notifier.send_video(path, caption=caption)
                    else:
                        ok = await self.notifier.send_photo(path, caption=caption)

                    if ok:
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
            except Exception as exc:
                logger.exception(
                    "Story check failed for @{}: {}", username, exc
                )
