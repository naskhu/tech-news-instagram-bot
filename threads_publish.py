#!/usr/bin/env python3
"""Publish generated Tech News posts to Threads through Zernio.

Uses the same public git-hosted images as Instagram publishing. Tracks progress
in threads-posted.json so Instagram and Threads queues stay independent.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from threads_limits import (
    DAILY_LIMIT,
    count_posted_last_24h,
    missing_channel_ids,
    needs_publish,
    parse_channel_ids,
    quota_left_24h,
)
from publish_limits import (
    clamp_due_at_before_local_midnight,
    schedule_window_seconds_until_midnight,
    seconds_until_local_midnight,
    today_folder_name,
    today_output_dir,
)
from zernio_client import (
    ZernioDailyLimitReached,
    ZernioError,
    ZernioRateLimitReached,
    create_post,
    request_id,
)

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
STATE_FILE = Path(os.getenv("THREADS_STATE_FILE", "threads-posted.json"))
REPOSITORY = os.getenv("GITHUB_REPOSITORY", "naskhu/tech-news-instagram-bot")
BRANCH = os.getenv("GITHUB_REF_NAME", "main") or "main"
MAX_POSTS = max(1, int(os.getenv("MAX_POSTS_PER_RUN", "1")))
PUBLISH_MODE = os.getenv("PUBLISH_MODE", "batch").strip().lower() or "batch"
DRAIN_WITHIN_SECONDS = max(0, int(os.getenv("DRAIN_WITHIN_SECONDS", "0")))
COMMIT_STATE_EACH_POST = os.getenv("COMMIT_STATE_EACH_POST", "").strip() == "1"
THREADS_TOPIC = os.getenv("THREADS_TOPIC", "TechNews").strip() or "TechNews"
# Threads hard limit is 500 characters per message.
THREADS_MAX_CHARS = max(80, min(500, int(os.getenv("THREADS_MAX_CHARS", "500"))))
# FIFO Zernio scheduled times evenly spaced inside the next hour
# (clamped to local midnight). Oldest pending posts go out first.
PREFERRED_SCHEDULE_WINDOW_SECONDS = max(
    120, int(os.getenv("THREADS_SCHEDULE_WINDOW_SECONDS", "3600"))
)
SCHEDULE_WINDOW_SECONDS = schedule_window_seconds_until_midnight(
    preferred=PREFERRED_SCHEDULE_WINDOW_SECONDS,
    min_seconds=max(120, int(os.getenv("THREADS_SECONDARY_DELAY_SECONDS", "120")) + 60),
)
SCHEDULE_MIN_OFFSET_SECONDS = max(20, int(os.getenv("THREADS_SCHEDULE_MIN_OFFSET_SECONDS", "45")))
# Delay secondary profiles (e.g. naskhu) after primary (news.world.tech) dueAt.
SECONDARY_DELAY_SECONDS = max(0, min(600, int(os.getenv("THREADS_SECONDARY_DELAY_SECONDS", "120"))))
# Small pause between Zernio API create calls.
API_GAP_SECONDS = max(1, int(os.getenv("THREADS_API_GAP_SECONDS", "3")))
MIDNIGHT_RESERVE_SECONDS = max(0, int(os.getenv("THREADS_MIDNIGHT_RESERVE_SECONDS", "180")))


class DailyLimitReached(RuntimeError):
    """Threads daily scheduling limit was hit; retry later."""


class RateLimitReached(ZernioRateLimitReached):
    """Zernio API rate limit was hit; retry later."""


class MissingPostFiles(RuntimeError):
    """Queued image/caption disappeared (usually overnight cleanup or generate rebase)."""


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"posted": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read {STATE_FILE}: {exc}") from exc
    data.setdefault("posted", {})
    return data


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def generated_order_key(image: Path) -> tuple[str, str]:
    """Stable FIFO key from committed metadata, independent of checkout mtime."""
    metadata = image.with_suffix(".json")
    if metadata.exists():
        try:
            loaded = json.loads(metadata.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                generated = str(loaded.get("generated_utc") or "").strip()
                if generated:
                    return generated, image.as_posix()
        except (OSError, json.JSONDecodeError):
            pass
    # Legacy posts without generated_utc remain deterministic.
    return "", image.as_posix()


def list_unpublished(state: dict[str, Any]) -> list[tuple[Path, Path, Path | None]]:
    """Only today's local folder — never queue previous-day posts after rollover."""
    posted = state.get("posted", {})
    needed = parse_channel_ids()
    candidates: list[tuple[Path, Path, Path | None]] = []
    day_dir = today_output_dir(OUTPUT_DIR)
    if day_dir is None:
        print(f"No today output folder ({today_folder_name()}); Threads queue empty.")
        return candidates

    images = sorted(
        day_dir.glob("*.png"),
        key=generated_order_key,
    )
    for image in images:
        relative = image.as_posix()
        existing = posted.get(relative)
        if not needs_publish(existing, needed):
            continue

        caption = image.with_suffix(".txt")
        metadata = image.with_suffix(".json")
        if not image.exists() or not caption.exists():
            print(
                f"Skipping {relative}: image or caption missing on disk",
                file=sys.stderr,
            )
            continue

        candidates.append((image, caption, metadata if metadata.exists() else None))

    return candidates


