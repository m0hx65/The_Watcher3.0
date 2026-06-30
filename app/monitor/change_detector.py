"""Compares two snapshots and classifies the field-level changes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from app.database.models import AccountSnapshot
from app.monitor.media_hasher import PHASH_PREFIX

# A v2 fingerprint is "p2:<dhash>:<ahash>:<mean>" — two independent 256-bit
# perceptual hashes plus the brightness mean of one normalized image. They have
# complementary strengths, measured across a spread of avatars × CDN re-encodes:
#   * aHash (overall light/dark layout) is rock-solid stable across re-encodes
#     (same avatar ≤ ~2 bits) but a softer discriminator (distinct avatars ≥ ~10).
#   * dHash (gradient structure) separates distinct avatars strongly (≥ ~45) but
#     is noisier on flat re-encodes (same avatar up to ~24 bits).
#   * mean (brightness) barely moves across re-encodes (≤3 levels) and is the
#     only signal that sees a flat solid-color swap (where both hashes are 0).
#
# The decision below plays each to its strength so the result is bulletproof in
# both directions:
#   changed = (dHash ≥ DHASH_CHANGE_MIN  AND  aHash ≥ AHASH_CHANGE_MIN)
#             OR dHash ≥ DHASH_CHANGE_STRONG
#             OR |Δmean| ≥ BRIGHTNESS_CHANGE_MIN
# The AND branch needs the STABLE aHash to also move, so dHash's flat-region
# noise can never raise a false "changed" on a re-encode (the aHash vetoes it).
# The OR branches catch a structurally very different picture even when its
# layout happens to be similar, and a solid-color swap the hashes can't see.
# Every floor sits in the wide no-man's-land between the two distributions
# (verified, with margin, by scripts/test_profile_pic_change.py).
PHASH_BITS = 256
DHASH_CHANGE_MIN = 32     # AND-branch dHash floor (re-encode ≤24, distinct ≥45)
AHASH_CHANGE_MIN = 6      # AND-branch aHash floor (re-encode ≤2,  distinct ≥10)
DHASH_CHANGE_STRONG = 40  # OR-branch: structural change strong enough on its own
# OR-branch on overall brightness. Re-encodes jitter ≤3 grayscale levels; a swap
# between two different solid-color avatars (whose structural hashes are an
# uninformative all-zero) shifts the mean far more. The floor sits well above
# re-encode jitter so it never false-positives, yet catches the flat-image swap
# the two bit-hashes are blind to.
BRIGHTNESS_CHANGE_MIN = 12

# Back-compat alias: the old single-threshold name a couple of call sites/tests
# referenced. The v2 decision uses the floors above instead.
PHASH_HAMMING_THRESHOLD = DHASH_CHANGE_MIN


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

    # Profile picture: compare perceptual hashes, not URLs (which rotate) or raw
    # bytes (the CDN re-encodes the same avatar per signed URL — see HashedMedia).
    old_hash = previous.profile_pic_hash
    if new_pic_hash and old_hash and _pic_changed(old_hash, new_pic_hash):
        changeset.profile_pic_changed = True
        changeset.old_pic_hash = old_hash
        changeset.new_pic_hash = new_pic_hash
    elif new_pic_hash and not old_hash:
        # We didn't have a hash before; record baseline silently.
        changeset.new_pic_hash = new_pic_hash

    return changeset


def _parse_fingerprint(value: str) -> Optional[tuple[int, int, int]]:
    """Parse "p2:<dhash>:<ahash>:<mean>" into (dhash_bits, ahash_bits, mean).

    Returns None for anything that isn't a v2 fingerprint — a legacy raw-SHA256
    baseline, the old 64-bit "p:" dHash, or a malformed value. Callers treat a
    None on either side as a silent baseline so the one-time format upgrade never
    fires a spurious "changed" on the first post-deploy sweep.
    """
    if not value.startswith(PHASH_PREFIX):
        return None
    parts = value[len(PHASH_PREFIX):].split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0], 16), int(parts[1], 16), int(parts[2], 16)
    except ValueError:
        return None


def _pic_changed(old_hash: str, new_hash: str) -> bool:
    """True only when two profile-picture fingerprints are a real, visible change.

    Each fingerprint carries a dHash (gradient structure), an aHash (light/dark
    layout), and a brightness mean. A change is reported when the structural
    evidence is strong on both bit-hashes, OR overwhelming on the dHash alone, OR
    the overall brightness shifts well past CDN re-encode jitter (the flat
    solid-color swap the bit-hashes can't see). A single noisy signal can never
    raise a false alarm, while a real swap clears one of these comfortably. Any
    non-v2 value on either side (legacy hash, format upgrade, unhashable payload)
    is a silent baseline: recorded, but not reported as a change.
    """
    old = _parse_fingerprint(old_hash)
    new = _parse_fingerprint(new_hash)
    if old is None or new is None:
        return False
    dhash_dist = bin(old[0] ^ new[0]).count("1")
    ahash_dist = bin(old[1] ^ new[1]).count("1")
    brightness_dist = abs(old[2] - new[2])
    return (
        (dhash_dist >= DHASH_CHANGE_MIN and ahash_dist >= AHASH_CHANGE_MIN)
        or dhash_dist >= DHASH_CHANGE_STRONG
        or brightness_dist >= BRIGHTNESS_CHANGE_MIN
    )


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
