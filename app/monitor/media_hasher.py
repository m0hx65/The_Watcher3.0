"""Profile picture downloader + SHA256 hasher."""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import settings
from app.utils.logger import logger

CHROME_IMPERSONATE = "chrome120"

# Prefix marking a v3 fingerprint, so change detection can tell it apart from
# every older format (raw SHA256, the 64-bit "p:" dHash, the "p2:" dHash+aHash)
# and treat the switch as a silent baseline rather than a spurious "changed".
#
# The v3 fingerprint is "p3:<hex>": the picture itself, as a 16×16 grayscale
# thumbnail (256 bytes). Change detection compares the two thumbnails pixel by
# pixel (see change_detector.pic_difference).
#
# Why not the bit hashes any more: a dHash/aHash bit is the SIGN of a tiny
# difference, and in a dark or low-contrast picture those differences are one
# or two gray levels, which JPEG noise flips. Measured 2026-10-22 on 21 real
# photos, each as Instagram serves an avatar (1440/1080/320/150/96px at JPEG
# quality 25-92) and as-is, darkened and flattened: p2 called the SAME picture
# changed in 61 of 1,323 version pairs (dHash up to 63 bits, aHash up to 60,
# against a 40-bit floor), nearly all dark or flat ones, i.e. a typical selfie
# avatar. Averaging pixels instead of thresholding them cancels that noise:
# the same pairs differ by at most 1.62 gray levels RMS (median 0.84), while
# distinct photos differ by 10.5 or more, and no distinct pair came closer than
# 2.8 (two washed-out textures).
PHASH_PREFIX = "p3:"

# Side of the stored thumbnail. Measured at 8, 12, 16, 24 and 32: the margin
# between same-picture noise and distinct pictures was ~1.6-1.7× at every size,
# so the smallest that still keeps a face's layout (eyes, hair, background) wins.
_THUMB_SIZE = 16
# Intermediate resolution every source is first reduced to, so a 1440px HD
# avatar and a 150px thumbnail of one picture take the same path to the
# thumbnail.
_WORK_SIZE = 64


@dataclass
class HashedMedia:
    sha256: str
    byte_size: int
    content_type: Optional[str]
    local_path: Path
    source_url: str
    # Perceptual fingerprint of the decoded image. Profile-picture change
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

    MEASURED 2026-08-12: it no longer works on a signed URL. Instagram's `oh`
    signature covers the `stp` transform, so the stripped variant comes back
    `403 text/plain "URL signature mismatch"` — confirmed twice on one avatar
    (strip s150x150 -> 403; reuse the s150 signature on the s100 transform ->
    403; each variant fetched with ITS OWN signature -> 200). Every size of an
    avatar is separately signed, so there is no URL-editing path to a larger
    image; the anonymous ceiling stays profile_pic_url_hd (~320px), or the
    media downloader's full-resolution avatar.

    The attempt is kept because hash_url falls back correctly (403 -> None ->
    retry the original), and an unsigned/legacy URL would still benefit — but
    on today's URLs it costs one guaranteed-403 round trip per download.
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


def _thumbnail(data: bytes) -> Image.Image:
    """Decode image bytes into the canonical grayscale thumbnail.

      * exif_transpose: honor any rotation flag so orientation can't differ.
      * convert("L"): drop color; a recolor that keeps the light/dark layout is
        not what "changed their picture" means.
      * LANCZOS to the working size, then a BOX average to the thumbnail: every
        thumbnail pixel is the mean of a block of the picture, which is what
        cancels JPEG noise.
    """
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img) or img
    gray = img.convert("L").resize((_WORK_SIZE, _WORK_SIZE), Image.LANCZOS)
    return gray.resize((_THUMB_SIZE, _THUMB_SIZE), Image.BOX)


def perceptual_hash(data: bytes) -> Optional[str]:
    """The picture's fingerprint, as ``p3:<hex of a 16×16 grayscale thumbnail>``.

    Stable across every size and JPEG quality Instagram serves one avatar at,
    dark and low-contrast pictures included (see PHASH_PREFIX for the
    measurement). Returns None for non-image payloads (e.g. a CDN error page),
    which the caller treats as "no new fingerprint", never a change.
    """
    try:
        thumb = _thumbnail(data)
    except (UnidentifiedImageError, OSError, ValueError, TypeError) as exc:
        logger.warning("Could not fingerprint image ({} bytes): {}", len(data), exc)
        return None
    return PHASH_PREFIX + thumb.tobytes().hex()


def image_size(path: Path) -> Optional[tuple[int, int]]:
    """(width, height) of an image file, read from its header only.

    What a source CALLS full resolution and what it serves are different
    claims; this is the measured one. None for anything that isn't a
    decodable image.
    """
    try:
        with Image.open(path) as img:
            return img.size
    except (UnidentifiedImageError, OSError, ValueError):
        return None


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

        content = response.content
        ext = _ext_from_content_type(response.headers.get("Content-Type", "")) or ".jpg"
        account_dir = settings.media_path / username

        def _persist() -> tuple[str, Path, Optional[str]]:
            # SHA256 + disk write + PIL decode/resize/hash are all CPU/disk
            # bound — run them in a worker thread so a sweep hashing dozens of
            # avatars never stalls the event loop (and with it every Telegram
            # handler and in-flight download).
            digest = hashlib.sha256(content).hexdigest()
            account_dir.mkdir(parents=True, exist_ok=True)
            path = account_dir / f"{digest}{ext}"
            if not path.exists():
                path.write_bytes(content)
            return digest, path, perceptual_hash(content)

        digest, path, phash = await asyncio.to_thread(_persist)

        return HashedMedia(
            sha256=digest,
            byte_size=len(content),
            content_type=response.headers.get("Content-Type"),
            local_path=path,
            source_url=url,
            phash=phash,
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
