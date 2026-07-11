"""Regression tests for the profile-picture detection + delivery bugs.

Bug 1 (false "profile picture changed", and missed real changes): Instagram's
CDN serves byte-different re-encodes of the SAME avatar on each signed URL, at
varying resolutions and JPEG qualities. The old detector hashed whatever
resolution it happened to fetch with a coarse 8×8 dHash, so flat-region JPEG
noise faked changes while genuinely different (similarly-composed) avatars slid
under the threshold. Detection now NORMALIZES every image to one canonical
grayscale before hashing and compares TWO independent 256-bit perceptual hashes
(dHash structure + aHash layout): a change is reported only when the evidence is
strong on both — or overwhelming on dHash alone. See media_hasher.perceptual_hash
and change_detector._pic_changed.

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
    AHASH_CHANGE_MIN,
    DHASH_CHANGE_MIN,
    DHASH_CHANGE_STRONG,
    _parse_fingerprint,
    _pic_changed,
    detect_changes,
)
from app.monitor.instagram import ProfileFetchResult  # noqa: E402
from app.monitor.media_hasher import (  # noqa: E402
    HashedMedia,
    PHASH_PREFIX,
    perceptual_hash,
    pic_asset_id,
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


def _dist(a: str, b: str) -> tuple[int, int]:
    pa, pb = _parse_fingerprint(a), _parse_fingerprint(b)
    return bin(pa[0] ^ pb[0]).count("1"), bin(pa[1] ^ pb[1]).count("1")


def _encode(img: Image.Image, *, size: int, quality: int) -> bytes:
    buf = io.BytesIO()
    img.resize((size, size), Image.LANCZOS).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# A spread of avatars chosen to stress BOTH failure modes:
#  * #2 is mostly flat (the false-positive trap — flat regions flap under JPEG).
#  * #4/#5 are deliberately similar headshots (the false-negative trap — a real
#    swap with near-identical composition).
def _make_avatars() -> list[Image.Image]:
    out: list[Image.Image] = []

    a = Image.new("RGB", (512, 512), (30, 60, 120))
    d = ImageDraw.Draw(a)
    for y in range(512):
        d.line([(0, y), (512, y)], fill=(y % 256, (2 * y) % 256, (120 + y) % 256))
    d.ellipse([150, 150, 360, 360], fill=(240, 230, 40))
    out.append(a)

    b = Image.new("RGB", (512, 512), (245, 245, 245))
    ImageDraw.Draw(b).rectangle([210, 210, 300, 300], fill=(20, 20, 20))
    out.append(b)

    c = Image.new("RGB", (512, 512), (255, 255, 255))
    d = ImageDraw.Draw(c)
    d.rectangle([0, 0, 256, 512], fill=(15, 15, 15))
    d.ellipse([320, 60, 470, 210], fill=(200, 30, 30))
    out.append(c)

    e = Image.new("RGB", (512, 512), (180, 200, 210))
    d = ImageDraw.Draw(e)
    d.ellipse([140, 120, 372, 440], fill=(225, 190, 165))
    d.chord([140, 90, 372, 320], 180, 360, fill=(60, 40, 30))
    d.ellipse([210, 250, 240, 280], fill=(40, 40, 40))
    d.ellipse([300, 250, 330, 280], fill=(40, 40, 40))
    out.append(e)

    f = Image.new("RGB", (512, 512), (180, 200, 210))
    d = ImageDraw.Draw(f)
    d.ellipse([120, 130, 392, 450], fill=(235, 205, 180))
    d.chord([120, 100, 392, 340], 180, 360, fill=(150, 110, 60))
    d.ellipse([205, 260, 240, 295], fill=(30, 30, 30))
    d.ellipse([305, 260, 340, 295], fill=(30, 30, 30))
    out.append(f)

    return out


def _reencodes(img: Image.Image) -> list[bytes]:
    """Same picture as the CDN serves it: many sizes × JPEG qualities."""
    out: list[bytes] = []
    for size in (1440, 640, 320, 150, 96):
        for q in (95, 80, 60, 40, 20):
            out.append(_encode(img, size=size, quality=q))
    return out


# ---------- Part A: the fingerprint + decision are bulletproof both ways ------

def test_perceptual_hash() -> None:
    avatars = _make_avatars()
    groups = [[perceptual_hash(b) for b in _reencodes(im)] for im in avatars]

    expect("every re-encode hashes to a fingerprint",
           all(all(g) for g in groups))
    expect("fingerprint carries the p2: marker",
           groups[0][0].startswith(PHASH_PREFIX), repr(groups[0][0]))
    expect("non-image payload yields None", perceptual_hash(b"not an image") is None)

    # The decisive checks: run the ACTUAL decision over every pair.
    # 1) No re-encode pair of the SAME avatar may read as a change (false pos).
    worst_same = (0, 0)
    fp = 0
    for g in groups:
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                dd, ad = _dist(g[i], g[j])
                worst_same = (max(worst_same[0], dd), max(worst_same[1], ad))
                if _pic_changed(g[i], g[j]):
                    fp += 1
    expect("ZERO false positives across all re-encode pairs", fp == 0, f"{fp} flagged")

    # 2) Every distinct-avatar pair MUST read as a change (false neg).
    best_diff = (10**9, 10**9)
    fn = 0
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            dd, ad = _dist(groups[i][0], groups[j][0])
            best_diff = (min(best_diff[0], dd), min(best_diff[1], ad))
            if not _pic_changed(groups[i][0], groups[j][0]):
                fn += 1
    expect("ZERO false negatives across all distinct pairs", fn == 0, f"{fn} missed")

    # Margin report — the floors must sit clear of both distributions.
    print(f"   margins: same(dd<={worst_same[0]}, ad<={worst_same[1]}) "
          f"diff(dd>={best_diff[0]}, ad>={best_diff[1]}) "
          f"floors(dd_min={DHASH_CHANGE_MIN}, ad_min={AHASH_CHANGE_MIN}, "
          f"dd_strong={DHASH_CHANGE_STRONG})")
    expect("same-image dHash noise stays under the floor",
           worst_same[0] < DHASH_CHANGE_MIN, f"worst same dd={worst_same[0]}")
    expect("same-image aHash noise stays under the floor",
           worst_same[1] < AHASH_CHANGE_MIN, f"worst same ad={worst_same[1]}")

    # The HD-vs-thumbnail flip (1440px stripped URL one sweep, 320px the next)
    # was the live flapping source — it must read identical now.
    big = perceptual_hash(_encode(avatars[0], size=1440, quality=85))
    thumb = perceptual_hash(_encode(avatars[0], size=320, quality=60))
    expect("HD vs 320px of one avatar is NOT a change",
           not _pic_changed(big, thumb), f"dist={_dist(big, thumb)}")

    # Flat solid-color avatars: the gradient/layout hashes are an uninformative
    # all-zero, so the brightness signal has to carry the decision. Same color
    # across re-encodes is NOT a change; a swap to a different solid color IS.
    red1 = perceptual_hash(_encode(Image.new("RGB", (512, 512), (200, 40, 40)),
                                   size=320, quality=80))
    red2 = perceptual_hash(_encode(Image.new("RGB", (512, 512), (200, 40, 40)),
                                   size=150, quality=25))
    blue = perceptual_hash(_encode(Image.new("RGB", (512, 512), (40, 40, 200)),
                                   size=320, quality=80))
    expect("same solid color across re-encodes is NOT a change",
           not _pic_changed(red1, red2), f"dist={_dist(red1, red2)}")
    expect("solid-color swap IS a change",
           _pic_changed(red1, blue), f"dist={_dist(red1, blue)}")


# ---------- Part A1b: the URL asset-id signal ----------
# Instagram avatar URLs carry a numeric asset id in the basename that changes
# ONLY on a new upload; the signed params / CDN shard / size variant rotate per
# fetch. When the id changed, reduced perceptual floors apply so subtle real
# swaps are caught — while an id rotation with an unchanged picture stays quiet.

URL_A = (
    "https://scontent-mad1-1.cdninstagram.com/v/t51.2885-19/s320x320/"
    "111111111_2222222222222222_3333333333333333333_n.jpg"
    "?stp=dst-jpg_e0_s320x320&_nc_ht=scontent.cdninstagram.com&oh=aaa&oe=111"
)
# SAME asset id as URL_A: different shard, size variant, size-class letter, and
# signature — everything that rotates without the picture changing.
URL_A_ROTATED = (
    "https://scontent-lhr8-1.cdninstagram.com/v/t51.2885-19/s150x150/"
    "111111111_2222222222222222_3333333333333333333_s.jpg"
    "?stp=dst-jpg_e0_s150x150&_nc_ht=other.cdninstagram.com&oh=bbb&oe=222"
)
URL_B = (
    "https://scontent-mad1-1.cdninstagram.com/v/t51.2885-19/s320x320/"
    "444444444_5555555555555555_6666666666666666666_n.jpg"
    "?stp=dst-jpg_e0_s320x320&_nc_ht=scontent.cdninstagram.com&oh=ccc&oe=333"
)


def _fp(dhash: int = 0, ahash: int = 0, mean: int = 0x80) -> str:
    """Craft a v2 fingerprint with exact bit distances from _fp() (all-zero)."""
    return f"{PHASH_PREFIX}{dhash:064x}:{ahash:064x}:{mean:02x}"


def test_url_asset_id() -> None:
    expect("asset id parses from a CDN avatar URL",
           pic_asset_id(URL_A) == "111111111_2222222222222222_3333333333333333333",
           repr(pic_asset_id(URL_A)))
    expect("asset id survives shard/size/signature rotation",
           pic_asset_id(URL_A) == pic_asset_id(URL_A_ROTATED))
    expect("different upload yields a different asset id",
           pic_asset_id(URL_B) != pic_asset_id(URL_A))
    expect("non-CDN URL yields None (signal disabled, not faked)",
           pic_asset_id("https://saveinsta.to/dl?token=eyJ0eXAi") is None)
    expect("None URL yields None", pic_asset_id(None) is None)
    expect("CDN URL without a numeric basename yields None",
           pic_asset_id("https://scontent.cdninstagram.com/v/t51/avatar.jpg") is None)

    # dd=30/ad=5/Δmean=2: real-but-subtle territory — under every strong floor,
    # over the reduced dHash floor.
    base = _fp()
    subtle = _fp(dhash=(1 << 30) - 1, ahash=(1 << 5) - 1, mean=0x82)
    # dd=20/ad=2/Δmean=3: same-picture re-encode territory — under the reduced
    # floors too (a re-upload/migration of the SAME picture must stay quiet).
    reupload = _fp(dhash=(1 << 20) - 1, ahash=(1 << 2) - 1, mean=0x83)
    # Only the brightness moved past re-encode jitter.
    dimmed = _fp(mean=0x88)

    expect("subtle diff WITHOUT url evidence is NOT a change",
           not _pic_changed(base, subtle))
    expect("subtle diff with the SAME asset id is NOT a change",
           not _pic_changed(base, subtle, old_url=URL_A, new_url=URL_A_ROTATED))
    expect("subtle diff with a NEW asset id IS a change",
           _pic_changed(base, subtle, old_url=URL_A, new_url=URL_B))
    expect("new asset id but identical-reading image is NOT a change",
           not _pic_changed(base, reupload, old_url=URL_A, new_url=URL_B))
    expect("new asset id + subtle brightness shift IS a change",
           _pic_changed(base, dimmed, old_url=URL_A, new_url=URL_B))
    expect("subtle brightness shift alone is NOT a change",
           not _pic_changed(base, dimmed))


# ---------- Part A2: detect_changes uses the perceptual comparison ----------

def _snap(phash, url=None):
    return AccountSnapshot(
        account_id=1, username="t", profile_pic_hash=phash, profile_pic_url=url
    )


def test_detect_changes() -> None:
    avatars = _make_avatars()
    a = perceptual_hash(_encode(avatars[0], size=320, quality=80))
    a_reencode = perceptual_hash(_encode(avatars[0], size=150, quality=30))
    different = perceptual_hash(_encode(avatars[2], size=320, quality=80))
    legacy_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    legacy_dhash = "p:4cb27169b271e8d4"  # the old 64-bit single-hash format

    cs = detect_changes(_snap(a), _snap(a_reencode), new_pic_hash=a_reencode)
    expect("re-encode is NOT a change", cs.profile_pic_changed is False)

    cs = detect_changes(_snap(a), _snap(different), new_pic_hash=different)
    expect("different avatar IS a change", cs.profile_pic_changed is True)
    expect("change records both fingerprints",
           cs.old_pic_hash == a and cs.new_pic_hash == different)

    # Legacy raw-SHA256 baseline (pre-upgrade) vs a new fingerprint: silent
    # baseline, never a one-off false alarm on the first post-deploy sweep.
    cs = detect_changes(_snap(legacy_sha), _snap(a), new_pic_hash=a)
    expect("legacy sha256 -> v2 is a silent baseline", cs.profile_pic_changed is False)

    # Legacy 64-bit "p:" dHash baseline vs a new v2 fingerprint: also silent —
    # the formats aren't comparable, so the upgrade must not cry wolf once.
    cs = detect_changes(_snap(legacy_dhash), _snap(a), new_pic_hash=a)
    expect("legacy p: dHash -> v2 is a silent baseline", cs.profile_pic_changed is False)

    # No prior hash → baseline recorded, no alert.
    cs = detect_changes(_snap(None), _snap(a), new_pic_hash=a)
    expect("first observation is a silent baseline",
           cs.profile_pic_changed is False and cs.new_pic_hash == a)

    # The URL asset-id signal flows through detect_changes via the snapshots.
    base = _fp()
    subtle = _fp(dhash=(1 << 30) - 1, ahash=(1 << 5) - 1, mean=0x82)
    cs = detect_changes(
        _snap(base, URL_A), _snap(subtle, URL_B), new_pic_hash=subtle
    )
    expect("subtle swap with a new asset id IS a change via detect_changes",
           cs.profile_pic_changed is True)
    cs = detect_changes(
        _snap(base, URL_A), _snap(subtle, URL_A_ROTATED), new_pic_hash=subtle
    )
    expect("same subtle diff with a rotated-only URL is NOT a change",
           cs.profile_pic_changed is False)


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
    """Returns a scripted HashedMedia per hash_url call (one per sweep).

    A scripted None simulates a failed download (hash_url returns None)."""

    def __init__(self, phashes: list, path: Path) -> None:
        self._phashes = phashes
        self._path = path
        self.i = 0

    async def hash_url(self, url: str, username: str):
        phash = self._phashes[self.i]
        self.i += 1
        if phash is None:
            return None
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

        avatars = _make_avatars()
        # Real v2 fingerprints: sweep 1 baselines, sweep 2 is a CDN re-encode of
        # the SAME avatar (different size/quality), sweep 3 is a genuinely
        # different avatar. Sweep 3's tentative change triggers the
        # confirmation re-download, which sees a re-encode of the SAME new
        # avatar — so the change is confirmed and alerts.
        same = perceptual_hash(_encode(avatars[0], size=320, quality=85))
        reencode = perceptual_hash(_encode(avatars[0], size=150, quality=35))
        swapped = perceptual_hash(_encode(avatars[2], size=320, quality=85))
        swapped_reencode = perceptual_hash(_encode(avatars[2], size=150, quality=35))
        hasher = SeqHasher([same, reencode, swapped, swapped_reencode], pic)

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
        expect(
            "sweep3 confirmed via a second download (4 downloads total)",
            hasher.i == 4, f"hash_url called {hasher.i}x",
        )


# ---------- Part D: the confirmation re-check ("check the pic again") --------
# A tentative change must survive a SECOND independent download before it may
# alert. A one-off glitch (corrupt payload / CDN flicker that reads as a
# different picture once) is suppressed; a real swap still alerts, one sweep
# later at worst.

async def test_confirmation_recheck() -> None:
    parsed = {
        "username": "glitchy", "full_name": "G", "biography": "",
        "followers_count": 10, "following_count": 5, "posts_count": 0,
        "reels_count": 0, "story_count": 0, "is_private": True,
        "is_verified": False, "is_business": False,
        "profile_pic_url": "http://cdn/pic.jpg", "external_url": None,
        "instagram_id": "1000",
    }
    async with get_session() as session:
        session.add(MonitoredAccount(username="glitchy", active=True))

    with tempfile.TemporaryDirectory() as tmp:
        pic = Path(tmp) / "pic.jpg"
        pic.write_bytes(b"\xff\xd8\xfffake")

        avatars = _make_avatars()
        baseline = perceptual_hash(_encode(avatars[0], size=320, quality=85))
        baseline_re = perceptual_hash(_encode(avatars[0], size=150, quality=35))
        glitch = perceptual_hash(_encode(avatars[1], size=320, quality=85))
        swapped = perceptual_hash(_encode(avatars[2], size=320, quality=85))
        swapped_re = perceptual_hash(_encode(avatars[2], size=150, quality=35))

        # sweep1: baseline. sweep2: first download GLITCHES to a different
        # picture, the confirmation re-download sees the real (unchanged)
        # avatar → suppressed. sweep3: both downloads agree on a new avatar
        # → confirmed change.
        hasher = SeqHasher(
            [baseline, glitch, baseline_re, swapped, swapped_re], pic
        )

        notifier = AsyncMock()
        notifier.send_text = AsyncMock(return_value=True)
        notifier.send_document = AsyncMock(return_value=True)
        notifier.create_forum_topic = AsyncMock(return_value=None)

        service = MonitorService(
            instagram=FakeInstagram(parsed), hasher=hasher,
            notifier=notifier, stories=None,
        )

        r1 = await service.check_username("glitchy")
        expect("recheck sweep1 baselines", r1.get("first_seen") is True, repr(r1))

        r2 = await service.check_username("glitchy")
        expect(
            "one-off glitch is SUPPRESSED (no change reported)",
            r2.get("changed") is False, repr(r2),
        )
        expect(
            "glitch sends no photo",
            notifier.send_document.call_count == 0,
            f"send_document called {notifier.send_document.call_count}x",
        )
        expect("glitch consumed the confirmation download", hasher.i == 3)

        # The baseline must have survived the glitch (carried forward, not
        # overwritten with the glitch fingerprint).
        async with get_session() as session:
            from sqlalchemy import select
            snaps = (await session.execute(
                select(AccountSnapshot)
                .where(AccountSnapshot.username == "glitchy")
                .order_by(AccountSnapshot.id)
            )).scalars().all()
        expect("baseline survives the glitch",
               snaps[-1].profile_pic_hash == baseline,
               repr(snaps[-1].profile_pic_hash))

        r3 = await service.check_username("glitchy")
        expect(
            "real swap confirmed by second download - change reported",
            r3.get("changed") is True, repr(r3),
        )
        expect(
            "confirmed swap sends the photo exactly once",
            notifier.send_document.call_count == 1,
            f"send_document called {notifier.send_document.call_count}x",
        )


# ---------- Part E: the URL-identity signal across sweeps --------------------
# A new avatar upload changes the URL's asset id. Even when the pic DOWNLOAD
# fails that sweep (blocked CDN egress), the stored baseline URL must NOT
# absorb the new id — the change stays pending and fires on the next sweep
# that manages to fingerprint the picture, even a change too subtle for the
# strong perceptual floors.

async def test_url_signal_sweep() -> None:
    parsed = {
        "username": "urlcase", "full_name": "U", "biography": "",
        "followers_count": 10, "following_count": 5, "posts_count": 0,
        "reels_count": 0, "story_count": 0, "is_private": True,
        "is_verified": False, "is_business": False,
        "profile_pic_url": URL_A, "external_url": None,
        "instagram_id": "2000",
    }
    async with get_session() as session:
        session.add(MonitoredAccount(username="urlcase", active=True))

    with tempfile.TemporaryDirectory() as tmp:
        pic = Path(tmp) / "pic.jpg"
        pic.write_bytes(b"\xff\xd8\xfffake")

        base = _fp()
        # Subtle real swap: under every strong floor, over the reduced floors.
        subtle = _fp(dhash=(1 << 30) - 1, ahash=(1 << 5) - 1, mean=0x82)
        # sweep1: baseline. sweep2: download FAILS while IG already serves the
        # new URL. sweep3: fingerprint lands (tentative + confirmation).
        hasher = SeqHasher([base, None, subtle, subtle], pic)

        notifier = AsyncMock()
        notifier.send_text = AsyncMock(return_value=True)
        notifier.send_document = AsyncMock(return_value=True)
        notifier.create_forum_topic = AsyncMock(return_value=None)

        service = MonitorService(
            instagram=FakeInstagram(parsed), hasher=hasher,
            notifier=notifier, stories=None,
        )

        r1 = await service.check_username("urlcase")
        expect("url sweep1 baselines", r1.get("first_seen") is True, repr(r1))

        # Instagram now serves the NEW upload's URL…
        parsed["profile_pic_url"] = URL_B
        # …but the download fails this sweep.
        r2 = await service.check_username("urlcase")
        expect("failed download reports no change", r2.get("changed") is False, repr(r2))
        from sqlalchemy import select
        async with get_session() as session:
            snaps = (await session.execute(
                select(AccountSnapshot)
                .where(AccountSnapshot.username == "urlcase")
                .order_by(AccountSnapshot.id)
            )).scalars().all()
        expect(
            "failed download does NOT absorb the new URL into the baseline",
            snaps[-1].profile_pic_url == URL_A,
            repr(snaps[-1].profile_pic_url),
        )
        expect("failed download keeps the baseline hash",
               snaps[-1].profile_pic_hash == base)

        r3 = await service.check_username("urlcase")
        expect(
            "subtle swap fires once the new upload is fingerprinted",
            r3.get("changed") is True, repr(r3),
        )
        expect(
            "url-signal change sends the photo exactly once",
            notifier.send_document.call_count == 1,
            f"send_document called {notifier.send_document.call_count}x",
        )
        expect("url-signal change consumed the confirmation download",
               hasher.i == 4, f"hash_url called {hasher.i}x")
        async with get_session() as session:
            snaps = (await session.execute(
                select(AccountSnapshot)
                .where(AccountSnapshot.username == "urlcase")
                .order_by(AccountSnapshot.id)
            )).scalars().all()
        expect("baseline advances to the new fingerprint after the alert",
               snaps[-1].profile_pic_hash == subtle)
        expect("baseline advances to the new URL after the alert",
               snaps[-1].profile_pic_url == URL_B)


async def main() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    test_perceptual_hash()
    test_url_asset_id()
    test_detect_changes()
    await test_media_send_file_lifecycle()
    await test_full_sweep()
    await test_confirmation_recheck()
    await test_url_signal_sweep()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("All profile-picture regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
