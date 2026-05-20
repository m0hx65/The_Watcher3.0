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
)


def normalize_username(username: str) -> str:
    return username.strip().lstrip("@").lower()


# ---------- MonitoredAccount ----------

async def add_account(
    session: AsyncSession,
    username: str,
    added_by: Optional[int] = None,
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
        return account, created

    account = MonitoredAccount(username=username, added_by=added_by, active=True)
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
        .order_by(desc(AccountSnapshot.created_at))
        .limit(1)
    )
    if successful_only:
        stmt = stmt.where(AccountSnapshot.http_status == 200)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def insert_snapshot(
    session: AsyncSession, snapshot: AccountSnapshot
) -> AccountSnapshot:
    session.add(snapshot)
    await session.flush()
    return snapshot


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
        .order_by(desc(ProfileMediaHash.created_at))
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


# ---------- SeenStory (deduplication for stories & highlights) ----------

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


# ---------- AppSetting (runtime-tunable KV) ----------

async def get_setting(session: AsyncSession, key: str) -> Optional[str]:
    result = await session.execute(select(AppSetting).where(AppSetting.key == key))
    row = result.scalar_one_or_none()
    return row.value if row else None


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    result = await session.execute(select(AppSetting).where(AppSetting.key == key))
    row = result.scalar_one_or_none()
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    await session.flush()


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
    - profile_media_hashes are never purged (sparse table, serves dedup).

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


async def clear_history(session: AsyncSession) -> dict[str, int]:
    """Delete all history except the newest snapshot per account.

    Keeps monitored_accounts, app_settings, and profile_media_hashes untouched.
    Returns counts of deleted rows.
    """
    totals: dict[str, int] = {
        "snapshots_deleted": 0,
        "notifications_deleted": 0,
        "stories_deleted": 0,
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

    return totals
