#!/usr/bin/env python3
"""Decide Buffer Threads publish plan (rolling 250 / 24h cap).

Only today's local output folder is eligible. Previous-day posts are never
queued after the next Maldives day starts. After Generate (and schedule
backups), enqueue today's pending posts so Buffer releases them before local
midnight.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from publish_limits import (
    schedule_window_seconds_until_midnight,
    seconds_until_local_midnight,
    today_folder_name,
    today_local,
    today_output_dir,
)
from threads_limits import (
    DAILY_LIMIT,
    count_posted_last_24h,
    needs_publish,
    parse_channel_ids,
    quota_left_24h,
)

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
STATE_FILE = Path(os.getenv("THREADS_STATE_FILE", "threads-posted.json"))
# Safety ceiling only; normal runs drain all of today's pending under the 24h quota.
MAX_PER_SCHEDULE_TICK = max(1, int(os.getenv("THREADS_MAX_PER_SCHEDULE_TICK", "250")))
MAX_PER_GENERATE_TICK = max(1, int(os.getenv("THREADS_MAX_PER_GENERATE_TICK", "250")))
# Prefer scheduling across the rest of today (clamped to local midnight).
PREFERRED_SCHEDULE_WINDOW_SECONDS = max(
    120, int(os.getenv("THREADS_SCHEDULE_WINDOW_SECONDS", "86400"))
)
SECONDARY_DELAY_SECONDS = max(0, min(600, int(os.getenv("THREADS_SECONDARY_DELAY_SECONDS", "120"))))


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


def count_pending(state: dict) -> int:
    """Count unpublished posts under today's output folder only."""
    posted = state.get("posted", {})
    needed = parse_channel_ids()
    day_dir = today_output_dir(OUTPUT_DIR)
    if day_dir is None:
        return 0
    pending = 0
    for image in day_dir.glob("*.png"):
        relative = image.as_posix()
        existing = posted.get(relative) if isinstance(posted, dict) else None
        if not needs_publish(existing, needed):
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


def skip(reason: str, pending: int, used_24h: int, quota_left: int) -> int:
    write_output(
        should_publish="false",
        mode="none",
        max_posts=0,
        drain_seconds=0,
        pending=pending,
        used_24h=used_24h,
        quota_left=quota_left,
        local_date=today_local().isoformat(),
        reason=reason,
    )
    return 0


def main() -> int:
    event_name = os.getenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    manual_max = os.getenv("MANUAL_MAX_POSTS", "").strip()
    state = load_state()
    pending = count_pending(state)
    used_24h = count_posted_last_24h(state)
    quota_left = quota_left_24h(state)
    today_name = today_folder_name()
    until_midnight = seconds_until_local_midnight()
    schedule_window = schedule_window_seconds_until_midnight(
        preferred=PREFERRED_SCHEDULE_WINDOW_SECONDS,
        min_seconds=max(120, SECONDARY_DELAY_SECONDS + 60),
    )

    if pending <= 0:
        return skip("queue_empty_today_only", pending, used_24h, quota_left)

    if quota_left <= 0:
        return skip(
            f"threads_rolling_24h_limit_{DAILY_LIMIT}_reached",
            pending,
            used_24h,
            0,
        )

    # Do not schedule into tomorrow — leftover today posts are dropped at day rollover.
    if schedule_window <= 0:
        return skip(
            f"too_close_to_midnight_skip_spill_{until_midnight}s_left_today_{today_name}",
            pending,
            used_24h,
            quota_left,
        )

    # After Generate: schedule every remaining story from today before midnight.
    if event_name == "workflow_run":
        max_posts = min(pending, quota_left, MAX_PER_GENERATE_TICK)
        write_output(
            should_publish="true",
            mode="batch",
            max_posts=max_posts,
            drain_seconds=0,
            pending=pending,
            used_24h=used_24h,
            quota_left=quota_left,
            local_date=today_local().isoformat(),
            reason="after_generate_today_drain_before_midnight",
        )
        return 0

    if event_name == "schedule":
        max_posts = min(pending, quota_left, MAX_PER_SCHEDULE_TICK)
        write_output(
            should_publish="true",
            mode="batch",
            max_posts=max_posts,
            drain_seconds=0,
            pending=pending,
            used_24h=used_24h,
            quota_left=quota_left,
            local_date=today_local().isoformat(),
            reason="schedule_today_drain_before_midnight",
        )
        return 0

    if manual_max.lower() == "all" or manual_max == "":
        max_posts = min(pending, quota_left, MAX_PER_GENERATE_TICK)
        write_output(
            should_publish="true",
            mode="batch",
            max_posts=max_posts,
            drain_seconds=0,
            pending=pending,
            used_24h=used_24h,
            quota_left=quota_left,
            local_date=today_local().isoformat(),
            reason="manual_today_drain_before_midnight",
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
        local_date=today_local().isoformat(),
        reason="manual_batch_today_only",
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
