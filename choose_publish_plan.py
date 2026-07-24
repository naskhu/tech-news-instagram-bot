#!/usr/bin/env python3
"""Decide Buffer publish mode with a calendar-day Instagram cap."""

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
    today_local,
)

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
STATE_FILE = Path(os.getenv("INSTAGRAM_STATE_FILE", "instagram-posted.json"))
# Gentle ticks so we don't burst inside the day (≈50 across ~15 waking hours).
MAX_PER_SCHEDULE_TICK = max(1, int(os.getenv("MAX_PER_SCHEDULE_TICK", "2")))
MAX_PER_GENERATE_TICK = max(1, int(os.getenv("MAX_PER_GENERATE_TICK", "2")))


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


def main() -> int:
    event_name = os.getenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    manual_max = os.getenv("MANUAL_MAX_POSTS", "").strip()
    state = load_state()
    pending = count_pending(state)
    used_today = count_posted_today(state)
    quota_left = quota_left_today(state)

    paused, pause_reason = cooldown_active()
    if paused:
        return skip(pause_reason, pending, used_today, quota_left)

    too_early, early_reason = before_posting_window()
    if too_early and event_name != "workflow_dispatch":
        # Manual runs can still test; automatic runs wait for the start hour.
        return skip(early_reason, pending, used_today, quota_left)

    if pending <= 0:
        return skip("queue_empty", pending, used_today, quota_left)

    if quota_left <= 0:
        return skip(
            f"calendar_day_limit_{DAILY_LIMIT}_reached",
            pending,
            used_today,
            0,
        )

    if event_name == "workflow_run":
        max_posts = min(pending, quota_left, MAX_PER_GENERATE_TICK)
        write_output(
            should_publish="true",
            mode="drain",
            max_posts=max_posts,
            drain_seconds=max(1200, max_posts * 1200),
            pending=pending,
            used_today=used_today,
            quota_left=quota_left,
            local_date=today_local().isoformat(),
            reason="after_generate_calendar_day_cap",
        )
        return 0

    if event_name == "schedule":
        max_posts = min(pending, quota_left, MAX_PER_SCHEDULE_TICK)
        write_output(
            should_publish="true",
            mode="drain",
            max_posts=max_posts,
            drain_seconds=max(900, max_posts * 1000),
            pending=pending,
            used_today=used_today,
            quota_left=quota_left,
            local_date=today_local().isoformat(),
            reason="schedule_calendar_day_cap",
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
            used_today=used_today,
            quota_left=quota_left,
            local_date=today_local().isoformat(),
            reason="manual_drain_up_to_day_cap",
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
        used_today=used_today,
        quota_left=quota_left,
        local_date=today_local().isoformat(),
        reason="manual_batch_day_cap",
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
