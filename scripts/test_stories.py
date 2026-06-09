"""End-to-end smoke test for StoriesClient against a real public account.

Usage:  python scripts/test_stories.py [username]

Hits the live, login-free saveinsta.to source for story/highlight media and
Instagram's anonymous graphql reel query for the highlight catalog.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Allow running from repo root: `python scripts/test_stories.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Stub required env vars so app.config.Settings() validates.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "0")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("MEDIA_DIR", "./data/media_test")

from app.monitor.instagram import InstagramClient  # noqa: E402
from app.monitor.stories import StoriesClient  # noqa: E402

USERNAME = sys.argv[1] if len(sys.argv) > 1 else "nasa"


async def main() -> int:
    print(f"=== Testing StoriesClient against @{USERNAME} ===\n")
    client = StoriesClient()
    instagram = InstagramClient()
    failures = 0

    try:
        # 1. Resolve the highlight catalog (id -> title) via anonymous graphql.
        print("[1] Resolving user id + highlight catalog via graphql…")
        profile = await instagram.fetch_profile(USERNAME)
        instagram_id = profile.parsed.get("instagram_id") if profile.parsed else None
        catalog: dict[str, str] = {}
        if instagram_id:
            reel_user = await instagram.fetch_reel_user(str(instagram_id))
            catalog = dict(reel_user.get("highlights", {})) if reel_user else {}
            print(f"    OK -> id={instagram_id}, {len(catalog)} highlight reel(s)\n")
        else:
            print("    FAIL: could not resolve user id (private or fetch failed)\n")
            failures += 1

        # 2. Fetch active stories.
        print("[2] Fetching active stories…")
        stories = await client.fetch_stories(USERNAME)
        print(f"    Got {len(stories)} story item(s)")
        for s in stories[:3]:
            print(f"      - pk={s.pk} type={s.media_type}")
            print(f"        url={s.url[:80]}…")
        print()

        # 3. Fetch highlight items across every reel in the catalog.
        print("[3] Fetching highlight items per reel…")
        highlights: list = []
        for hid, title in list(catalog.items())[:10]:
            items = await client.fetch_highlight_items(USERNAME, hid, title)
            print(f"      - {title!r} ({hid}): {len(items)} item(s)")
            highlights.extend(items)
        print(f"    Got {len(highlights)} total highlight item(s)\n")

        # 4. Try downloading one image and one video to verify the URL works.
        print("[4] Testing download…")
        sample_image = next(
            (s for s in stories + highlights if s.media_type == "image"), None
        )
        sample_video = next(
            (s for s in stories + highlights if s.media_type == "video"), None
        )

        for label, sample in (("image", sample_image), ("video", sample_video)):
            if not sample:
                print(f"    No {label} item available to test")
                continue
            print(f"    Downloading {label} pk={sample.pk}…")
            path = await client.download(sample, USERNAME)
            if path and path.exists():
                print(f"    OK -> {path} ({path.stat().st_size:,} bytes)")
            else:
                print(f"    FAIL: {label} download returned None")
                failures += 1
        print()

        # 5. PK-based deduplication sanity check (re-fetch returns same PKs).
        print("[5] Re-fetching stories — PKs should be stable (dedup key)…")
        stories2 = await client.fetch_stories(USERNAME)
        pks_a = {s.pk for s in stories}
        pks_b = {s.pk for s in stories2}
        if stories and pks_a == pks_b:
            print(f"    OK -> {len(pks_a)} PKs identical across calls")
        elif not stories:
            print("    SKIP -> no stories to compare")
        else:
            print(f"    FAIL: PKs differed ({len(pks_a)} vs {len(pks_b)})")
            failures += 1

    finally:
        await client.close()
        await instagram.close()

    print()
    if failures == 0:
        print("=== ALL CHECKS PASSED ===")
        return 0
    print(f"=== {failures} CHECK(S) FAILED ===")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
