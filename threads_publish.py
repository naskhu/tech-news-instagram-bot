#!/usr/bin/env python3
"""Publish generated Tech News posts to Threads through Buffer's GraphQL API.

Uses the same public git-hosted images as Instagram publishing. Tracks progress
in threads-posted.json so Instagram and Threads queues stay independent.
"""

from __future__ import annotations

import json
import os
import random
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

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
STATE_FILE = Path(os.getenv("THREADS_STATE_FILE", "threads-posted.json"))
REPOSITORY = os.getenv("GITHUB_REPOSITORY", "naskhu/tech-news-instagram-bot")
BRANCH = os.getenv("GITHUB_REF_NAME", "main") or "main"
MAX_POSTS = max(1, int(os.getenv("MAX_POSTS_PER_RUN", "1")))
PUBLISH_MODE = os.getenv("PUBLISH_MODE", "batch").strip().lower() or "batch"
DRAIN_WITHIN_SECONDS = max(0, int(os.getenv("DRAIN_WITHIN_SECONDS", "0")))
COMMIT_STATE_EACH_POST = os.getenv("COMMIT_STATE_EACH_POST", "").strip() == "1"
BUFFER_API_URL = os.getenv("BUFFER_API_URL", "https://api.buffer.com")
THREADS_TOPIC = os.getenv("THREADS_TOPIC", "TechNews").strip() or "TechNews"
# Threads hard limit is 500 characters per message.
THREADS_MAX_CHARS = max(80, min(500, int(os.getenv("THREADS_MAX_CHARS", "500"))))
# Spread Buffer customScheduled times randomly across this window (default 20 minutes).
SCHEDULE_WINDOW_SECONDS = max(120, min(1800, int(os.getenv("THREADS_SCHEDULE_WINDOW_SECONDS", "1200"))))
SCHEDULE_MIN_OFFSET_SECONDS = max(20, int(os.getenv("THREADS_SCHEDULE_MIN_OFFSET_SECONDS", "45")))
# Delay secondary profiles (e.g. naskhu) after primary (news.world.tech) dueAt.
SECONDARY_DELAY_SECONDS = max(0, min(600, int(os.getenv("THREADS_SECONDARY_DELAY_SECONDS", "120"))))
# Small pause between Buffer API create calls (scheduling is handled by dueAt).
API_GAP_SECONDS = max(1, int(os.getenv("THREADS_API_GAP_SECONDS", "3")))

