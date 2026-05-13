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
from app.utils.formatting import fmt_timestamp
from app.utils.logger import logger


class MonitorService:
    """Coordinates a single account check or a fan-out across all accounts."""

    def __init__(
        self,
        instagram: InstagramClient,
        hasher: MediaHasher,
        notifier: NotificationDispatcher,
    ) -> None:
        self.instagram = instagram
        self.hasher = hasher
        self.notifier = notifier
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
        results = await asyncio.gather(
            *(self._run_check(aid, uname) for aid, uname in targets),
            return_exceptions=True,
        )

        checked = 0
        changed = 0
        failed = 0
        for r in results:
            if isinstance(r, Exception):
                failed += 1
                logger.exception("Unhandled error during sweep: {}", r)
                continue
            checked += 1
            if r.get("changed"):
                changed += 1
            if not r.get("ok"):
                failed += 1

        logger.info(
            "Sweep done: checked={}, changed={}, failed={}", checked, changed, failed
        )
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
            return await self._handle_failure(account_id, username, fetch)

        return await self._handle_success(account_id, username, fetch, notify_unchanged)

    async def _handle_failure(
        self, account_id: int, username: str, fetch: ProfileFetchResult
    ) -> dict:
        logger.warning(
            "Fetch failed for @{}: status={} error={}",
            username, fetch.http_status, fetch.error,
        )

        async with get_session() as session:
            snapshot = AccountSnapshot(
                account_id=account_id,
                username=username,
                http_status=fetch.http_status,
                raw_response=fetch.raw_response,
                error=fetch.error,
            )
            await crud.insert_snapshot(session, snapshot)
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

        # Hash profile picture from the media URL returned by web_profile_info.
        hashed: Optional[HashedMedia] = None
        if parsed.get("profile_pic_url"):
            hashed = await self.hasher.hash_url(parsed["profile_pic_url"], username)

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
            await crud.insert_snapshot(session, snapshot)

            changeset = detect_changes(previous, snapshot, new_pic_hash=new_pic_hash)

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
                if parsed.get("instagram_id") and not account.instagram_id:
                    account.instagram_id = parsed["instagram_id"]
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

        # Profile picture sent separately so it shows inline
        if changeset.profile_pic_changed and new_pic_path is not None:
            caption = (
                f"<b>@{username}</b> changed profile picture\n"
                f"Old hash: <code>{changeset.old_pic_hash}</code>\n"
                f"New hash: <code>{changeset.new_pic_hash}</code>"
            )
            ok = await self.notifier.send_photo(new_pic_path, caption=caption)
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
