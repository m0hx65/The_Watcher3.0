"""Database access functions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, List, Optional

from sqlalchemy import delete, desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AccountSnapshot,
    AppSetting,
    MonitoredAccount,
    NotificationLog,
    ProfileMediaHash,
    SeenStory,
    StoredHighlight,
)


def normalize_username(username: str) -> str:
    return username.strip().lstrip("@").lower()


# ---------- MonitoredAccount ----------

async def add_account(
    session: AsyncSession,
    username: str,
    added_by: Optional[int] = None,
    instagram_id: Optional[str] = None,
) -> tuple[MonitoredAccount, bool]:
    """Insert or reactivate an account. Returns (account, created)."""
    username = normalize_username(username)
    result = await session.execute(
        select(MonitoredAccount).where(MonitoredAccount.username == username)
    )
    account = result.scalar_one_or_none()
    if account:
        created = False
        if not account.active:
            account.active = True
            created = True
        if instagram_id and not account.instagram_id:
            account.instagram_id = instagram_id
        return account, created

    account = MonitoredAccount(
        username=username,
        added_by=added_by,
        active=True,
        instagram_id=instagram_id,
    )
    session.add(account)
    await session.flush()
    return account, True


async def get_account(session: AsyncSession, username: str) -> Optional[MonitoredAccount]:
    username = normalize_username(username)
    result = await session.execute(
        select(MonitoredAccount).where(MonitoredAccount.username == username)
    )
    return result.scalar_one_or_none()


async def list_accounts(
    session: AsyncSession, only_active: bool = True
) -> List[MonitoredAccount]:
    stmt = select(MonitoredAccount).order_by(MonitoredAccount.username)
    if only_active:
        stmt = stmt.where(MonitoredAccount.active.is_(True))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def remove_account(session: AsyncSession, username: str) -> bool:
    username = normalize_username(username)
    result = await session.execute(
        delete(MonitoredAccount).where(MonitoredAccount.username == username)
    )
    return result.rowcount > 0


async def deactivate_account(session: AsyncSession, username: str) -> bool:
    username = normalize_username(username)
    result = await session.execute(
        update(MonitoredAccount)
        .where(MonitoredAccount.username == username)
        .values(active=False)
    )
    return result.rowcount > 0


async def set_account_active(
    session: AsyncSession, username: str, active: bool
) -> bool:
    """Pause (active=False) or resume (active=True) monitoring for an account.

    Paused accounts are skipped by the sweep (list_accounts(only_active=True))
    but kept in the DB with their history intact."""
    username = normalize_username(username)
    result = await session.execute(
        update(MonitoredAccount)
        .where(MonitoredAccount.username == username)
        .values(active=active)
    )
    return result.rowcount > 0


async def mark_checked(
    session: AsyncSession,
    account_id: int,
    status_code: int,
    success: bool,
) -> int:
    """Update last-checked fields and return the resulting consecutive_failures count."""
    account = await session.get(MonitoredAccount, account_id)
    if account is None:
        return 0
    account.last_checked_at = datetime.now(timezone.utc)
    account.last_status_code = status_code
    if success:
        account.consecutive_failures = 0
    else:
        account.consecutive_failures = (account.consecutive_failures or 0) + 1
    await session.flush()
    return account.consecutive_failures


# ---------- AccountSnapshot ----------

async def get_latest_snapshot(
    session: AsyncSession, account_id: int, *, successful_only: bool = True
) -> Optional[AccountSnapshot]:
    stmt = (
        select(AccountSnapshot)
        .where(AccountSnapshot.account_id == account_id)
        # id tiebreaker: created_at can collide (SQLite stores whole seconds),
        # and returning the older row would silently rewind the baseline.
        .order_by(desc(AccountSnapshot.created_at), desc(AccountSnapshot.id))
        .limit(1)
    )
    if successful_only:
        stmt = stmt.where(AccountSnapshot.http_status == 200)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_latest_pic_baseline(
    session: AsyncSession, account_id: int
) -> tuple[Optional[str], Optional[str]]:
    """The newest successful snapshot's (profile_pic_hash, profile_pic_url) —
    one indexed two-column read, for the pic-change confirmation pass that
    runs before the full snapshot is loaded."""
    stmt = (
        select(AccountSnapshot.profile_pic_hash, AccountSnapshot.profile_pic_url)
        .where(
            AccountSnapshot.account_id == account_id,
            AccountSnapshot.http_status == 200,
        )
        .order_by(desc(AccountSnapshot.created_at), desc(AccountSnapshot.id))
        .limit(1)
    )
    result = await session.execute(stmt)
    row = result.first()
    return (row[0], row[1]) if row else (None, None)


async def insert_snapshot(
    session: AsyncSession, snapshot: AccountSnapshot
) -> AccountSnapshot:
    session.add(snapshot)
    await session.flush()
    return snapshot


async def cleanup_old_snapshots(
    session: AsyncSession, account_id: int, keep_count: int = 200
) -> int:
    """Delete old snapshots keeping only the most recent keep_count records. Returns count deleted."""
    # Get all snapshot IDs for this account ordered by creation time (newest first)
    stmt = (
        select(AccountSnapshot.id)
        .where(AccountSnapshot.account_id == account_id)
        .order_by(desc(AccountSnapshot.created_at))
        .offset(keep_count)
    )
    result = await session.execute(stmt)
    old_ids = result.scalars().all()
    
    if not old_ids:
        return 0
    
    # Delete snapshots with IDs in the old_ids list
    delete_stmt = delete(AccountSnapshot).where(AccountSnapshot.id.in_(old_ids))
    delete_result = await session.execute(delete_stmt)
    return delete_result.rowcount


async def recent_snapshots(
    session: AsyncSession, account_id: int, limit: int = 10
) -> List[AccountSnapshot]:
    stmt = (
        select(AccountSnapshot)
        .where(AccountSnapshot.account_id == account_id)
        .order_by(desc(AccountSnapshot.created_at))
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ---------- ProfileMediaHash ----------

async def latest_media_hash(
    session: AsyncSession, account_id: int
) -> Optional[ProfileMediaHash]:
    stmt = (
        select(ProfileMediaHash)
        .where(ProfileMediaHash.account_id == account_id)
        # id tiebreaker: created_at can collide (same-second inserts), and an
        # arbitrary winner would serve a stale avatar (see get_latest_snapshot).
        .order_by(desc(ProfileMediaHash.created_at), desc(ProfileMediaHash.id))
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def find_media_hash(
    session: AsyncSession, account_id: int, sha256: str
) -> Optional[ProfileMediaHash]:
    stmt = select(ProfileMediaHash).where(
        ProfileMediaHash.account_id == account_id,
        ProfileMediaHash.sha256 == sha256,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def insert_media_hash(
    session: AsyncSession, media: ProfileMediaHash
) -> ProfileMediaHash:
    session.add(media)
    await session.flush()
    return media


# ---------- NotificationLog ----------

async def log_notification(
    session: AsyncSession,
    account_id: int,
    change_type: str,
    payload: Optional[dict[str, Any]],
    message: Optional[str],
    delivered: bool,
    delivery_error: Optional[str] = None,
) -> NotificationLog:
    note = NotificationLog(
        account_id=account_id,
        change_type=change_type,
        payload=payload,
        message=message,
        delivered=delivered,
        delivery_error=delivery_error,
    )
    session.add(note)
    await session.flush()
    return note


async def recent_notifications(
    session: AsyncSession, account_id: int, limit: int = 20
) -> List[NotificationLog]:
    stmt = (
        select(NotificationLog)
        .where(NotificationLog.account_id == account_id)
        .order_by(desc(NotificationLog.created_at))
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def notifications_since(
    session: AsyncSession, since: datetime
) -> List[tuple[NotificationLog, str]]:
    """Every logged notification since `since`, paired with its account username.

    Powers the digest — one indexed range scan joined to the account, instead of
    N per-account queries. Ordered newest-first within the window.
    """
    stmt = (
        select(NotificationLog, MonitoredAccount.username)
        .join(MonitoredAccount, MonitoredAccount.id == NotificationLog.account_id)
        .where(NotificationLog.created_at >= since)
        .order_by(desc(NotificationLog.created_at))
    )
    result = await session.execute(stmt)
    return [(row[0], row[1]) for row in result.all()]


async def stats_summary(session: AsyncSession) -> dict[str, Any]:
    total = (
        await session.execute(select(func.count()).select_from(MonitoredAccount))
    ).scalar_one()
    active = (
        await session.execute(
            select(func.count())
            .select_from(MonitoredAccount)
            .where(MonitoredAccount.active.is_(True))
        )
    ).scalar_one()
    snapshots = (
        await session.execute(select(func.count()).select_from(AccountSnapshot))
    ).scalar_one()
    notifications = (
        await session.execute(select(func.count()).select_from(NotificationLog))
    ).scalar_one()
    return {
        "accounts_total": total,
        "accounts_active": active,
        "snapshots_total": snapshots,
        "notifications_total": notifications,
    }


async def export_all(session: AsyncSession) -> Iterable[NotificationLog]:
    stmt = (
        select(NotificationLog)
        .order_by(desc(NotificationLog.created_at))
        .limit(5000)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


# ---------- StoredHighlight (highlight reel catalog per account) ----------

async def get_highlight_catalog(
    session: AsyncSession, account_id: int
) -> dict[str, str]:
    """Return highlight_id -> title for an account."""
    result = await session.execute(
        select(StoredHighlight).where(StoredHighlight.account_id == account_id)
    )
    return {row.highlight_id: row.title for row in result.scalars().all()}


async def replace_highlight_catalog(
    session: AsyncSession,
    account_id: int,
    catalog: dict[str, str],
) -> None:
    """Sync the stored highlight catalog with the current Instagram list.

    Rows are updated in place — titles refresh, vanished ids are deleted, new
    ids are inserted (tracked by default) — so per-highlight state like the
    `tracked` mute flag survives every refresh."""
    result = await session.execute(
        select(StoredHighlight).where(StoredHighlight.account_id == account_id)
    )
    existing = {row.highlight_id: row for row in result.scalars().all()}
    for highlight_id, title in catalog.items():
        row = existing.pop(highlight_id, None)
        if row is None:
            session.add(
                StoredHighlight(
                    account_id=account_id,
                    highlight_id=highlight_id,
                    title=title or "",
                )
            )
        elif row.title != (title or ""):
            row.title = title or ""
    if existing:
        await session.execute(
            delete(StoredHighlight).where(
                StoredHighlight.account_id == account_id,
                StoredHighlight.highlight_id.in_(existing.keys()),
            )
        )
    await session.flush()


async def get_untracked_highlight_ids(
    session: AsyncSession, account_id: int
) -> set[str]:
    """Highlight ids muted for sweep auto-download on this account."""
    result = await session.execute(
        select(StoredHighlight.highlight_id).where(
            StoredHighlight.account_id == account_id,
            StoredHighlight.tracked.is_(False),
        )
    )
    return set(result.scalars().all())


async def set_highlight_tracked(
    session: AsyncSession, account_id: int, highlight_id: str, tracked: bool
) -> bool:
    """Mute (tracked=False) or unmute one highlight. Returns False if unknown."""
    result = await session.execute(
        update(StoredHighlight)
        .where(
            StoredHighlight.account_id == account_id,
            StoredHighlight.highlight_id == highlight_id,
        )
        .values(tracked=tracked)
    )
    return result.rowcount > 0


async def set_all_highlights_tracked(
    session: AsyncSession, account_id: int, tracked: bool
) -> int:
    """Mute or unmute every stored highlight of an account. Returns rows changed."""
    result = await session.execute(
        update(StoredHighlight)
        .where(StoredHighlight.account_id == account_id)
        .values(tracked=tracked)
    )
    return result.rowcount


# ---------- SeenStory (deduplication for stories & highlights) ----------

async def mark_story_items_seen(
    session: AsyncSession,
    account_id: int,
    items: Iterable[Any],
) -> None:
    """Mark multiple story/highlight items as seen (skips duplicates).

    The seen-set is loaded ONCE and rows are added in bulk with a single
    flush — the old per-item mark_story_seen path cost two round-trips per
    item, which made baselining a large highlight catalog crawl on a remote
    Postgres."""
    seen = await get_seen_story_pks(session, account_id)
    added = False
    for item in items:
        pk = getattr(item, "pk", None) or (item.get("pk") if isinstance(item, dict) else None)
        if not pk or str(pk) in seen:
            continue
        source = getattr(item, "source", None) or (item.get("source") if isinstance(item, dict) else "story")
        session.add(SeenStory(
            account_id=account_id,
            story_pk=str(pk),
            source=str(source),
            highlight_id=getattr(item, "highlight_id", None)
            or (item.get("highlight_id") if isinstance(item, dict) else None),
            highlight_title=getattr(item, "highlight_title", None)
            or (item.get("highlight_title") if isinstance(item, dict) else None),
            media_type=str(
                getattr(item, "media_type", None)
                or (item.get("media_type") if isinstance(item, dict) else "image")
            ),
            taken_at=int(
                getattr(item, "taken_at", None)
                or (item.get("taken_at") if isinstance(item, dict) else 0)
            ),
        ))
        seen.add(str(pk))
        added = True
    if added:
        await session.flush()

async def get_seen_story_pks(session: AsyncSession, account_id: int) -> set[str]:
    """Return the set of story PKs already delivered for this account."""
    result = await session.execute(
        select(SeenStory.story_pk).where(SeenStory.account_id == account_id)
    )
    return set(result.scalars().all())


async def mark_story_seen(
    session: AsyncSession,
    account_id: int,
    story_pk: str,
    source: str,
    highlight_id: Optional[str],
    highlight_title: Optional[str],
    media_type: str,
    taken_at: int,
) -> None:
    # Idempotent: the on-demand Story/Highlights buttons re-send items the user
    # has already seen (they pass an empty seen-set), so this can be called for a
    # (account_id, story_pk) that already exists — a plain INSERT would hit the
    # unique constraint and crash the handler.
    existing = await session.execute(
        select(SeenStory.id)
        .where(SeenStory.account_id == account_id, SeenStory.story_pk == story_pk)
        .limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        return
    session.add(SeenStory(
        account_id=account_id,
        story_pk=story_pk,
        source=source,
        highlight_id=highlight_id,
        highlight_title=highlight_title,
        media_type=media_type,
        taken_at=taken_at,
    ))
    await session.flush()


# ---------- Activity rhythm & went-dark radar (from seen_stories) ----------

async def activity_timestamps(
    session: AsyncSession, account_id: int
) -> list[datetime]:
    """All delivered-item timestamps for an account (stories, posts, highlights).

    seen_at is when the bot first caught the item — within one sweep of the
    real post for stories/posts — so it's a faithful proxy for posting time.
    taken_at from the anonymous media source is usually 0, so it isn't used."""
    result = await session.execute(
        select(SeenStory.seen_at).where(SeenStory.account_id == account_id)
    )
    out: list[datetime] = []
    for ts in result.scalars().all():
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append(ts)
    return out


