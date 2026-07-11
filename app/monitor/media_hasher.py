"""Profile picture downloader + SHA256 hasher."""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException
from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

from app.config import settings
from app.utils.logger import logger

CHROME_IMPERSONATE = "chrome120"

# Prefix marking a v2 perceptual fingerprint, so change detection can tell it
# apart from a legacy raw-SHA256 hash OR the old 64-bit "p:" dHash and treat the
# format switch as a silent baseline rather than a spurious "changed".
#
# The v2 fingerprint is "p2:<dhash>:<ahash>" — two independent 256-bit perceptual
# hashes of the SAME normalized image (see perceptual_hash). Two hashes that must
# BOTH agree before a change is reported makes the decision noise-proof.
PHASH_PREFIX = "p2:"

# Side of the perceptual-hash grid. 16 → 16*16 = 256 bits per hash. Much finer
# than the old 8 (64 bits): genuinely different avatars separate cleanly so real
# swaps are never missed, while the normalization below keeps re-encodes at ~0.
_HASH_SIZE = 16
# Canonical grayscale working resolution. Both a 1440px HD avatar and a 150px
# thumbnail of the SAME picture collapse to this identical intermediate, so it no
# longer matters which CDN variant we happened to download — the fingerprint is
# the same either way. This is the single biggest fix for the "said it changed
# when it didn't" flapping.
_WORK_SIZE = 64


@dataclass
class HashedMedia:
    sha256: str
    byte_size: int
    content_type: Optional[str]
    local_path: Path
    source_url: str
    # Perceptual (difference) hash of the decoded image. Profile-picture change
    # detection compares THIS, not sha256: Instagram's CDN re-encodes the same
    # avatar at different sizes/qualities per signed URL, so the raw bytes (and
    # thus sha256) differ on every fetch even when the picture is unchanged.
    # None when the payload isn't a decodable image.
    phash: Optional[str] = None


# Leading numeric core of an avatar basename, e.g.
# "463908845_1234567890123456_987654321_n.jpg" -> "463908845_1234567890123456_987654321".
# The trailing size-class letter and extension are excluded — they can vary per
# CDN variant of the same upload.
_ASSET_ID_RE = re.compile(r"^(\d+(?:_\d+)+)")


def pic_asset_id(url: Optional[str]) -> Optional[str]:
    """Stable identity of an Instagram avatar URL, or None when unknowable.

    The basename of a t51.*-19 avatar URL carries the numeric id of the avatar
    UPLOAD. The signed query params, the CDN shard host, and the size variant
    all rotate per fetch, but this numeric core only changes when the user
    actually sets a new picture — so comparing it across sweeps is an exact
    "did they upload a new avatar" signal that no perceptual threshold can
    miss. Non-CDN URLs (e.g. a saveinsta JWT href) yield None, which disables
    the signal for that comparison rather than faking a change.
    """
    if not url or ("fbcdn.net" not in url and "cdninstagram.com" not in url):
        return None
    basename = urlparse(url).path.rsplit("/", 1)[-1]
    m = _ASSET_ID_RE.match(basename)
    return m.group(1) if m else None


def _strip_cdn_size(url: str) -> Optional[str]:
    """Return a size-constraint-free CDN URL, or None if no modification was made.

    Instagram/Facebook CDN URLs encode a target resolution in two ways:
      - Path segment:  /v/t51.2885-19/s320x320/HASH_n.jpg   (older format)
      - Query param:   ?stp=dst-jpg_s320x320_e35             (newer format)

    Removing the constraint asks the CDN for the original stored image rather
    than a downscaled thumbnail.  The technique is the same one used by
    InstaRaider (github.com/akurtovic/InstaRaider).
    """
    if "fbcdn.net" not in url and "cdninstagram.com" not in url:
        return None

    modified = False

    # 1. Strip /s{W}x{H}/ from the URL path  (e.g. /s150x150/, /s320x320/)
    new_url = re.sub(r"/s\d+x\d+/", "/", url)
    if new_url != url:
        modified = True
        url = new_url

    # 2. Strip size specs from the `stp` query parameter
    #    e.g. stp=dst-jpg_s320x320_e35  →  stp=dst-jpg_e35
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    if "stp" in params:
        new_stp = []
        for stp in params["stp"]:
            stripped = re.sub(r"_?[sp]\d+x\d+", "", stp).strip("_")
            if stripped != stp:
                modified = True
            new_stp.append(stripped or stp)
        if modified:
            params["stp"] = new_stp
            url = urlunparse(parsed._replace(query=urlencode(params, doseq=True)))

    return url if modified else None


def _normalized_gray(data: bytes) -> Image.Image:
    """Decode image bytes into a canonical grayscale image for hashing.

    Every step here exists to make the SAME avatar produce the SAME hash no
    matter which CDN re-encode we fetched:
      * exif_transpose — honor any rotation flag so orientation can't differ.
      * convert("L")   — drop color; we compare structure, not exact RGB.
      * resize to a fixed working size — collapse 1440px / 320px / 150px
        variants of one picture into one identical intermediate.
      * GaussianBlur   — smooth away JPEG 8×8 block noise, the thing that used
        to flip near-zero gradient bits and fake a "change".
    """
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img) or img
    img = img.convert("L").resize((_WORK_SIZE, _WORK_SIZE), Image.LANCZOS)
    return img.filter(ImageFilter.GaussianBlur(radius=1))