def ensure_post_files(image: Path, caption: Path) -> None:
    missing = [path for path in (image, caption) if not path.exists()]
    if missing:
        raise MissingPostFiles(
            "Missing after queue refresh (generate cleanup/rebase?): "
            + ", ".join(path.as_posix() for path in missing)
        )


def discover_posts(state: dict[str, Any]) -> list[tuple[Path, Path, Path | None]]:
    return list_unpublished(state)[:MAX_POSTS]


def assert_daily_quota(state: dict[str, Any]) -> None:
    used = count_posted_last_24h(state)
    left = quota_left_24h(state)
    if left <= 0:
        raise DailyLimitReached(
            f"Threads rolling 24h cap reached ({used}/{DAILY_LIMIT}). "
            "Resume after older posts age out of the window."
        )
    print(f"Threads API usage (24h): {used}/{DAILY_LIMIT} ({left} left)")


def public_image_url(image: Path) -> str:
    encoded_path = "/".join(quote(part) for part in image.as_posix().split("/"))
    return f"https://raw.githubusercontent.com/{REPOSITORY}/{quote(BRANCH)}/{encoded_path}"


def wait_for_public_image(image_url: str, attempts: int = 12, delay_seconds: float = 5.0) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(image_url, timeout=30, stream=True)
            if response.ok and int(response.headers.get("Content-Length", "1")) > 0:
                response.close()
                print(f"Public git image URL is ready: {image_url}")
                return
            last_error = RuntimeError(f"HTTP {response.status_code} for {image_url}")
            response.close()
        except requests.RequestException as exc:
            last_error = exc

        print(f"Waiting for git-hosted image (attempt {attempt}/{attempts}): {last_error}")
        time.sleep(delay_seconds)

    raise MissingPostFiles(
        f"Image is not publicly available from git yet: {image_url} ({last_error})"
    )


def truncate_text(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: max(0, limit - 1)].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(".,;:") + "…"


def build_threads_caption(caption_file: Path, metadata: Path | None) -> str:
    """Short hook-first Threads caption: title, one beat, source, link."""
    try:
        raw = caption_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise MissingPostFiles(f"Caption disappeared: {caption_file.as_posix()}") from exc
    meta: dict[str, Any] = {}
    if metadata and metadata.exists():
        try:
            loaded = json.loads(metadata.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, json.JSONDecodeError):
            meta = {}

    title = str(meta.get("title") or "").strip()
    source = str(meta.get("source") or "").strip()
    url = str(meta.get("url") or "").strip()
    summary = str(meta.get("summary") or "").strip()

    if not title:
        for line in raw.splitlines():
            line = line.strip()
            if line:
                title = line
                break

    if not url:
        match = re.search(r"https?://\S+", raw)
        if match:
            url = match.group(0).rstrip(").,]")

    # First line is the scroll-stopping hook.
    hook = truncate_text(title, 160) if title else ""
    beat = ""
    if summary and summary.lower() not in (title or "").lower():
        beat = truncate_text(summary, 140)

    parts: list[str] = []
    if hook:
        parts.append(hook)
    if beat:
        parts.append(beat)
    if source:
        parts.append(f"Via {source}")
    if url:
        parts.append(url)

    body = "\n\n".join(parts).strip() or raw
    topic_line = f"#{THREADS_TOPIC.lstrip('#')}"
    room = THREADS_MAX_CHARS - len(topic_line) - 2
    body = truncate_text(body, max(40, room))
    return f"{body}\n\n{topic_line}"


