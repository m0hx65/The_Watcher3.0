"""Compares two snapshots and classifies the field-level changes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from app.database.models import AccountSnapshot


@dataclass
class Change:
    field: str
    old: Any
    new: Any
    label: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "label": self.label,
            "old": self.old,
            "new": self.new,
        }


@dataclass
class ChangeSet:
    username: str
    changes: List[Change] = field(default_factory=list)
    profile_pic_changed: bool = False
    old_pic_hash: Optional[str] = None
    new_pic_hash: Optional[str] = None

    @property
    def has_changes(self) -> bool:
        return bool(self.changes) or self.profile_pic_changed

    def find(self, field_name: str) -> Optional[Change]:
        for c in self.changes:
            if c.field == field_name:
                return c
        return None


FIELD_LABELS: dict[str, str] = {
    "username": "username",
    "full_name": "full name",
    "biography": "bio",
    "followers_count": "followers",
    "following_count": "following",
    "posts_count": "posts",
    "reels_count": "reels",
    "story_count": "highlights",
    "is_private": "privacy",
    "is_verified": "verification",
    "is_business": "business account",
    "external_url": "external link",
}

NUMERIC_FIELDS = {
    "followers_count",
    "following_count",
    "posts_count",
    "reels_count",
    "story_count",
}

BOOL_FIELDS = {"is_private", "is_verified", "is_business"}

TEXT_FIELDS = {"username", "full_name", "biography", "external_url"}


def detect_changes(
    previous: Optional[AccountSnapshot],
    current: AccountSnapshot,
    *,
    new_pic_hash: Optional[str] = None,
) -> ChangeSet:
    """Build a structured changeset between two snapshots."""
    changeset = ChangeSet(username=current.username)

    if previous is None:
        # First successful observation — record baseline hash, no change events.
        changeset.new_pic_hash = new_pic_hash
        return changeset

    for field_name, label in FIELD_LABELS.items():
        old_val = getattr(previous, field_name, None)
        new_val = getattr(current, field_name, None)
        if _is_meaningful_change(field_name, old_val, new_val):
            changeset.changes.append(
                Change(field=field_name, old=old_val, new=new_val, label=label)
            )

    # Profile picture: compare via hash, not URL (URLs rotate often).
    old_hash = previous.profile_pic_hash
    if new_pic_hash and old_hash and new_pic_hash != old_hash:
        changeset.profile_pic_changed = True
        changeset.old_pic_hash = old_hash
        changeset.new_pic_hash = new_pic_hash
    elif new_pic_hash and not old_hash:
        # We didn't have a hash before; record baseline silently.
        changeset.new_pic_hash = new_pic_hash

    return changeset


def _is_meaningful_change(field_name: str, old: Any, new: Any) -> bool:
    if old is None and new is None:
        return False
    if field_name in NUMERIC_FIELDS:
        # Don't report transitions to/from None as changes
        if old is None or new is None:
            return False
        return int(old) != int(new)
    if field_name in BOOL_FIELDS:
        if old is None or new is None:
            return False
        return bool(old) != bool(new)
    if field_name in TEXT_FIELDS:
        old_s = (old or "").strip()
        new_s = (new or "").strip()
        return old_s != new_s
    return old != new