async def last_activity_at(
    session: AsyncSession, account_id: int
) -> Optional[datetime]:
    """Most recent delivered story/post/highlight time, or None if never."""
    result = await session.execute(
        select(func.max(SeenStory.seen_at)).where(
            SeenStory.account_id == account_id
        )
    )
    ts = result.scalar_one_or_none()
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


async def last_activity_map(session: AsyncSession) -> dict[int, datetime]:
    """account_id -> most recent delivered-item time, for EVERY account at once.

    One grouped query instead of a per-account last_activity_at loop — the
    dark radar runs at the end of every sweep, so this is on the hot path."""
    result = await session.execute(
        select(SeenStory.account_id, func.max(SeenStory.seen_at)).group_by(
            SeenStory.account_id
        )
    )
    out: dict[int, datetime] = {}
    for account_id, ts in result.all():
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out[account_id] = ts
    return out


async def first_activity_at(
    session: AsyncSession, account_id: int
) -> Optional[datetime]:
    """Earliest delivered item time — the start of the observation window."""
    result = await session.execute(
        select(func.min(SeenStory.seen_at)).where(
            SeenStory.account_id == account_id
        )
    )
    ts = result.scalar_one_or_none()
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


# ---------- AppSetting (runtime-tunable KV) ----------

async def get_setting(session: AsyncSession, key: str) -> Optional[str]:
    result = await session.execute(select(AppSetting).where(AppSetting.key == key))
    row = result.scalar_one_or_none()
    return row.value if row else None