def allocate_due_ats(count: int) -> list[str]:
    """FIFO dueAt times evenly spaced inside the next schedule window (default 1h).

    Oldest pending posts get the earliest slots. No random times.
    """
    if count <= 0:
        return []
    now = datetime.now(timezone.utc)
    lo = SCHEDULE_MIN_OFFSET_SECONDS
    # Leave room for secondary-channel delay after the primary dueAt.
    usable_window = max(lo + 1, SCHEDULE_WINDOW_SECONDS - SECONDARY_DELAY_SECONDS - 30)
    hi = max(lo + 1, usable_window)
    if count == 1:
        offsets = [lo]
    else:
        span = hi - lo
        offsets = [lo + int(round(i * span / (count - 1))) for i in range(count)]
        # Keep strictly non-decreasing when rounding collapses neighbors.
        for index in range(1, len(offsets)):
            if offsets[index] <= offsets[index - 1]:
                offsets[index] = min(hi, offsets[index - 1] + 1)
    return [
        (now + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        for offset in offsets
    ]


def shift_due_at(due_at: str | None, delay_seconds: int) -> str | None:
    """Return due_at shifted forward by delay_seconds, never past local midnight."""
    if not due_at or delay_seconds <= 0:
        return clamp_due_at_before_local_midnight(
            due_at, reserve_seconds=MIDNIGHT_RESERVE_SECONDS
        )
    text = due_at.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return due_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    shifted = (dt + timedelta(seconds=delay_seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return clamp_due_at_before_local_midnight(
        shifted, reserve_seconds=MIDNIGHT_RESERVE_SECONDS
    )


def publish_post(
    api_key: str,
    account_id: str,
    image: Path,
    caption_file: Path,
    metadata: Path | None,
    *,
    due_at: str | None = None,
) -> tuple[str, str | None]:
    ensure_post_files(image, caption_file)
    if metadata is not None and not metadata.exists():
        metadata = None
    caption = build_threads_caption(caption_file, metadata)
    if not caption:
        raise RuntimeError(f"Caption is empty after Threads formatting: {caption_file}")

    image_url = public_image_url(image)
    wait_for_public_image(image_url)

    if due_at:
        print(f"Creating Zernio Threads post for {image.as_posix()} scheduledFor={due_at}")
    else:
        print(f"Creating Zernio Threads post for {image.as_posix()} (publishNow)")

    try:
        created = create_post(
            api_key,
            platform="threads",
            account_id=account_id,
            content=caption,
            image_url=image_url,
            scheduled_for=due_at,
            publish_now=not bool(due_at),
            idempotency_key=request_id(
                "threads",
                account_id,
                image.as_posix(),
                due_at or "now",
            ),
        )
    except ZernioDailyLimitReached as exc:
        raise DailyLimitReached(str(exc)) from exc
    except ZernioRateLimitReached as exc:
        raise RateLimitReached(str(exc)) from exc
    except ZernioError as exc:
        raise RuntimeError(f"Zernio rejected Threads post: {exc}") from exc

    print(
        f"Zernio Threads post accepted: id={created.post_id} "
        f"status={created.status}"
    )
    return created.post_id, due_at


def record_post(
    state: dict[str, Any],
    account_id: str,
    image: Path,
    caption: Path,
    metadata: Path | None,
    post_id: str,
    *,
    due_at: str | None = None,
) -> None:
    relative = image.as_posix()
    existing = state["posted"].get(relative)
    entry: dict[str, Any]
    if isinstance(existing, dict):
        entry = dict(existing)
    else:
        entry = {}

    channels = entry.get("channels")
    if not isinstance(channels, dict):
        channels = {}
        # Migrate legacy single-channel records into the channels map.
        old_id = str(entry.get("channel_id") or "").strip()
        old_post_id = str(entry.get("buffer_post_id") or "").strip()
        if old_id and old_post_id:
            legacy: dict[str, Any] = {
                "buffer_post_id": old_post_id,
                "published_at_utc": entry.get("published_at_utc"),
                "buffer_mode": entry.get("buffer_mode"),
            }
            if entry.get("buffer_due_at_utc"):
                legacy["buffer_due_at_utc"] = entry.get("buffer_due_at_utc")
            channels[old_id] = legacy

    channel_entry: dict[str, Any] = {
        "zernio_post_id": post_id,
        "published_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if due_at:
        channel_entry["zernio_scheduled_for_utc"] = due_at
        channel_entry["zernio_mode"] = "scheduled"
    else:
        channel_entry["zernio_mode"] = "publishNow"
    channels[account_id] = channel_entry

    entry.update(
        {
            "channels": channels,
            "account_id": account_id,
            "zernio_post_id": post_id,
            "publisher": "zernio_threads",
            "caption_file": caption.as_posix(),
            "metadata_file": metadata.as_posix() if metadata else None,
            "image_url": public_image_url(image),
            "published_at_utc": channel_entry["published_at_utc"],
            "zernio_mode": channel_entry["zernio_mode"],
        }
    )
    if due_at:
        entry["zernio_scheduled_for_utc"] = due_at
    else:
        entry.pop("zernio_scheduled_for_utc", None)

    state["posted"][relative] = entry
    save_state(state)


def commit_state_to_git() -> None:
    if not COMMIT_STATE_EACH_POST:
        return

    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
    subprocess.run(
        [
            "git",
            "config",
            "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com",
        ],
        check=False,
    )
    subprocess.run(["git", "add", str(STATE_FILE)], check=False)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
    if diff.returncode == 0:
        return

    subprocess.run(
        ["git", "commit", "-m", "Record published Threads post"],
        check=False,
    )
    for attempt in range(1, 4):
        subprocess.run(["git", "fetch", "origin", "main"], check=False)
        rebase = subprocess.run(["git", "rebase", "origin/main"], check=False)
        if rebase.returncode != 0:
            subprocess.run(["git", "checkout", "--ours", str(STATE_FILE)], check=False)
            subprocess.run(["git", "add", str(STATE_FILE)], check=False)
            subprocess.run(
                ["git", "rebase", "--continue"],
                check=False,
                env={**os.environ, "GIT_EDITOR": "true"},
            )
        push = subprocess.run(["git", "push", "origin", "HEAD:main"], check=False)
        if push.returncode == 0:
            print("Threads publishing state pushed to git.")
            return
        time.sleep(attempt * 3)
    print("WARNING: could not push Threads publishing state after this post", file=sys.stderr)


def publish_one_channel(
    api_key: str,
    account_id: str,
    state: dict[str, Any],
    image: Path,
    caption: Path,
    metadata: Path | None,
    *,
    due_at: str | None = None,
) -> None:
    ensure_post_files(image, caption)
    if metadata is not None and not metadata.exists():
        metadata = None
    post_id, scheduled_due_at = publish_post(
        api_key,
        account_id,
        image,
        caption,
        metadata,
        due_at=due_at,
    )
    record_post(
        state,
        account_id,
        image,
        caption,
        metadata,
        post_id,
        due_at=scheduled_due_at,
    )
    print(
        f"Published {image.as_posix()} to Threads account {account_id} "
        f"as Zernio post {post_id}"
    )
    commit_state_to_git()
    refreshed = load_state()
    state.clear()
    state.update(refreshed)


def publish_one(
    api_key: str,
    account_ids: list[str],
    state: dict[str, Any],
    image: Path,
    caption: Path,
    metadata: Path | None,
    *,
    due_at: str | None = None,
) -> None:
    assert_daily_quota(state)
    relative = image.as_posix()
    existing = state.get("posted", {}).get(relative)
    pending_channels = missing_channel_ids(existing, account_ids)
    if not pending_channels:
        print(f"Already fully posted on all Threads channels: {relative}")
        return

    # Account ids are primary-first; secondary profiles get a later scheduledFor.
    primary_id = account_ids[0] if account_ids else ""
    for index, account_id in enumerate(pending_channels):
        channel_due_at = due_at
        if (
            due_at
            and primary_id
            and account_id != primary_id
            and SECONDARY_DELAY_SECONDS > 0
        ):
            channel_due_at = shift_due_at(due_at, SECONDARY_DELAY_SECONDS)
            print(
                f"Secondary account {account_id}: scheduledFor delayed "
                f"+{SECONDARY_DELAY_SECONDS}s after primary {primary_id}"
            )
        publish_one_channel(
            api_key,
            account_id,
            state,
            image,
            caption,
            metadata,
            due_at=channel_due_at,
        )
        if index + 1 < len(pending_channels) and API_GAP_SECONDS:
            time.sleep(API_GAP_SECONDS)


def publish_batch(api_key: str, account_ids: list[str]) -> int:
    """Enqueue today's pending posts before local midnight.

    Normal mode: FIFO customScheduled dueAt evenly spaced within ~1 hour.
    Flush mode (late day / tight window): shareNow so leftovers are not missed.
    """
    global SCHEDULE_WINDOW_SECONDS
    flush_mode = PUBLISH_MODE == "flush"
    SCHEDULE_WINDOW_SECONDS = schedule_window_seconds_until_midnight(
        preferred=PREFERRED_SCHEDULE_WINDOW_SECONDS,
        min_seconds=max(120, SECONDARY_DELAY_SECONDS + 60),
        reserve_seconds=MIDNIGHT_RESERVE_SECONDS,
    )
    until_midnight = seconds_until_local_midnight()
    if until_midnight <= 0:
        leftover = len(list_unpublished(load_state()))
        print(
            "Local midnight already passed; refusing to post leftovers into the "
            f"next day. Leaving {leftover} item(s)."
        )
        return 0

    # If scheduled window collapsed, auto-escalate to shareNow flush.
    if not flush_mode and SCHEDULE_WINDOW_SECONDS <= 0:
        flush_mode = True

    published = 0
    skipped_missing = 0
    target = MAX_POSTS
    due_ats: list[str | None]
    if flush_mode:
        due_ats = [None] * target
        channels_label = ", ".join(account_ids)
        print(
            f"Flushing up to {target} Threads post(s) from today only "
            f"({today_folder_name()}) with shareNow before midnight "
            f"({until_midnight}s left); account(s) [{channels_label}]."
        )
    else:
        due_ats = [
            clamp_due_at_before_local_midnight(
                due, reserve_seconds=MIDNIGHT_RESERVE_SECONDS
            )
            for due in allocate_due_ats(target)
        ]
        channels_label = ", ".join(account_ids)
        print(
            f"Scheduling up to {target} Threads post(s) from today only "
            f"({today_folder_name()}); FIFO primary-first account(s) "
            f"[{channels_label}] evenly within {SCHEDULE_WINDOW_SECONDS}s "
            f"(secondary +{SECONDARY_DELAY_SECONDS}s, never past local midnight) "
            "via Zernio scheduling."
        )

    while published < target:
        if seconds_until_local_midnight() <= 0:
            leftover = len(list_unpublished(load_state()))
            print(
                f"Hit local midnight after {published} post(s); stopping so "
                f"nothing spills into tomorrow. Leaving {leftover} item(s)."
            )
            break

        state = load_state()
        pending = list_unpublished(state)
        if not pending:
            if published == 0:
                print("No unpublished generated posts found for Threads.")
            break

        image, caption, metadata = pending[0]
        if flush_mode:
            due_at = None
        elif published < len(due_ats):
            due_at = due_ats[published]
        else:
            due_at = clamp_due_at_before_local_midnight(
                allocate_due_ats(1)[0],
                reserve_seconds=MIDNIGHT_RESERVE_SECONDS,
            )
        try:
            publish_one(
                api_key,
                account_ids,
                state,
                image,
                caption,
                metadata,
                due_at=due_at,
            )
        except MissingPostFiles as exc:
            skipped_missing += 1
            print(f"Skipping vanished queue item: {exc}", file=sys.stderr)
            continue
        except FileNotFoundError as exc:
            skipped_missing += 1
            print(f"Skipping vanished queue item: {exc}", file=sys.stderr)
            continue
        except DailyLimitReached as exc:
            leftover = len(list_unpublished(load_state()))
            print(
                f"Threads daily limit reached after {published} post(s): {exc}. "
                f"Leaving {leftover} queued."
            )
            return published
        except RateLimitReached as exc:
            leftover = len(list_unpublished(load_state()))
            print(
                f"Zernio rate limit hit after {published} post(s): {exc}. "
                f"Leaving {leftover} queued for later automatic runs."
            )
            return published
        published += 1
        if published < target and API_GAP_SECONDS:
            time.sleep(API_GAP_SECONDS)

    if skipped_missing:
        print(f"Skipped {skipped_missing} vanished queue item(s) this run.")
    return published


def drain_queue(api_key: str, account_ids: list[str]) -> int:
    return publish_batch(api_key, account_ids)


def main() -> int:
    api_key = required_env("ZERNIO_API_KEY")
    account_ids = parse_channel_ids(required_env("ZERNIO_THREADS_ACCOUNT_IDS"))
    if not account_ids:
        raise RuntimeError("ZERNIO_THREADS_ACCOUNT_IDS has no account ids")

    if PUBLISH_MODE == "drain" and DRAIN_WITHIN_SECONDS > 0:
        published = drain_queue(api_key, account_ids)
    else:
        published = publish_batch(api_key, account_ids)

    print(f"Finished Threads publish run. Posted {published} item(s).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DailyLimitReached as exc:
        print(
            f"Threads daily limit reached: {exc}. "
            "Queued posts will retry on later automatic runs.",
            file=sys.stderr,
        )
        raise SystemExit(0)
    except RateLimitReached as exc:
        print(
            f"Zernio rate limit reached: {exc}. "
            "Queued posts will retry after the API window resets.",
            file=sys.stderr,
        )
        raise SystemExit(0)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