CREATE_POST_MUTATION = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess {
      post {
        id
        status
        text
        dueAt
      }
    }
    ... on MutationError {
      message
    }
  }
}
"""


class DailyLimitReached(RuntimeError):
    """Threads/Buffer daily scheduling limit was hit; retry later."""


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


def list_unpublished(state: dict[str, Any]) -> list[tuple[Path, Path, Path | None]]:
    posted = state.get("posted", {})
    needed = parse_channel_ids()
    candidates: list[tuple[Path, Path, Path | None]] = []

    for image in sorted(OUTPUT_DIR.glob("**/*.png")):
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
    print(f"Threads Buffer usage (24h): {used}/{DAILY_LIMIT} ({left} left)")


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


def is_daily_limit_error(message: object) -> bool:
    text = str(message or "").lower()
    return (
        "maximum number of posts" in text
        or ("threads" in text and "limit" in text and "day" in text)
        or ("daily" in text and "limit" in text)
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


def buffer_graphql(
    access_token: str,
    query: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = requests.post(
        BUFFER_API_URL,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        json={"query": query, "variables": variables or {}},
        timeout=90,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Buffer API returned non-JSON HTTP {response.status_code}: {response.text[:300]}"
        ) from exc

    if not response.ok:
        body = json.dumps(payload)
        if is_daily_limit_error(body):
            raise DailyLimitReached(body)
        raise RuntimeError(f"Buffer API HTTP {response.status_code}: {body}")
    if payload.get("errors"):
        body = json.dumps(payload["errors"])
        if is_daily_limit_error(body):
            raise DailyLimitReached(body)
        raise RuntimeError(f"Buffer GraphQL errors: {body}")
    return payload


def allocate_due_ats(count: int) -> list[str]:
    """Pick unique random UTC dueAt times inside the next schedule window."""
    if count <= 0:
        return []
    now = datetime.now(timezone.utc)
    lo = SCHEDULE_MIN_OFFSET_SECONDS
    # Leave room for secondary-channel delay after the primary dueAt.
    usable_window = max(lo + 1, SCHEDULE_WINDOW_SECONDS - SECONDARY_DELAY_SECONDS - 30)
    hi = max(lo + 1, usable_window)
    span = hi - lo + 1
    if span >= count:
        offsets = sorted(random.sample(range(lo, hi + 1), count))
    else:
        offsets = sorted(random.randint(lo, hi) for _ in range(count))
    return [
        (now + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        for offset in offsets
    ]


def shift_due_at(due_at: str | None, delay_seconds: int) -> str | None:
    """Return due_at shifted forward by delay_seconds (UTC ISO)."""
    if not due_at or delay_seconds <= 0:
        return due_at
    text = due_at.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return due_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(seconds=delay_seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def publish_post(
    access_token: str,
    channel_id: str,
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

    post_input: dict[str, Any] = {
        "text": caption,
        "channelId": channel_id,
        "schedulingType": "automatic",
        "assets": [{"image": {"url": image_url}}],
        "metadata": {
            "threads": {
                "type": "post",
                "topic": THREADS_TOPIC.lstrip("#"),
            }
        },
    }
    if due_at:
        post_input["mode"] = "customScheduled"
        post_input["dueAt"] = due_at
        print(f"Creating Buffer Threads post for {image.as_posix()} dueAt={due_at}")
    else:
        post_input["mode"] = "shareNow"
        print(f"Creating Buffer Threads post for {image.as_posix()} (shareNow)")

    payload = buffer_graphql(
        access_token,
        CREATE_POST_MUTATION,
        {"input": post_input},
    )

    result = (payload.get("data") or {}).get("createPost") or {}
    typename = result.get("__typename")
    if typename == "MutationError" or result.get("message"):
        message = result.get("message") or result
        if is_daily_limit_error(message):
            raise DailyLimitReached(str(message))
        raise RuntimeError(f"Buffer rejected Threads post: {message}")
    if typename != "PostActionSuccess" or not result.get("post", {}).get("id"):
        raise RuntimeError(f"Unexpected Buffer createPost response: {json.dumps(result)}")

    post_id = str(result["post"]["id"])
    status = str(result["post"].get("status") or "").strip().lower()
    print(f"Buffer Threads post created: id={post_id} status={status or 'unknown'}")
    if status in {"error", "failed", "rejected"}:
        raise RuntimeError(
            f"Buffer created Threads post {post_id} but status={status}. "
            "Check Buffer → Threads channel (connect Threads / plan limits)."
        )
    return post_id, due_at


def record_post(
    state: dict[str, Any],
    channel_id: str,
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
        "buffer_post_id": post_id,
        "published_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if due_at:
        channel_entry["buffer_due_at_utc"] = due_at
        channel_entry["buffer_mode"] = "customScheduled"
    else:
        channel_entry["buffer_mode"] = "shareNow"
    channels[channel_id] = channel_entry

    entry.update(
        {
            "channels": channels,
            "channel_id": channel_id,
            "buffer_post_id": post_id,
            "publisher": "buffer_threads",
            "caption_file": caption.as_posix(),
            "metadata_file": metadata.as_posix() if metadata else None,
            "image_url": public_image_url(image),
            "published_at_utc": channel_entry["published_at_utc"],
            "buffer_mode": channel_entry["buffer_mode"],
        }
    )
    if due_at:
        entry["buffer_due_at_utc"] = due_at
    elif "buffer_due_at_utc" in entry:
        entry.pop("buffer_due_at_utc", None)

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
    access_token: str,
    channel_id: str,
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
        access_token,
        channel_id,
        image,
        caption,
        metadata,
        due_at=due_at,
    )
    record_post(
        state,
        channel_id,
        image,
        caption,
        metadata,
        post_id,
        due_at=scheduled_due_at,
    )
    print(
        f"Published {image.as_posix()} to Threads channel {channel_id} "
        f"as Buffer post {post_id}"
    )
    commit_state_to_git()
    refreshed = load_state()
    state.clear()
    state.update(refreshed)


def publish_one(
    access_token: str,
    channel_ids: list[str],
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
    pending_channels = missing_channel_ids(existing, channel_ids)
    if not pending_channels:
        print(f"Already fully posted on all Threads channels: {relative}")
        return

    # channel_ids are primary-first; secondary profiles get a later dueAt.
    primary_id = channel_ids[0] if channel_ids else ""
    for index, channel_id in enumerate(pending_channels):
        channel_due_at = due_at
        if (
            due_at
            and primary_id
            and channel_id != primary_id
            and SECONDARY_DELAY_SECONDS > 0
        ):
            channel_due_at = shift_due_at(due_at, SECONDARY_DELAY_SECONDS)
            print(
                f"Secondary channel {channel_id}: dueAt delayed "
                f"+{SECONDARY_DELAY_SECONDS}s after primary {primary_id}"
            )
        publish_one_channel(
            access_token,
            channel_id,
            state,
            image,
            caption,
            metadata,
            due_at=channel_due_at,
        )
        if index + 1 < len(pending_channels) and API_GAP_SECONDS:
            time.sleep(API_GAP_SECONDS)


def publish_batch(access_token: str, channel_ids: list[str]) -> int:
    """Enqueue up to MAX_POSTS into Buffer with random dueAt times in the next window."""
    published = 0
    skipped_missing = 0
    target = MAX_POSTS
    due_ats = allocate_due_ats(target)
    channels_label = ", ".join(channel_ids)
    print(
        f"Scheduling up to {target} Threads post(s); primary-first channel(s) "
        f"[{channels_label}] randomly within {SCHEDULE_WINDOW_SECONDS}s "
        f"(secondary +{SECONDARY_DELAY_SECONDS}s) via Buffer customScheduled."
    )

    while published < target:
        state = load_state()
        pending = list_unpublished(state)
        if not pending:
            if published == 0:
                print("No unpublished generated posts found for Threads.")
            break

        image, caption, metadata = pending[0]
        due_at = due_ats[published] if published < len(due_ats) else allocate_due_ats(1)[0]
        try:
            publish_one(
                access_token,
                channel_ids,
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
        published += 1
        if published < target and API_GAP_SECONDS:
            time.sleep(API_GAP_SECONDS)

    if skipped_missing:
        print(f"Skipped {skipped_missing} vanished queue item(s) this run.")
    return published


def drain_queue(access_token: str, channel_ids: list[str]) -> int:
    # Prefer Buffer-side random scheduling; keep drain as a thin wrapper.
    return publish_batch(access_token, channel_ids)


def main() -> int:
    access_token = required_env("BUFFER_ACCESS_TOKEN")
    channel_ids = parse_channel_ids(required_env("BUFFER_THREADS_CHANNEL_ID"))
    if not channel_ids:
        raise RuntimeError("BUFFER_THREADS_CHANNEL_ID has no channel ids")

    if PUBLISH_MODE == "drain" and DRAIN_WITHIN_SECONDS > 0:
        published = drain_queue(access_token, channel_ids)
    else:
        published = publish_batch(access_token, channel_ids)

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
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
