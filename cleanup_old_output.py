#!/usr/bin/env python3
"""Remove old output day folders and optional unpublished leftovers.

Keeps today + the previous local calendar day under output/ by default.
Older output/YYYY-MM-DD folders are deleted entirely. Published history stays
in instagram-posted.json / threads-posted.json.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import timedelta
from pathlib import Path

from publish_limits import KEEP_OUTPUT_DAYS, today_folder_name, today_local

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
STATE_FILE = Path(os.getenv("INSTAGRAM_STATE_FILE", "instagram-posted.json"))
THREADS_STATE_FILE = Path(os.getenv("THREADS_STATE_FILE", "threads-posted.json"))


def load_posted_keys(*paths: Path) -> set[str]:
    keys: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        posted = data.get("posted", {})
        if isinstance(posted, dict):
            keys.update(str(key) for key in posted.keys())
    return keys


def clear_unpublished() -> int:
    posted = load_posted_keys(STATE_FILE, THREADS_STATE_FILE)
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


def clear_old_day_folders(*, keep_days: int = KEEP_OUTPUT_DAYS) -> int:
    """Delete output day folders older than the keep window.

    With keep_days=2, keeps today + yesterday; deletes older folders.
    """
    today = today_local()
    cutoff = (today - timedelta(days=max(0, keep_days - 1))).isoformat()
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
        if name >= cutoff:
            continue

        shutil.rmtree(day_dir)
        removed_dirs += 1
        print(f"Removed old output day folder: {day_dir.as_posix()}")

    print(
        f"Keeping output day folder(s) on/after {cutoff} "
        f"(today={today_folder_name()}, keep_days={keep_days})"
    )
    return removed_dirs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--clear-unpublished",
        action="store_true",
        help="Delete queued image/caption/json files not yet in posted state files",
    )
    parser.add_argument(
        "--clear-old-days",
        action="store_true",
        help=(
            f"Delete output/YYYY-MM-DD folders older than the last {KEEP_OUTPUT_DAYS} "
            "local calendar day(s). Default keeps today + previous day."
        ),
    )
    args = parser.parse_args()
    if not args.clear_unpublished and not args.clear_old_days:
        parser.error("Pass --clear-unpublished and/or --clear-old-days")

    total = 0
    if args.clear_unpublished:
        total += clear_unpublished()
    if args.clear_old_days:
        total += clear_old_day_folders(keep_days=KEEP_OUTPUT_DAYS)
    print(f"Cleanup finished. Items/folders touched: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
