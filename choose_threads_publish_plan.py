#!/usr/bin/env python3
"""Decide Buffer Threads publish plan (rolling 240 / 24h soft cap)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from threads_limits import DAILY_LIMIT, count_posted_last_24h, quota_left_24h

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
STATE_FILE = Path(os.getenv("THREADS_STATE_FILE", "threads-posted.json"))
# Gentle hourly ticks via Buffer (≈240 across the day under the soft cap).
MAX_PER_SCHEDULE_TICK = max(1, int(os.getenv("THREADS_MAX_PER_SCHEDULE_TICK", "8")))
MAX_PER_GENERATE_TICK = max(1, int(os.getenv("THREADS_MAX_PER_GENERATE_TICK", "8")))


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

    if event_name == "workflow_run":
        max_posts = min(pending, quota_left, MAX_PER_GENERATE_TICK)
        write_output(
            should_publish="true",
            mode="drain",
            max_posts=max_posts,
            # ~2–3 min average spacing for up to 8 posts (~15–20 min/tick).
            drain_seconds=max(900, max_posts * 150),
            pending=pending,
            used_24h=used_24h,
            quota_left=quota_left,
            reason="after_generate_threads_cap",
        )
        return 0

    if event_name == "schedule":
        max_posts = min(pending, quota_left, MAX_PER_SCHEDULE_TICK)
        write_output(
            should_publish="true",
            mode="drain",
            max_posts=max_posts,
            drain_seconds=max(900, max_posts * 150),
            pending=pending,
            used_24h=used_24h,
            quota_left=quota_left,
            reason="schedule_threads_cap",
        )
        return 0

    if manual_max.lower() == "all":
        max_posts = min(pending, quota_left)
        write_output(
            should_publish="true",
            mode="drain",
            max_posts=max_posts,
            drain_seconds=max(900, max_posts * 120),
            pending=pending,
            used_24h=used_24h,
            quota_left=quota_left,
            reason="manual_drain_up_to_threads_cap",
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