async def get_settings_by_prefix(
    session: AsyncSession, prefix: str
) -> dict[str, str]:
    """All settings whose key starts with `prefix`, as one query.

    Used to load every per-account flag of one kind (e.g. "dark_state:") in a
    single round-trip instead of N get_setting calls."""
    result = await session.execute(
        select(AppSetting).where(AppSetting.key.like(f"{prefix}%"))
    )
    return {row.key: row.value for row in result.scalars().all()}


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    result = await session.execute(select(AppSetting).where(AppSetting.key == key))
    row = result.scalar_one_or_none()
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    await session.flush()


async def delete_setting(session: AsyncSession, key: str) -> bool:
    result = await session.execute(delete(AppSetting).where(AppSetting.key == key))
    return result.rowcount > 0


# ---------- Per-account forum topic mapping (stored in app_settings) ----------

def _topic_key(account_id: int) -> str:
    return f"topic:{account_id}"


async def get_account_topic(
    session: AsyncSession, account_id: int
) -> Optional[int]:
    """Return the Telegram forum topic (message_thread_id) for an account."""
    raw = await get_setting(session, _topic_key(account_id))
    if raw and raw.lstrip("-").isdigit():
        return int(raw)
    return None


async def set_account_topic(
    session: AsyncSession, account_id: int, thread_id: int
) -> None:
    await set_setting(session, _topic_key(account_id), str(thread_id))


