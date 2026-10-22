"""Compares two snapshots and classifies the field-level changes."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, List, Optional

from app.database.models import AccountSnapshot
from app.monitor.media_hasher import PHASH_PREFIX, pic_asset_id
from app.monitor.public_page import strip_bidi
from app.utils.logger import logger

# A v3 fingerprint is the picture as a 16×16 grayscale thumbnail (see
# media_hasher.PHASH_PREFIX). Two fingerprints are compared by the RMS of their
# pixel differences, in gray levels (0-255), with three rules in order:
#
#   1. Same asset id on both avatar URLs: the same upload, so never a change,
#      whatever the pixels say. Instagram assigns a new id when, and only when,
#      a new picture is set (see media_hasher.pic_asset_id). The pixels of two
#      versions of one upload (150px id route, 320px API, up to 1080px from
#      saveinsta) are not evidence against that.
#   2. Difference ≥ PIC_CHANGE_MIN: a change.
#   3. A NEW asset id and difference ≥ PIC_URL_CHANGE_MIN: a change. A new id
#      already says "they uploaded something", so the pixels only have to rule
#      out the same picture uploaded again.
#
# Measured 2026-10-22 (21 real photos × as-is/dark/flat × 7 CDN versions): the
# same picture differs by ≤1.62 (median 0.84); distinct photos by ≥10.5, and the
# closest distinct pair of all, two washed-out textures, by 2.8. Hard-edged
# graphics at JPEG quality 20, harsher than anything Instagram serves, reach
# 2.96: the floor sits above that, so even that never reads as a change. The
# pathological near-blank pairs below it are still caught when they arrive
# with a new asset id, as a real upload does.
PIC_CHANGE_MIN = 3.5
PIC_URL_CHANGE_MIN = 2.0


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
    observed_fields: Optional[set[str]] = None,
    pic_baseline: Optional[tuple[Optional[str], Optional[str]]] = None,
) -> ChangeSet:
    """Build a structured changeset between two snapshots.

    `observed_fields` restricts which fields may raise an alert. A partial
    reading (the public page) stores carried-forward values for the fields it
    could not see, so that the card still shows what we last knew — but those
    values arrived from the OTHER source, and diffing them here re-reports a
    change the API already alerted on:

        API  100 → 101 posts   → alert (correct)
        page snapshot inherits 101, becomes the page baseline
        API  101 → 102 posts   → alert (correct)
        page snapshot inherits 102, diffs against the page baseline's 101
                               → alert AGAIN for the same change

    Carrying a value forward is a statement about storage, not an observation.
    Only what this reading actually saw can be a change; everything else is
    unknown and stays silent.

    `pic_baseline` is the (fingerprint, avatar URL) the picture is compared
    against; without it, `previous`'s are used.
    """
    changeset = ChangeSet(username=current.username)

    # The picture is the same whichever door saw it, so it is compared against
    # the newest fingerprint from ANY door (`pic_baseline`, the one the
    # service's confirmation pass already checked), not this door's own last
    # reading. Diffing it per door re-announced a change another door had
    # already reported: the page's weeks-old picture A against today's B, which
    # the chat had been shown the day it happened.
    if pic_baseline is not None:
        old_hash, old_url = pic_baseline
    elif previous is not None:
        old_hash, old_url = previous.profile_pic_hash, previous.profile_pic_url
    else:
        old_hash, old_url = None, None
    if new_pic_hash and old_hash and _pic_changed(
        old_hash, new_pic_hash, old_url=old_url, new_url=current.profile_pic_url
    ):
        changeset.profile_pic_changed = True
        changeset.old_pic_hash = old_hash
        changeset.new_pic_hash = new_pic_hash
    elif new_pic_hash and not old_hash:
        # We didn't have a hash before; record baseline silently.
        changeset.new_pic_hash = new_pic_hash

    if previous is None:
        # First successful observation from this door: no field changes.
        if not changeset.profile_pic_changed:
            changeset.new_pic_hash = new_pic_hash
        return changeset

    for field_name, label in FIELD_LABELS.items():
        if observed_fields is not None and field_name not in observed_fields:
            continue
        old_val = getattr(previous, field_name, None)
        new_val = getattr(current, field_name, None)
        if _is_meaningful_change(field_name, old_val, new_val):
            changeset.changes.append(
                Change(field=field_name, old=old_val, new=new_val, label=label)
            )

    return changeset


def _parse_fingerprint(value: Optional[str]) -> Optional[bytes]:
    """The thumbnail inside a "p3:" fingerprint, or None.

    None for anything else: a raw-SHA256, "p:" or "p2:" baseline from before
    the upgrade, or a malformed value. Callers treat a None on either side as a
    silent baseline, so the format upgrade never fires a spurious "changed" on
    the first sweep after it.
    """
    if not value or not value.startswith(PHASH_PREFIX):
        return None
    try:
        thumb = bytes.fromhex(value[len(PHASH_PREFIX):])
    except ValueError:
        return None
    return thumb or None


def pic_difference(old_hash: Optional[str], new_hash: Optional[str]) -> Optional[float]:
    """RMS pixel difference of two fingerprints, in gray levels; None when
    either is not a comparable v3 fingerprint."""
    old = _parse_fingerprint(old_hash)
    new = _parse_fingerprint(new_hash)
    if old is None or new is None or len(old) != len(new):
        return None
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(old, new)) / len(old))


def pic_fingerprints_differ(
    old_hash: str,
    new_hash: str,
    *,
    old_url: Optional[str] = None,
    new_url: Optional[str] = None,
) -> bool:
    """Public entry to the fingerprint comparison, for callers outside the
    snapshot diff — e.g. the service's second-download confirmation pass.
    Pass the avatar URLs when available so the asset-id signal applies."""
    return _pic_changed(old_hash, new_hash, old_url=old_url, new_url=new_url)


def _pic_changed(
    old_hash: str,
    new_hash: str,
    *,
    old_url: Optional[str] = None,
    new_url: Optional[str] = None,
) -> bool:
    """True only when two profile-picture fingerprints are a real change.

    See PIC_CHANGE_MIN for the rules and the measurement behind them. Any
    non-v3 value on either side (legacy hash, format upgrade, unhashable
    payload) is a silent baseline: recorded, but not reported as a change.
    """
    difference = pic_difference(old_hash, new_hash)
    old_id = pic_asset_id(old_url)
    new_id = pic_asset_id(new_url)
    if difference is None:
        # A baseline from before the p3 format can't be compared pixel by
        # pixel, so the upgrade re-baselines silently — unless the asset id
        # says a new picture was uploaded meanwhile, which needs no pixels.
        # Without this, a change made between the last old-format reading and
        # the first new one was absorbed into the baseline and never reported.
        legacy_baseline = bool(old_hash) and not old_hash.startswith(PHASH_PREFIX)
        if (
            legacy_baseline
            and _parse_fingerprint(new_hash) is not None
            and old_id and new_id and old_id != new_id
        ):
            logger.info(
                "Profile-pic change detected via asset id ({} -> {}) across the "
                "fingerprint upgrade (the old one can't be compared by pixels)",
                old_id, new_id,
            )
            return True
        return False
    if old_id and new_id and old_id == new_id:
        if difference >= PIC_CHANGE_MIN:
            logger.info(
                "Avatar pixels differ by {:.1f} but the asset id ({}) is the same "
                "upload — two versions of one picture, not a change",
                difference, old_id,
            )
        return False
    if difference >= PIC_CHANGE_MIN:
        return True
    if old_id and new_id:
        if difference >= PIC_URL_CHANGE_MIN:
            logger.info(
                "Profile-pic change detected via asset id ({} -> {}) with "
                "subtle pixel evidence (difference {:.1f})",
                old_id, new_id, difference,
            )
            return True
        logger.info(
            "Avatar asset id changed ({} -> {}) but the pictures read identical "
            "(difference {:.1f}) — a re-upload of the same picture, not a change",
            old_id, new_id, difference,
        )
    return False


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
        # None means "this observation couldn't see the field", which is not
        # the same as seeing it empty — the public-page fallback knows the
        # counts but genuinely cannot read the bio. Instagram's API sends ""
        # for a cleared bio, so a real removal still reports as "" vs text.
        if old is None or new is None:
            return False
        # Invisible direction/zero-width marks are presentation, not content.
        # Instagram's public page wraps RTL names in them and its API doesn't,
        # so without this an Arabic name reads as "changed" from one visibly
        # identical string to another every time the source flips.
        old_s = (strip_bidi(old) or "").strip()
        new_s = (strip_bidi(new) or "").strip()
        return old_s != new_s
    return old != new
