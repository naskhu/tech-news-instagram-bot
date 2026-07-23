#!/usr/bin/env python3
"""Decide publish mode: drain queue within an hour, or a small batch."""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
STATE_FILE = Path(os.getenv("INSTAGRAM_STATE_FILE", "instagram-posted.json"))


def load_posted() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    posted = data.get("posted", {})
    return set(posted.keys()) if isinstance(posted, dict) else set()


def count_pending() -> int:
    posted = load_posted()
    pending = 0
    for image in OUTPUT_DIR.glob("**/*.png"):
        if image.as_posix() in posted:
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
    pending = count_pending()

    if pending <= 0:
        write_output(
            should_publish="false",
            mode="none",
            max_posts=0,
            drain_seconds=0,
            pending=pending,
            reason="queue_empty",
        )
        return 0

    # After Generate: clear the whole queue randomly within ~1 hour.
    if event_name == "workflow_run":
        write_output(
            should_publish="true",
            mode="drain",
            max_posts=pending,
            drain_seconds=3300,
            pending=pending,
            reason="after_generate_drain_within_hour",
        )
        return 0

    # Frequent schedule backup: drain leftovers in a long window so a new
    # day / backlog clears quickly instead of only ~8 minutes per tick.
    if event_name == "schedule":
        write_output(
            should_publish="true",
            mode="drain",
            max_posts=pending,
            drain_seconds=3000,
            pending=pending,
            reason="schedule_drain_within_hour",
        )
        return 0

    # Manual: "all" or a high number drains; otherwise publish a batch.
    if manual_max.lower() == "all":
        write_output(
            should_publish="true",
            mode="drain",
            max_posts=pending,
            drain_seconds=3300,
            pending=pending,
            reason="manual_drain_all",
        )
        return 0

    max_posts = max(1, int(manual_max or "1"))
    max_posts = min(max_posts, pending)
    write_output(
        should_publish="true",
        mode="batch",
        max_posts=max_posts,
        drain_seconds=0,
        pending=pending,
        reason="manual_batch",
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # Keep Actions logs concise.
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
