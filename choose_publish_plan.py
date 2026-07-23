#!/usr/bin/env python3
"""Decide Buffer publish mode with a rolling 50-post / 24h Instagram cap."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
STATE_FILE = Path(os.getenv("INSTAGRAM_STATE_FILE", "instagram-posted.json"))
# Buffer's documented Instagram limit (posts + reels + stories per 24h).
DAILY_LIMIT = max(1, int(os.getenv("INSTAGRAM_DAILY_LIMIT", "50")))
# Keep each Actions tick gentle so we don't burst inside the day.
MAX_PER_SCHEDULE_TICK = max(1, int(os.getenv("MAX_PER_SCHEDULE_TICK", "2")))
MAX_PER_GENERATE_TICK = max(1, int(os.getenv("MAX_PER_GENERATE_TICK", "3")))


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"posted": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"posted": {}}
    if not isinstance(data, dict):
        return {"posted": {}}
    data.setdefault("posted", {})
    return data


def parse_published_at(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def count_posted_last_24h(state: dict) -> int:
    cutoff = time.time() - 24 * 60 * 60
    posted = state.get("posted", {})
    if not isinstance(posted, dict):
        return 0
    count = 0
    for entry in posted.values():
        if not isinstance(entry, dict):
            continue
        # Only count Buffer/Meta Actions publishes toward the Buffer daily cap.
        publisher = str(entry.get("publisher", "buffer")).lower()
        if publisher not in {"buffer", "meta", ""}:
            continue
        ts = parse_published_at(entry.get("published_at_utc"))
        if ts is not None and ts >= cutoff:
            count += 1
    return count


def count_pending(state: dict) -> int:
    posted = state.get("posted", {})
    keys = set(posted.keys()) if isinstance(posted, dict) else set()
    pending = 0
    for image in OUTPUT_DIR.glob("**/*.png"):
        if image.as_posix() in keys:
            continue
        if image.with_suffix(".txt").exists():
            pending += 1
    return pending


def write_output(**values: object) -> None:
    github_output = os.getenv("GITHUB_OUTPUT")
    lines = [f"{key}={value}" for key, value in values.items()]
    text = "\n".join(lines) + "\n"
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(text)
    print(text, end="")


def main() -> int:
    event_name = os.getenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    manual_max = os.getenv("MANUAL_MAX_POSTS", "").strip()
    state = load_state()
    pending = count_pending(state)
    used_24h = count_posted_last_24h(state)
    quota_left = max(0, DAILY_LIMIT - used_24h)

    if pending <= 0:
        write_output(
            should_publish="false",
            mode="none",
            max_posts=0,
            drain_seconds=0,
            pending=pending,
            used_24h=used_24h,
            quota_left=quota_left,
            reason="queue_empty",
        )
        return 0

    if quota_left <= 0:
        write_output(
            should_publish="false",
            mode="none",
            max_posts=0,
            drain_seconds=0,
            pending=pending,
            used_24h=used_24h,
            quota_left=0,
            reason="daily_limit_50_reached",
        )
        return 0

    # Spread across the day: each tick only ships a few posts with spacing.
    # ~2 posts / 30 min ≈ up to ~50 over 24h when the queue is full.
    if event_name == "workflow_run":
        max_posts = min(pending, quota_left, MAX_PER_GENERATE_TICK)
        write_output(
            should_publish="true",
            mode="drain",
            max_posts=max_posts,
            drain_seconds=max(900, max_posts * 900),
            pending=pending,
            used_24h=used_24h,
            quota_left=quota_left,
            reason="after_generate_paced_daily_cap",
        )
        return 0

    if event_name == "schedule":
        max_posts = min(pending, quota_left, MAX_PER_SCHEDULE_TICK)
        write_output(
            should_publish="true",
            mode="drain",
            max_posts=max_posts,
            drain_seconds=max(600, max_posts * 800),
            pending=pending,
            used_24h=used_24h,
            quota_left=quota_left,
            reason="schedule_paced_daily_cap",
        )
        return 0

    if manual_max.lower() == "all":
        max_posts = min(pending, quota_left)
        write_output(
            should_publish="true",
            mode="drain",
            max_posts=max_posts,
            drain_seconds=max(1200, max_posts * 600),
            pending=pending,
            used_24h=used_24h,
            quota_left=quota_left,
            reason="manual_drain_up_to_daily_cap",
        )
        return 0

    requested = max(1, int(manual_max or "1"))
    max_posts = min(requested, pending, quota_left)
    write_output(
        should_publish="true",
        mode="batch",
        max_posts=max_posts,
        drain_seconds=0,
        pending=pending,
        used_24h=used_24h,
        quota_left=quota_left,
        reason="manual_batch_daily_cap",
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # Keep Actions logs concise.
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
