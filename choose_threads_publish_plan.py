#!/usr/bin/env python3
"""Decide Buffer Threads publish plan (rolling 250 / 24h cap).

After Generate (and on schedule backups), enqueue all pending posts so Buffer
can release them at random times within the next hour.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from threads_limits import (
    DAILY_LIMIT,
    count_posted_last_24h,
    needs_publish,
    parse_channel_ids,
    quota_left_24h,
)

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
STATE_FILE = Path(os.getenv("THREADS_STATE_FILE", "threads-posted.json"))
# Safety ceiling only; normal runs drain all pending under the 24h quota.
MAX_PER_SCHEDULE_TICK = max(1, int(os.getenv("THREADS_MAX_PER_SCHEDULE_TICK", "250")))
MAX_PER_GENERATE_TICK = max(1, int(os.getenv("THREADS_MAX_PER_GENERATE_TICK", "250")))


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
    posted = state.get("posted", {})
    needed = parse_channel_ids()
    pending = 0
    for image in OUTPUT_DIR.glob("**/*.png"):
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

    if pending <= 0:
        return skip("queue_empty", pending, used_24h, quota_left)

    if quota_left <= 0:
        return skip(
            f"threads_rolling_24h_limit_{DAILY_LIMIT}_reached",
            pending,
            used_24h,
            0,
        )

    # After Generate: schedule every pending story randomly inside the next hour.
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
            reason="after_generate_drain_all_1h",
        )
        return 0

    # Backup schedule: keep draining leftovers under the rolling cap.
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
            reason="schedule_drain_all_1h",
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
            reason="manual_drain_all_1h",
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
        reason="manual_batch_threads_cap",
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
