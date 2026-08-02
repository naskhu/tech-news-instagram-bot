#!/usr/bin/env python3
"""Decide Buffer publish mode with a calendar-day Instagram cap.

Pending photos from kept output days (yesterday + today) are eligible. Hard
cap: INSTAGRAM_DAILY_LIMIT (default 49). Runs stay gentle early, then drain
remaining posts before local midnight.
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
    kept_output_dirs,
    quota_left_today,
    seconds_until_local_midnight,
    today_local,
)

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
STATE_FILE = Path(os.getenv("INSTAGRAM_STATE_FILE", "instagram-posted.json"))
# Gentle ticks early in the day; may rise automatically before midnight.
MAX_PER_SCHEDULE_TICK = max(1, int(os.getenv("MAX_PER_SCHEDULE_TICK", "2")))
MAX_PER_GENERATE_TICK = max(1, int(os.getenv("MAX_PER_GENERATE_TICK", "2")))
# Keep drain windows under workflow timeout-minutes (75).
ACTIONS_SAFE_DRAIN_SECONDS = max(600, int(os.getenv("ACTIONS_SAFE_DRAIN_SECONDS", str(55 * 60))))
# When less than this many seconds remain, drain all remaining under the day cap.
FORCE_DRAIN_WITHIN_SECONDS = max(600, int(os.getenv("FORCE_DRAIN_WITHIN_SECONDS", str(3 * 3600))))


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
    # Include today + previous kept day so leftovers are not stranded.
    for day_dir in kept_output_dirs(OUTPUT_DIR):
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


def drain_seconds_for(max_posts: int) -> int:
    until_midnight = max(300, seconds_until_local_midnight() - 120)
    window = min(until_midnight, ACTIONS_SAFE_DRAIN_SECONDS)
    if max_posts <= 1:
        return min(900, window)
    return max(600, min(window, max_posts * 900))


def choose_tick(pending: int, quota_left: int, base_tick: int) -> tuple[int, str]:
    """Gentle early; drain remaining under day cap before folder delete at midnight."""
    left = min(pending, quota_left)
    if left <= 0:
        return 0, "none"

    until = max(0, seconds_until_local_midnight() - 600)
    # Schedule runs about every 30 minutes.
    approx_ticks_left = max(1, until // 1800)
    gentle_capacity = approx_ticks_left * max(1, base_tick)

    if until <= FORCE_DRAIN_WITHIN_SECONDS or left > gentle_capacity:
        return left, "drain_before_midnight"
    return min(left, base_tick), "gentle"


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
        return skip("queue_empty_kept_days", pending, used_today, quota_left)

    if quota_left <= 0:
        return skip(
            f"calendar_day_limit_{DAILY_LIMIT}_reached",
            pending,
            used_today,
            0,
        )

    if event_name == "workflow_run":
        max_posts, mode_tag = choose_tick(pending, quota_left, MAX_PER_GENERATE_TICK)
        write_output(
            should_publish="true",
            mode="drain",
            max_posts=max_posts,
            drain_seconds=drain_seconds_for(max_posts),
            pending=pending,
            used_today=used_today,
            quota_left=quota_left,
            local_date=today_local().isoformat(),
            reason=f"after_generate_{mode_tag}",
        )
        return 0

    if event_name == "schedule":
        max_posts, mode_tag = choose_tick(pending, quota_left, MAX_PER_SCHEDULE_TICK)
        write_output(
            should_publish="true",
            mode="drain",
            max_posts=max_posts,
            drain_seconds=drain_seconds_for(max_posts),
            pending=pending,
            used_today=used_today,
            quota_left=quota_left,
            local_date=today_local().isoformat(),
            reason=f"schedule_{mode_tag}",
        )
        return 0

    # Manual: "all" drains remaining under the day cap so nothing is left for delete.
    if manual_max.lower() == "all" or manual_max == "":
        max_posts = min(pending, quota_left, DAILY_LIMIT)
        write_output(
            should_publish="true",
            mode="drain",
            max_posts=max_posts,
            drain_seconds=drain_seconds_for(max_posts),
            pending=pending,
            used_today=used_today,
            quota_left=quota_left,
            local_date=today_local().isoformat(),
            reason="manual_drain_before_midnight",
        )
        return 0

    requested = max(1, int(manual_max or "1"))
    max_posts = min(requested, pending, quota_left, DAILY_LIMIT)
    write_output(
        should_publish="true",
        mode="batch",
        max_posts=max_posts,
        drain_seconds=0,
        pending=pending,
        used_today=used_today,
        quota_left=quota_left,
        local_date=today_local().isoformat(),
        reason="manual_batch_today_only_day_cap",
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
