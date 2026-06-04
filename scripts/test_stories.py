"""End-to-end smoke test for StoriesClient against a real public account."""

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

from app.monitor.stories import StoriesClient  # noqa: E402

USERNAME = sys.argv[1] if len(sys.argv) > 1 else "saudibox1"


async def main() -> int:
    print(f"=== Testing StoriesClient against @{USERNAME} ===\n")
    client = StoriesClient()
    failures = 0

    try:
        # 1. Resolve user PK
        print("[1] Resolving user PK via userInfoByUsername…")
        pk = await client._get_user_pk(USERNAME)
        if pk:
            print(f"    OK -> pk = {pk}\n")
        else:
            print("    FAIL: no PK returned (account may be private or API down)\n")
            failures += 1

        # 2. Fetch active stories
        print("[2] Fetching active stories…")
        stories = await client.fetch_stories(USERNAME)
        print(f"    Got {len(stories)} story item(s)")
        for s in stories[:3]:
            print(f"      - pk={s.pk} type={s.media_type} taken_at={s.taken_at}")
            print(f"        url={s.url[:80]}…")
        print()

        # 3. Fetch highlights (list + items per highlight)
        print("[3] Fetching highlights (list + items)…")
        highlights = await client.fetch_highlights(USERNAME)
        print(f"    Got {len(highlights)} total highlight item(s)")
        seen_titles = {h.highlight_title for h in highlights}
        print(f"    Across {len(seen_titles)} distinct highlight reel(s):")
        for title in list(seen_titles)[:10]:
            count = sum(1 for h in highlights if h.highlight_title == title)
            print(f"      - {title!r}: {count} item(s)")
        for h in highlights[:3]:
            print(f"      sample: pk={h.pk} type={h.media_type} title={h.highlight_title!r}")
            print(f"              url={h.url[:80]}…")
        print()

        # 4. Try downloading one story and one highlight item to verify the URL works
        print("[4] Testing download…")
        sample_image = next(
            (s for s in stories + highlights if s.media_type == "image"), None
        )
        sample_video = next(
            (s for s in stories + highlights if s.media_type == "video"), None
        )

        if sample_image:
            print(f"    Downloading image pk={sample_image.pk}…")
            path = await client.download(sample_image, USERNAME)
            if path and path.exists():
                size = path.stat().st_size
                print(f"    OK -> {path} ({size:,} bytes)")
            else:
                print("    FAIL: image download returned None")
                failures += 1
        else:
            print("    No image item available to test")

        if sample_video:
            print(f"    Downloading video pk={sample_video.pk}…")
            path = await client.download(sample_video, USERNAME)
            if path and path.exists():
                size = path.stat().st_size
                print(f"    OK -> {path} ({size:,} bytes)")
            else:
                print("    FAIL: video download returned None")
                failures += 1
        else:
            print("    No video item available to test")
        print()

        # 5. PK-based deduplication sanity check (re-fetch returns same PKs)
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

    print()
    if failures == 0:
        print("=== ALL CHECKS PASSED ===")
        return 0
    print(f"=== {failures} CHECK(S) FAILED ===")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
