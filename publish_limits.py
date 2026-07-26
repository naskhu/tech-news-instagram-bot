#!/usr/bin/env python3
"""Shared Instagram/Buffer publishing limits (calendar day in local timezone)."""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# Maldives local day (user timezone). Override with PUBLISH_TIMEZONE if needed.
DEFAULT_TZ = "Indian/Maldives"
DAILY_LIMIT = max(1, min(50, int(os.getenv("INSTAGRAM_DAILY_LIMIT", "50"))))
# Do not post before this local hour (0-23). Default 8:00 local.
POSTING_START_HOUR = max(0, min(23, int(os.getenv("POSTING_START_HOUR", "8"))))
# Optional hard pause while Instagram action-blocks the account (ISO date YYYY-MM-DD local).
# Example: PUBLISH_RESUME_DATE=2026-07-26 means posting allowed from that local midnight.
PUBLISH_RESUME_DATE = os.getenv("PUBLISH_RESUME_DATE", "2026-07-26").strip()


def publish_tz() -> ZoneInfo:
    name = os.getenv("PUBLISH_TIMEZONE", DEFAULT_TZ).strip() or DEFAULT_TZ
    return ZoneInfo(name)


def now_local() -> datetime:
    return datetime.now(publish_tz())


def today_local() -> date:
    return now_local().date()


def parse_published_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(publish_tz())


def count_posted_today(state: dict) -> int:
    """Count successful Buffer/Meta publishes on the current local calendar day."""
    today = today_local()
    posted = state.get("posted", {})
    if not isinstance(posted, dict):
        return 0
    count = 0
    for entry in posted.values():
        if not isinstance(entry, dict):
            continue
        publisher = str(entry.get("publisher", "buffer")).lower()
        if publisher not in {"buffer", "meta", ""}:
            continue
        status = str(entry.get("buffer_status") or "").strip().lower()
        if status in {"error", "failed", "rejected"}:
            continue
        dt = parse_published_at(entry.get("published_at_utc"))
        if dt is not None and dt.date() == today:
            count += 1
    return count


def quota_left_today(state: dict) -> int:
    return max(0, DAILY_LIMIT - count_posted_today(state))


def cooldown_active() -> tuple[bool, str]:
    """Return (paused, reason). Instagram action-block cool-down."""
    if not PUBLISH_RESUME_DATE:
        return False, ""
    try:
        resume = date.fromisoformat(PUBLISH_RESUME_DATE)
    except ValueError:
        return False, ""
    today = today_local()
    if today < resume:
        return (
            True,
            f"Publishing paused until {resume.isoformat()} local "
            f"({publish_tz().key}) while Instagram action-block cools down.",
        )
    return False, ""


def before_posting_window() -> tuple[bool, str]:
    local = now_local()
    start = time(hour=POSTING_START_HOUR)
    if local.time() < start:
        return (
            True,
            f"Before posting window (starts {POSTING_START_HOUR:02d}:00 "
            f"{publish_tz().key}).",
        )
    return False, ""


def seconds_until_local_midnight() -> int:
    local = now_local()
    tomorrow = datetime.combine(local.date() + timedelta(days=1), time(0, 0), tzinfo=local.tzinfo)
    return max(0, int((tomorrow - local).total_seconds()))
