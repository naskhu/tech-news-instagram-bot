#!/usr/bin/env python3
"""Decide Buffer Instagram publish mode with a calendar-day cap.

Only today's local output folder is eligible (never previous days). Hard cap:
INSTAGRAM_DAILY_LIMIT (default 49) counts every Buffer create attempt today,
including errors, so the Instagram queue cannot be flooded. Each automatic run
sends at most 1–2 randomly chosen today posts — never a backlog dump.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from publish_limits import (
    DAILY_LIMIT,
    before_posting_window,
    cooldown_active,
    count_posted_today,
    quota_left_today,
    today_folder_name,
    today_local,
    today_output_dir,
)

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
STATE_FILE = Path(os.getenv("INSTAGRAM_STATE_FILE", "instagram-posted.json"))
# Hard ceiling per Actions tick — never dump the day's remaining quota into Buffer.
MAX_PER_SCHEDULE_TICK = max(1, min(2, int(os.getenv("MAX_PER_SCHEDULE_TICK", "2"))))
MAX_PER_GENERATE_TICK = max(1, min(2, int(os.getenv("MAX_PER_GENERATE_TICK", "1"))))


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
    """Count unpublished PNGs under today's folder only."""
    posted = state.get("posted", {})
    keys = set(posted.keys()) if isinstance(posted, dict) else set()
    day_dir = today_output_dir(OUTPUT_DIR)
    if day_dir is None:
        return 0
    pending = 0
    for image in day_dir.glob("*.png"):
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


def skip(reason: str, pending: int, used_today: int, quota_left: int) -> int:
    write_output(
        should_publish="false",
        mode="none",
        max_posts=0,
        drain_seconds=0,
        pending=pending,
        used_today=used_today,
        quota_left=quota_left,
        local_date=today_local().isoformat(),
        reason=reason,
    )
    return 0


def emit(max_posts: int, pending: int, used_today: int, quota_left: int, reason: str) -> int:
    write_output(
        should_publish="true",
        mode="batch",
        max_posts=max_posts,
        drain_seconds=0,
        pending=pending,
        used_today=used_today,
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
    used_today = count_posted_today(state)
    quota_left = quota_left_today(state)
    today_name = today_folder_name()

    paused, pause_reason = cooldown_active()
    if paused:
        return skip(pause_reason, pending, used_today, quota_left)

    too_early, early_reason = before_posting_window()
    if too_early and event_name != "workflow_dispatch":
        return skip(early_reason, pending, used_today, quota_left)

    if pending <= 0:
        return skip(f"queue_empty_today_only_{today_name}", pending, used_today, quota_left)

    if quota_left <= 0:
        return skip(
            f"calendar_day_limit_{DAILY_LIMIT}_reached",
            pending,
            used_today,
            0,
        )

    if event_name == "workflow_run":
        max_posts = min(pending, quota_left, MAX_PER_GENERATE_TICK)
        return emit(
            max_posts,
            pending,
            used_today,
            quota_left,
            "after_generate_random_today_1or2",
        )

    if event_name == "schedule":
        max_posts = min(pending, quota_left, MAX_PER_SCHEDULE_TICK)
        return emit(
            max_posts,
            pending,
            used_today,
            quota_left,
            "schedule_random_today_1or2",
        )

    # Manual: default 1–2, never dump the full remaining day quota.
    if manual_max.lower() == "all":
        max_posts = min(pending, quota_left, MAX_PER_SCHEDULE_TICK)
        return emit(
            max_posts,
            pending,
            used_today,
            quota_left,
            "manual_all_capped_to_1or2_no_backlog_dump",
        )

    requested = max(1, int(manual_max or "2"))
    max_posts = min(requested, pending, quota_left, MAX_PER_SCHEDULE_TICK)
    return emit(
        max_posts,
        pending,
        used_today,
        quota_left,
        "manual_random_today_1or2",
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
