#!/usr/bin/env python3
"""Buffer Threads publishing limits (rolling 24h, separate from Instagram)."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

# Soft cap under Buffer's documented Threads limit (250 / rolling 24h) per channel.
# Count unique images (not per-channel fan-out) so dual profiles still land under ~240/day.
DAILY_LIMIT = max(1, min(240, int(os.getenv("THREADS_DAILY_LIMIT", "240"))))


def parse_channel_ids(raw: str | None = None) -> list[str]:
    """Parse comma/semicolon-separated Buffer Threads channel ids."""
    text = (raw if raw is not None else os.getenv("BUFFER_THREADS_CHANNEL_ID", "")).strip()
    ids: list[str] = []
    for part in text.replace(";", ",").split(","):
        channel_id = part.strip()
        if channel_id and channel_id not in ids:
            ids.append(channel_id)
    return ids


def backfill_missing_channels_enabled() -> bool:
    return os.getenv("THREADS_BACKFILL_MISSING_CHANNELS", "").strip() == "1"


def entry_done_channel_ids(entry: object) -> set[str]:
    """Return channel ids already recorded for one posted image entry."""
    if not isinstance(entry, dict):
        return set()
    channels = entry.get("channels")
    if isinstance(channels, dict):
        return {str(key) for key in channels.keys() if str(key).strip()}
    old_id = str(entry.get("channel_id") or "").strip()
    return {old_id} if old_id else set()


def is_fully_posted(entry: object, needed: list[str] | None = None) -> bool:
    required = needed if needed is not None else parse_channel_ids()
    if not required:
        return False
    return set(required).issubset(entry_done_channel_ids(entry))


def needs_publish(entry: object, needed: list[str] | None = None) -> bool:
    """True when this image should still be sent to one or more Threads channels."""
    required = needed if needed is not None else parse_channel_ids()
    if not required:
        return False
    if entry is None:
        return True
    if is_fully_posted(entry, required):
        return False
    done = entry_done_channel_ids(entry)
    if not done:
        return True
    # Multi-channel progress (channels map): finish any missing ids.
    if isinstance(entry, dict) and isinstance(entry.get("channels"), dict):
        return True
    # Legacy single-channel records: skip backfill unless explicitly enabled.
    return backfill_missing_channels_enabled()


def missing_channel_ids(entry: object, needed: list[str] | None = None) -> list[str]:
    required = needed if needed is not None else parse_channel_ids()
    if not needs_publish(entry, required):
        return []
    done = entry_done_channel_ids(entry)
    return [channel_id for channel_id in required if channel_id not in done]


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


def entry_latest_publish_ts(entry: dict) -> float | None:
    channels = entry.get("channels")
    latest: float | None = None
    if isinstance(channels, dict):
        for channel_entry in channels.values():
            if not isinstance(channel_entry, dict):
                continue
            ts = parse_published_at(channel_entry.get("published_at_utc"))
            if ts is not None and (latest is None or ts > latest):
                latest = ts
    ts = parse_published_at(entry.get("published_at_utc"))
    if ts is not None and (latest is None or ts > latest):
        latest = ts
    return latest


def count_posted_last_24h(state: dict) -> int:
    """Count unique images with any Threads publish in the last rolling 24h."""
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
        ts = entry_latest_publish_ts(entry)
        if ts is not None and ts >= cutoff:
            count += 1
    return count


def quota_left_24h(state: dict) -> int:
    return max(0, DAILY_LIMIT - count_posted_last_24h(state))
