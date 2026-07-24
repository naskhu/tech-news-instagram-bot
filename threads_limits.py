#!/usr/bin/env python3
"""Buffer Threads publishing limits (rolling 24h, separate from Instagram)."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

# Soft cap under Buffer's documented Threads limit (250 / rolling 24h).
DAILY_LIMIT = max(1, min(240, int(os.getenv("THREADS_DAILY_LIMIT", "240"))))


def parse_published_at(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def count_posted_last_24h(state: dict) -> int:
    cutoff = time.time() - 24 * 60 * 60
    posted = state.get("posted", {})
    if not isinstance(posted, dict):
        return 0
    count = 0
    for entry in posted.values():
        if not isinstance(entry, dict):
            continue
        publisher = str(entry.get("publisher", "buffer_threads")).lower()
        if publisher not in {"buffer_threads", "buffer", ""}:
            continue
        ts = parse_published_at(entry.get("published_at_utc"))
        if ts is not None and ts >= cutoff:
            count += 1
    return count


def quota_left_24h(state: dict) -> int:
    return max(0, DAILY_LIMIT - count_posted_last_24h(state))
