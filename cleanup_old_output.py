#!/usr/bin/env python3
"""Remove old output day folders and optional unpublished leftovers.

After local midnight, previous calendar-day image folders are deleted so the
generator builds a fresh daily queue. Published history stays in
instagram-posted.json.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from publish_limits import today_local

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
STATE_FILE = Path(os.getenv("INSTAGRAM_STATE_FILE", "instagram-posted.json"))


def load_posted_keys() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    posted = data.get("posted", {})
    return set(posted.keys()) if isinstance(posted, dict) else set()


def clear_unpublished() -> int:
    posted = load_posted_keys()
    removed = 0
    for image in list(OUTPUT_DIR.glob("**/*.png")):
        rel = image.as_posix()
        if rel in posted:
            continue
        for path in (image, image.with_suffix(".txt"), image.with_suffix(".json")):
            if path.exists():
                path.unlink()
                removed += 1
                print(f"Removed unpublished: {path.as_posix()}")
    return removed


def clear_old_day_folders(*, keep_today: bool = True) -> int:
    today = today_local().isoformat()
    removed_dirs = 0
    if not OUTPUT_DIR.exists():
        return 0
    for day_dir in sorted(OUTPUT_DIR.iterdir()):
        if not day_dir.is_dir():
            continue
        name = day_dir.name
        # Expect YYYY-MM-DD folders.
        if len(name) != 10 or name[4] != "-" or name[7] != "-":
            continue
        if keep_today and name >= today:
            continue
        shutil.rmtree(day_dir)
        removed_dirs += 1
        print(f"Removed old output day folder: {day_dir.as_posix()}")
    return removed_dirs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--clear-unpublished",
        action="store_true",
        help="Delete queued image/caption/json files not yet in instagram-posted.json",
    )
    parser.add_argument(
        "--clear-old-days",
        action="store_true",
        help="Delete output/YYYY-MM-DD folders before today's local date",
    )
    args = parser.parse_args()
    if not args.clear_unpublished and not args.clear_old_days:
        parser.error("Pass --clear-unpublished and/or --clear-old-days")

    total = 0
    if args.clear_unpublished:
        total += clear_unpublished()
    if args.clear_old_days:
        total += clear_old_day_folders(keep_today=True)
    print(f"Cleanup finished. Items/folders touched: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