# ---------- Data retention ----------

async def purge_old_data(
    session: AsyncSession,
    snapshot_days: int,
    notification_days: int,
    raw_response_days: int,
) -> dict[str, int]:
    """
    Delete aged-out rows and reclaim JSONB space.

    Rules:
    - account_snapshots older than snapshot_days are deleted, EXCEPT the single
      most-recent snapshot per account (needed as the change-detection baseline).
    - raw_response is NULLed on snapshots older than raw_response_days even when
      the row itself is kept — this is the biggest space saver.
    - notification_logs older than notification_days are deleted.
    - profile_media_hashes are trimmed separately by purge_old_media_hashes
      (the CDN's per-fetch re-encodes make that table grow every sweep).

    Pass 0 for any threshold to skip that step.
    Returns counts of affected rows.
    """
    now = datetime.now(timezone.utc)
    totals: dict[str, int] = {
        "snapshots_deleted": 0,
        "raw_responses_nulled": 0,
        "notifications_deleted": 0,
    }

    # --- NULL out raw_response on old-but-kept snapshots ---
    if raw_response_days > 0:
        cutoff = now - timedelta(days=raw_response_days)
        result = await session.execute(
            update(AccountSnapshot)
            .where(
                AccountSnapshot.created_at < cutoff,
                AccountSnapshot.raw_response.isnot(None),
            )
            .values(raw_response=None)
        )
        totals["raw_responses_nulled"] = result.rowcount

    # --- Delete old snapshots, preserving the newest per account ---
    if snapshot_days > 0:
        cutoff = now - timedelta(days=snapshot_days)

        # Subquery: id of the most-recent snapshot per account
        latest_ids_sq = (
            select(func.max(AccountSnapshot.id))
            .group_by(AccountSnapshot.account_id)
            .scalar_subquery()
        )

        result = await session.execute(
            delete(AccountSnapshot).where(
                AccountSnapshot.created_at < cutoff,
                AccountSnapshot.id.notin_(latest_ids_sq),
            )
        )
        totals["snapshots_deleted"] = result.rowcount

    # --- Delete old notification logs ---
    if notification_days > 0:
        cutoff = now - timedelta(days=notification_days)
        result = await session.execute(
            delete(NotificationLog).where(NotificationLog.created_at < cutoff)
        )
        totals["notifications_deleted"] = result.rowcount

    return totals


