#!/usr/bin/env python3
"""Shared Instagram/Buffer publishing limits (calendar day in local timezone)."""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Maldives local day (user timezone). Override with PUBLISH_TIMEZONE if needed.
DEFAULT_TZ = "Indian/Maldives"
DAILY_LIMIT = max(1, min(49, int(os.getenv("INSTAGRAM_DAILY_LIMIT", "49"))))
# Keep today + previous day by default; older output folders are deleted.
KEEP_OUTPUT_DAYS = max(1, min(14, int(os.getenv("KEEP_OUTPUT_DAYS", "2"))))
# Do not post before this local hour (0-23). Default 8:00 local.
POSTING_START_HOUR = max(0, min(23, int(os.getenv("POSTING_START_HOUR", "8"))))
# Optional hard pause while Instagram action-blocks the account (ISO date YYYY-MM-DD local).
# Example: PUBLISH_RESUME_DATE=2026-07-26 means posting allowed from that local midnight.
PUBLISH_RESUME_DATE = os.getenv("PUBLISH_RESUME_DATE", "2026-07-26").strip()
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))


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
    """Count Buffer create attempts on the current local calendar day.

    Includes error/sending statuses so failed creates still consume the daily
    Instagram budget and cannot flood the Buffer queue past DAILY_LIMIT.
    """
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


def today_folder_name() -> str:
    """YYYY-MM-DD folder name for the current local publish day."""
    return today_local().isoformat()


def today_output_dir(output_dir: Path | None = None) -> Path | None:
    """Today's output folder if it exists; otherwise None."""
    root = output_dir if output_dir is not None else OUTPUT_DIR
    day_dir = root / today_folder_name()
    return day_dir if day_dir.is_dir() else None


def next_local_midnight() -> datetime:
    """Next local midnight (publish timezone)."""
    local = now_local()
    return datetime.combine(local.date() + timedelta(days=1), time(0, 0), tzinfo=local.tzinfo)


def kept_output_day_names() -> list[str]:
    """Local calendar days still kept under output/ (today first, then older)."""
    today = today_local()
    return [(today - timedelta(days=offset)).isoformat() for offset in range(KEEP_OUTPUT_DAYS)]


def kept_output_dirs(output_dir: Path | None = None) -> list[Path]:
    """Existing kept day folders, oldest first so leftovers clear before new posts."""
    root = output_dir if output_dir is not None else OUTPUT_DIR
    dirs: list[Path] = []
    for name in reversed(kept_output_day_names()):
        day_dir = root / name
        if day_dir.is_dir():
            dirs.append(day_dir)
    return dirs


def count_today_output_posts() -> int:
    """How many generated PNGs already exist under output/<today>/."""
    day_dir = OUTPUT_DIR / today_folder_name()
    if not day_dir.is_dir():
        return 0
    return sum(1 for _ in day_dir.glob("*.png"))


def is_today_output_path(path: Path | str) -> bool:
    """True when path is under output/YYYY-MM-DD for today's local date."""
    text = path.as_posix() if isinstance(path, Path) else str(path)
    parts = Path(text).parts
    today = today_folder_name()
    for index, part in enumerate(parts):
        if part == today:
            return True
        if part == "output" and index + 1 < len(parts) and parts[index + 1] == today:
            return True
    return False


def schedule_window_seconds_until_midnight(
    *,
    preferred: int,
    min_seconds: int = 120,
    reserve_seconds: int = 180,
) -> int:
    """Prefer `preferred` window, but never schedule past local midnight.

    Returns 0 when there is not enough time left before midnight (including
    reserve) so callers can skip instead of spilling into the next day.
    """
    until_midnight = max(0, seconds_until_local_midnight() - max(0, reserve_seconds))
    if until_midnight < max(1, min_seconds):
        return 0
    return min(preferred, until_midnight)


def clamp_due_at_before_local_midnight(
    due_at: str | None,
    *,
    reserve_seconds: int = 180,
) -> str | None:
    """Clamp a UTC dueAt ISO string to strictly before local midnight."""
    if not due_at:
        return due_at
    text = due_at.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return due_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    deadline = next_local_midnight().astimezone(timezone.utc) - timedelta(
        seconds=max(0, reserve_seconds)
    )
    if dt > deadline:
        dt = deadline
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