def _dhash_bits(gray: Image.Image, size: int) -> int:
    """Difference hash: sign of each left→right gradient. Robust to brightness."""
    small = gray.resize((size + 1, size), Image.LANCZOS)
    px = list(small.getdata())
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | int(px[base + col] > px[base + col + 1])
    return bits


def _ahash_bits(gray: Image.Image, size: int) -> int:
    """Average hash: each pixel above the image mean. Captures overall layout."""
    small = gray.resize((size, size), Image.LANCZOS)
    px = list(small.getdata())
    avg = sum(px) / len(px)
    bits = 0
    for p in px:
        bits = (bits << 1) | int(p > avg)
    return bits


def perceptual_hash(data: bytes, *, size: int = _HASH_SIZE) -> Optional[str]:
    """Combined perceptual fingerprint of an image, as ``p2:<dhash>:<ahash>:<mean>``.

    Computes, over one normalized image (see _normalized_gray), THREE
    complementary signals:
      * dhash — a 256-bit difference hash (gradient structure),
      * ahash — a 256-bit average hash (overall light/dark layout), and
      * mean  — the overall grayscale brightness (0–255, two hex digits).
    They capture different aspects of the picture, so change detection can demand
    strong agreement before declaring a change — a single noisy signal can never
    raise a false alarm. The mean is what lets a flat solid-color avatar (whose
    gradient/layout hashes are uninformatively all-zero) still register a swap to
    a different solid color.

    The aggressive normalization keeps two CDN re-encodes of one avatar within a
    couple of bits on both hashes and ~3 levels of brightness, while genuinely
    different pictures sit far apart — so every threshold lives in a wide
    no-man's-land (see change_detector). Returns None for non-image payloads
    (e.g. a CDN error page), which the caller treats as "no new fingerprint",
    never a change.
    """
    try:
        gray = _normalized_gray(data)
    except (UnidentifiedImageError, OSError, ValueError, TypeError) as exc:
        logger.warning("Could not perceptually hash image ({} bytes): {}", len(data), exc)
        return None
    width = (size * size + 3) // 4  # hex digits needed for size*size bits
    dhash = _dhash_bits(gray, size)
    ahash = _ahash_bits(gray, size)
    pixels = list(gray.getdata())
    mean = round(sum(pixels) / len(pixels)) & 0xFF
    return f"{PHASH_PREFIX}{dhash:0{width}x}:{ahash:0{width}x}:{mean:02x}"


class MediaHasher:
    """Downloads images, computes SHA256, and stores them on disk."""

    def __init__(self) -> None:
        session_kwargs = {
            "impersonate": CHROME_IMPERSONATE,
            "timeout": (10.0, float(settings.request_timeout)),
            "allow_redirects": True,
        }
        if settings.proxy:
            session_kwargs["proxy"] = settings.proxy
        self._client = AsyncSession(**session_kwargs)

    async def close(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> "MediaHasher":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def hash_url(self, url: str, username: str) -> Optional[HashedMedia]:
        """Download an image URL and persist it. Returns None on failure.

        Tries to fetch a size-constraint-free (full-resolution) version of the
        URL first.  Falls back to the original URL if that attempt fails.
        """
        if not url:
            return None

        logger.info("Downloading pic for @{} — source URL: {}", username, url)

        # Try the full-resolution version of the CDN URL first.
        hd_url = _strip_cdn_size(url)
        if hd_url:
            logger.info("Trying size-stripped URL for @{}: {}", username, hd_url)
            result = await self._fetch_and_store(hd_url, username)
            if result is not None:
                logger.info(
                    "HD image for @{}: {} bytes", username, result.byte_size
                )
                return result
            logger.info("HD URL failed for @{}, falling back to original", username)
        else:
            logger.info("No CDN size constraint found in URL for @{} — using as-is", username)

        result = await self._fetch_and_store(url, username)
        if result:
            logger.info("Downloaded pic for @{}: {} bytes", username, result.byte_size)
        return result

    async def _fetch_and_store(self, url: str, username: str) -> Optional[HashedMedia]:
        """Download one URL, hash it, and persist to disk."""
        try:
            response = await self._client.get(
                url,
                headers={
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    "Referer": "https://www.instagram.com/",
                },
            )
        except RequestException as exc:
            logger.warning("Failed to download profile picture for @{}: {}", username, exc)
            return None

        if response.status_code != 200 or not response.content:
            logger.warning(
                "Bad image response for @{}: status={}, len={}",
                username, response.status_code, len(response.content or b""),
            )
            return None

        digest = hashlib.sha256(response.content).hexdigest()
        ext = _ext_from_content_type(response.headers.get("Content-Type", "")) or ".jpg"

        account_dir = settings.media_path / username
        account_dir.mkdir(parents=True, exist_ok=True)
        path = account_dir / f"{digest}{ext}"
        if not path.exists():
            path.write_bytes(response.content)

        return HashedMedia(
            sha256=digest,
            byte_size=len(response.content),
            content_type=response.headers.get("Content-Type"),
            local_path=path,
            source_url=url,
            phash=perceptual_hash(response.content),
        )


def _ext_from_content_type(ct: str) -> Optional[str]:
    ct = (ct or "").split(";")[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(ct)
