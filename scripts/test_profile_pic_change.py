"""Regression tests for the two profile-picture bugs.

Bug 1 (false "profile picture changed" every sweep): Instagram's CDN serves
byte-different re-encodes of the SAME avatar on each signed URL, so the old
raw-SHA256 comparison flip-flopped constantly. Change detection now runs on a
perceptual hash (dHash), which is stable across re-encodes.

Bug 2 (the promised photo never arrived): the notifier's media senders opened
the file inside a `with` block and returned the coroutine, so the handle was
closed BEFORE _send_with_retry awaited it — python-telegram-bot then raised
"read of closed file" at await time and every upload silently failed. The
senders now hand the path to PTB, which reads it when the coroutine runs.

Runs offline on sqlite with fakes — no Telegram, no network.
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_FILE = ROOT / "test_profile_pic_change.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_FILE.as_posix()}")

from PIL import Image, ImageDraw  # noqa: E402
from telegram import Document  # noqa: E402
from telegram._utils.files import parse_file_input  # noqa: E402

from app.database.models import (  # noqa: E402
    AccountSnapshot,
    Base,
    MonitoredAccount,
)
from app.database.session import engine, get_session  # noqa: E402
from app.monitor.change_detector import (  # noqa: E402
    PHASH_HAMMING_THRESHOLD,
    detect_changes,
)
from app.monitor.instagram import ProfileFetchResult  # noqa: E402
from app.monitor.media_hasher import (  # noqa: E402
    HashedMedia,
    PHASH_PREFIX,
    perceptual_hash,
)
from app.bot.notifications import NotificationDispatcher  # noqa: E402
from app.monitor.service import MonitorService  # noqa: E402

FAILURES: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    line = f"{status}: {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line)
    if not condition:
        FAILURES.append(name)


def _hamming_phash(a: str, b: str) -> int:
    return bin(
        int(a[len(PHASH_PREFIX):], 16) ^ int(b[len(PHASH_PREFIX):], 16)
    ).count("1")


def _encode(img: Image.Image, *, size: int, quality: int) -> bytes:
    buf = io.BytesIO()
    img.resize((size, size)).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# ---------- Part A: perceptual hash is stable across re-encodes ----------

def test_perceptual_hash() -> None:
    # A picture with structure (gradient + a shape) so the dHash isn't trivial.
    avatar = Image.new("RGB", (400, 400), (30, 60, 120))
    d = ImageDraw.Draw(avatar)
    for y in range(400):
        d.line([(0, y), (400, y)], fill=(y % 256, (2 * y) % 256, (120 + y) % 256))
    d.ellipse([120, 120, 280, 280], fill=(240, 230, 40))

    # Same avatar, the way the CDN actually varies it: different size + quality.
    big = _encode(avatar, size=320, quality=90)
    small = _encode(avatar, size=150, quality=30)
    tiny = _encode(avatar, size=96, quality=20)

    hb, hs, ht = perceptual_hash(big), perceptual_hash(small), perceptual_hash(tiny)
    expect("re-encode hashes are non-None", all([hb, hs, ht]))
    expect("phash carries the p: marker", hb.startswith(PHASH_PREFIX), repr(hb))
    expect(
        "same avatar, 320q90 vs 150q30 within threshold",
        _hamming_phash(hb, hs) <= PHASH_HAMMING_THRESHOLD,
        f"distance={_hamming_phash(hb, hs)}",
    )
    expect(
        "same avatar, 320q90 vs 96q20 within threshold",
        _hamming_phash(hb, ht) <= PHASH_HAMMING_THRESHOLD,
        f"distance={_hamming_phash(hb, ht)}",
    )

    # A genuinely different picture must land far away.
    other = Image.new("RGB", (400, 400), (255, 255, 255))
    od = ImageDraw.Draw(other)
    od.rectangle([0, 0, 200, 400], fill=(10, 10, 10))
    od.ellipse([250, 40, 380, 170], fill=(200, 30, 30))
    ho = perceptual_hash(_encode(other, size=320, quality=90))
    expect(
        "different picture exceeds threshold",
        _hamming_phash(hb, ho) > PHASH_HAMMING_THRESHOLD,
        f"distance={_hamming_phash(hb, ho)}",
    )

    # Non-image payload (e.g. a CDN error page) → no hash, never alarms.
    expect("non-image payload yields None", perceptual_hash(b"not an image") is None)


# ---------- Part A2: detect_changes uses the perceptual comparison ----------

def _snap(phash):
    return AccountSnapshot(account_id=1, username="t", profile_pic_hash=phash)


def test_detect_changes() -> None:
    a = "p:4cb27169b271e8d4"
    a_reencode = a  # CDN re-encode hashes identically
    different = "p:0069d4a8e8d46800"  # measured ~29 bits from `a`
    legacy_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    cs = detect_changes(_snap(a), _snap(a_reencode), new_pic_hash=a_reencode)
    expect("re-encode is NOT a change", cs.profile_pic_changed is False)

    cs = detect_changes(_snap(a), _snap(different), new_pic_hash=different)
    expect("different avatar IS a change", cs.profile_pic_changed is True)
    expect("change records both fingerprints",
           cs.old_pic_hash == a and cs.new_pic_hash == different)

    # Legacy raw-SHA256 baseline (pre-upgrade) vs a new perceptual hash: silent
    # baseline, never a one-off false alarm on the first post-deploy sweep.
    cs = detect_changes(_snap(legacy_sha), _snap(a), new_pic_hash=a)
    expect("legacy->perceptual is a silent baseline", cs.profile_pic_changed is False)

    # No prior hash → baseline recorded, no alert.
    cs = detect_changes(_snap(None), _snap(a), new_pic_hash=a)
    expect("first observation is a silent baseline",
           cs.profile_pic_changed is False and cs.new_pic_hash == a)


# ---------- Part B: notifier media senders survive the await ----------

class _PTBLikeBot:
    """Reads the file at await time exactly like python-telegram-bot does, so a
    handle closed before the await (the old bug) would raise here."""

    def __init__(self) -> None:
        self.read_bytes: bytes | None = None

    async def _accept(self, media):
        await asyncio.sleep(0)  # force the read to happen after the caller returned
        self.read_bytes = parse_file_input(media, Document).input_file_content
        return SimpleNamespace(message_id=1)

    async def send_document(self, *, chat_id, document, **kw):
        return await self._accept(document)

    async def send_photo(self, *, chat_id, photo, **kw):
        return await self._accept(photo)

    async def send_video(self, *, chat_id, video, **kw):
        return await self._accept(video)


async def test_media_send_file_lifecycle() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "avatar.jpg"
        payload = b"\xff\xd8\xff" + b"watcher-bytes" * 50
        path.write_bytes(payload)

        for label, method in (
            ("send_document", "send_document"),
            ("send_photo", "send_photo"),
            ("send_video", "send_video"),
        ):
            bot = _PTBLikeBot()
            disp = NotificationDispatcher(bot, chat_id=1)
            ok = await getattr(disp, method)(path, caption="x")
            expect(f"{label} reports delivered", ok is True)
            expect(
                f"{label} read the full file at await time",
                bot.read_bytes == payload,
                f"got {len(bot.read_bytes or b'')} of {len(payload)} bytes",
            )


# ---------- Part C: a full sweep no longer cries wolf, but real swaps fire ----

class FakeInstagram:
    def __init__(self, parsed: dict) -> None:
        self._parsed = parsed

    async def fetch_profile(self, username: str) -> ProfileFetchResult:
        return ProfileFetchResult(
            username=username, http_status=200, parsed=dict(self._parsed),
            raw_response={"data": {"user": {"id": self._parsed["instagram_id"]}}},
        )

    async def fetch_hd_pic_url(self, user_id: str):
        raise AssertionError("must not be called without a session cookie")


class SeqHasher:
    """Returns a scripted HashedMedia per hash_url call (one per sweep)."""

    def __init__(self, phashes: list[str], path: Path) -> None:
        self._phashes = phashes
        self._path = path
        self.i = 0

    async def hash_url(self, url: str, username: str) -> HashedMedia:
        phash = self._phashes[self.i]
        self.i += 1
        # sha256 differs every sweep (mimics the CDN re-encode) to prove change
        # detection ignores it and keys off phash instead.
        return HashedMedia(
            sha256=f"{self.i:064x}",
            byte_size=100 + self.i,
            content_type="image/jpeg",
            local_path=self._path,
            source_url=url,
            phash=phash,
        )


async def test_full_sweep() -> None:
    parsed = {
        "username": "tester", "full_name": "T", "biography": "",
        "followers_count": 10, "following_count": 5, "posts_count": 0,
        "reels_count": 0, "story_count": 0, "is_private": True,
        "is_verified": False, "is_business": False,
        "profile_pic_url": "http://cdn/pic.jpg", "external_url": None,
        "instagram_id": "999",
    }
    async with get_session() as session:
        session.add(MonitoredAccount(username="tester", active=True))

    with tempfile.TemporaryDirectory() as tmp:
        pic = Path(tmp) / "pic.jpg"
        pic.write_bytes(b"\xff\xd8\xfffake")

        same = "p:4cb27169b271e8d4"
        reencode = same                 # sweep 2: identical fingerprint
        swapped = "p:0069d4a8e8d46800"  # sweep 3: a real new picture
        hasher = SeqHasher([same, reencode, swapped], pic)

        notifier = AsyncMock()
        notifier.send_text = AsyncMock(return_value=True)
        notifier.send_document = AsyncMock(return_value=True)
        notifier.create_forum_topic = AsyncMock(return_value=None)

        service = MonitorService(
            instagram=FakeInstagram(parsed), hasher=hasher,
            notifier=notifier, stories=None,
        )

        r1 = await service.check_username("tester")
        expect("sweep1 first_seen", r1.get("first_seen") is True, repr(r1))
        expect("sweep1 sends no photo", notifier.send_document.call_count == 0)

        r2 = await service.check_username("tester")
        expect("sweep2 (re-encode) reports unchanged", r2.get("changed") is False, repr(r2))
        expect(
            "sweep2 sends NO photo — the false-positive is gone",
            notifier.send_document.call_count == 0,
            f"send_document called {notifier.send_document.call_count}x",
        )

        r3 = await service.check_username("tester")
        expect("sweep3 (real swap) reports changed", r3.get("changed") is True, repr(r3))
        expect(
            "sweep3 sends the photo exactly once",
            notifier.send_document.call_count == 1,
            f"send_document called {notifier.send_document.call_count}x",
        )
        # The stored baseline must now be the swapped fingerprint.
        async with get_session() as session:
            from sqlalchemy import select
            snaps = (await session.execute(
                select(AccountSnapshot).order_by(AccountSnapshot.id)
            )).scalars().all()
        expect("latest snapshot stores the new fingerprint",
               snaps[-1].profile_pic_hash == swapped,
               repr(snaps[-1].profile_pic_hash))


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    test_perceptual_hash()
    test_detect_changes()
    await test_media_send_file_lifecycle()
    await test_full_sweep()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All profile-picture regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