async def purge_old_media_hashes(
    session: AsyncSession, keep_per_account: int = 25
) -> list[str]:
    """Trim profile_media_hashes to the newest keep_per_account rows per account.

    The CDN serves byte-different re-encodes of the same avatar on almost every
    signed URL, so without a cap this table — and its file per row on disk —
    grows on every sweep, per account, forever. The newest rows are kept so
    /photo (latest_media_hash) and recent-re-encode dedup keep working.

    Returns the local_path of every deleted row so the caller can remove the
    files from disk AFTER the transaction commits.
    """
    result = await session.execute(
        select(
            ProfileMediaHash.id,
            ProfileMediaHash.account_id,
            ProfileMediaHash.local_path,
        ).order_by(
            ProfileMediaHash.account_id,
            desc(ProfileMediaHash.created_at),
            desc(ProfileMediaHash.id),
        )
    )
    doomed_ids: list[int] = []
    doomed_paths: list[str] = []
    kept = 0
    current_account: Optional[int] = None
    for row_id, account_id, local_path in result.all():
        if account_id != current_account:
            current_account = account_id
            kept = 0
        kept += 1
        if kept > keep_per_account:
            doomed_ids.append(row_id)
            if local_path:
                doomed_paths.append(local_path)

    if doomed_ids:
        await session.execute(
            delete(ProfileMediaHash).where(ProfileMediaHash.id.in_(doomed_ids))
        )
    return doomed_paths


async def clear_history(session: AsyncSession) -> dict[str, int]:
    """Delete all history except the newest snapshot per account.

    Keeps monitored_accounts, app_settings, and profile_media_hashes untouched.
    Returns counts of deleted rows.
    """
    totals: dict[str, int] = {
        "snapshots_deleted": 0,
        "notifications_deleted": 0,
        "stories_deleted": 0,
        "highlights_deleted": 0,
    }

    # Keep only the most-recent snapshot per account (needed as change-detection baseline)
    latest_ids_sq = (
        select(func.max(AccountSnapshot.id))
        .group_by(AccountSnapshot.account_id)
        .scalar_subquery()
    )
    result = await session.execute(
        delete(AccountSnapshot).where(AccountSnapshot.id.notin_(latest_ids_sq))
    )
    totals["snapshots_deleted"] = result.rowcount

    result = await session.execute(delete(NotificationLog))
    totals["notifications_deleted"] = result.rowcount

    result = await session.execute(delete(SeenStory))
    totals["stories_deleted"] = result.rowcount

    result = await session.execute(delete(StoredHighlight))
    totals["highlights_deleted"] = result.rowcount

    return totals
