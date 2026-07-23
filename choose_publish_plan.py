#!/usr/bin/env python3
"""Decide whether a scheduled publish should run, and with what post limit."""

from __future__ import annotations

import json
import os
import random
import sys
import time
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


def recommended_limit(pending: int, event_name: str) -> int:
    """Pick a safe publish batch size for the trigger type."""
    if pending <= 0:
        return 0
    if event_name == "schedule":
        # One post per chosen daytime slot keeps Instagram pacing natural.
        return 1
    if event_name == "workflow_run":
        return 1
    # Manual runs may clear a larger backlog intentionally.
    if pending <= 20:
        return 1
    if pending <= 40:
        return 2
    return 3


def schedule_probability(pending: int) -> float:
    """Higher backlog => more likely to publish this hourly slot."""
    if pending <= 0:
        return 0.0
    if pending <= 5:
        return 0.20
    if pending <= 15:
        return 0.35
    if pending <= 30:
        return 0.50
    return 0.70


def write_output(should_publish: bool, max_posts: int, pending: int, reason: str) -> None:
    github_output = os.getenv("GITHUB_OUTPUT")
    lines = [
        f"should_publish={'true' if should_publish else 'false'}",
        f"max_posts={max_posts}",
        f"pending={pending}",
        f"reason={reason}",
    ]
    text = "\n".join(lines) + "\n"
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(text)
    print(text, end="")


def main() -> int:
    event_name = os.getenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    manual_max = os.getenv("MANUAL_MAX_POSTS", "").strip()
    pending = count_pending()
    limit = recommended_limit(pending, event_name)

    if pending <= 0:
        write_output(False, 0, pending, "queue_empty")
        return 0

    if event_name == "workflow_dispatch":
        max_posts = max(1, int(manual_max or str(limit)))
        max_posts = min(max_posts, pending)
        write_output(True, max_posts, pending, "manual_dispatch")
        return 0

    if event_name == "workflow_run":
        write_output(True, 1, pending, "after_generate")
        return 0

    # schedule: randomly decide whether this daytime hour posts.
    probability = schedule_probability(pending)
    roll = random.random()
    if roll > probability:
        write_output(
            False,
            0,
            pending,
            f"random_skip roll={roll:.3f} p={probability:.3f} limit_would_be={limit}",
        )
        return 0

    # Small jitter so posts don't always fire at :23 exactly.
    delay = random.randint(0, 240)
    print(f"Random delay before publish: {delay}s")
    if delay:
        time.sleep(delay)

    write_output(
        True,
        limit,
        pending,
        f"random_publish roll={roll:.3f} p={probability:.3f}",
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # Keep Actions logs concise.
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
