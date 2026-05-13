"""Database access functions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional

from sqlalchemy import delete, desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AccountSnapshot,
    AppSetting,
    MonitoredAccount,
    NotificationLog,
    ProfileMediaHash,
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
