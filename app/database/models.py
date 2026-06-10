"""SQLAlchemy ORM models for monitored accounts, snapshots, hashes, and logs."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class MonitoredAccount(Base):
    __tablename__ = "monitored_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    instagram_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    added_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    snapshots: Mapped[list["AccountSnapshot"]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
        order_by="desc(AccountSnapshot.created_at)",
    )
    media_hashes: Mapped[list["ProfileMediaHash"]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
        order_by="desc(ProfileMediaHash.created_at)",
    )
    notifications: Mapped[list["NotificationLog"]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
        order_by="desc(NotificationLog.created_at)",
    )


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("monitored_accounts.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    username: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    biography: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    followers_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    following_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    posts_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reels_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    story_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_private: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    is_verified: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    is_business: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    profile_pic_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    profile_pic_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    external_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_response: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    account: Mapped["MonitoredAccount"] = relationship(back_populates="snapshots")

    __table_args__ = (
        Index("ix_snapshots_account_created", "account_id", "created_at"),
    )


class ProfileMediaHash(Base):
    __tablename__ = "profile_media_hashes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("monitored_accounts.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    local_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    byte_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    content_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    account: Mapped["MonitoredAccount"] = relationship(back_populates="media_hashes")

    __table_args__ = (
        Index("ix_media_account_hash", "account_id", "sha256", unique=True),
    )


class AppSetting(Base):
    """Single-row-per-key store for runtime-tunable config (e.g. check interval)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class NotificationLog(Base):
    __tablename__ = "notification_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("monitored_accounts.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    change_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    delivery_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    account: Mapped["MonitoredAccount"] = relationship(back_populates="notifications")


class StoredHighlight(Base):
    """Latest known highlight reels for a public account (id + title).

    `tracked` is the per-highlight mute switch: untracked highlights are kept
    in the catalog (so renames/removals are still detected) but skipped by the
    sweep's auto-download."""

    __tablename__ = "stored_highlights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("monitored_accounts.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    highlight_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    tracked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_stored_highlights_account_reel", "account_id", "highlight_id", unique=True),
    )


class SeenStory(Base):
    """Tracks every story/highlight item that has been delivered to Telegram."""

    __tablename__ = "seen_stories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("monitored_accounts.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    story_pk: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)   # "story" | "highlight"
    highlight_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    highlight_title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    media_type: Mapped[str] = mapped_column(String(8), nullable=False) # "image" | "video"
    taken_at: Mapped[int] = mapped_column(Integer, nullable=False)
    seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_seen_stories_account_pk", "account_id", "story_pk", unique=True),
    )
