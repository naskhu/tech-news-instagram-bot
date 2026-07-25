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
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from threads_limits import DAILY_LIMIT, count_posted_last_24h, quota_left_24h

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

CREATE_POST_MUTATION = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess {
      post {
        id
        status
        text
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
    candidates: list[tuple[Path, Path, Path | None]] = []

    for image in sorted(OUTPUT_DIR.glob("**/*.png")):
        relative = image.as_posix()
        if relative in posted:
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

    raise RuntimeError(
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
    """Fit Instagram caption content into Threads' 500-char / one-topic rules."""
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

    parts: list[str] = []
    if title:
        parts.append(title)
    if summary and summary.lower() not in title.lower():
        parts.append(summary)
    if source:
        parts.append(f"Source: {source}")
    if url:
        parts.append(url)

    body = "\n\n".join(parts).strip() or raw
    # Reserve room for a single Threads topic line.
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


def publish_post(
    access_token: str,
    channel_id: str,
    image: Path,
    caption_file: Path,
    metadata: Path | None,
) -> str:
    ensure_post_files(image, caption_file)
    if metadata is not None and not metadata.exists():
        metadata = None
    caption = build_threads_caption(caption_file, metadata)
    if not caption:
        raise RuntimeError(f"Caption is empty after Threads formatting: {caption_file}")

    image_url = public_image_url(image)
    wait_for_public_image(image_url)

    print(f"Creating Buffer Threads post for {image.as_posix()}")
    payload = buffer_graphql(
        access_token,
        CREATE_POST_MUTATION,
        {
            "input": {
                "text": caption,
                "channelId": channel_id,
                "schedulingType": "automatic",
                "mode": "shareNow",
                "assets": [{"image": {"url": image_url}}],
                "metadata": {
                    "threads": {
                        "type": "post",
                        "topic": THREADS_TOPIC.lstrip("#"),
                    }
                },
            }
        },
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
    return post_id


def record_post(
    state: dict[str, Any],
    channel_id: str,
    image: Path,
    caption: Path,
    metadata: Path | None,
    post_id: str,
) -> None:
    state["posted"][image.as_posix()] = {
        "buffer_post_id": post_id,
        "channel_id": channel_id,
        "publisher": "buffer_threads",
        "caption_file": caption.as_posix(),
        "metadata_file": metadata.as_posix() if metadata else None,
        "image_url": public_image_url(image),
        "published_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
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


def inter_post_delay_seconds(remaining_after: int, seconds_left: float) -> int:
    """Random gaps between Buffer sends — faster than before, still not a burst."""
    if remaining_after <= 0 or seconds_left <= 30:
        return 0
    average = max(60, int((seconds_left * 0.85) / remaining_after))
    low = max(45, int(average * 0.55))
    high = max(low + 1, min(int(average * 1.35), 180))
    return random.randint(low, high)


def publish_one(
    access_token: str,
    channel_id: str,
    state: dict[str, Any],
    image: Path,
    caption: Path,
    metadata: Path | None,
) -> None:
    assert_daily_quota(state)
    ensure_post_files(image, caption)
    if metadata is not None and not metadata.exists():
        metadata = None
    post_id = publish_post(access_token, channel_id, image, caption, metadata)
    # Rebase during commit may refresh the working tree; reload state afterward.
    record_post(state, channel_id, image, caption, metadata, post_id)
    print(f"Published {image.as_posix()} as Buffer Threads post {post_id}")
    commit_state_to_git()
    # Keep in-memory state aligned after possible rebase of threads-posted.json.
    refreshed = load_state()
    state.clear()
    state.update(refreshed)


def drain_queue(access_token: str, channel_id: str) -> int:
    deadline = time.time() + DRAIN_WITHIN_SECONDS
    target = MAX_POSTS
    initial_delay = random.randint(0, min(60, max(0, DRAIN_WITHIN_SECONDS // 20)))
    print(
        f"Threads drain: up to {target} post(s) within {DRAIN_WITHIN_SECONDS}s "
        f"(initial delay {initial_delay}s, rolling cap {DAILY_LIMIT}/24h)"
    )
    if initial_delay:
        time.sleep(initial_delay)

    published = 0
    skipped_missing = 0
    while published < target and time.time() < deadline:
        state = load_state()
        try:
            assert_daily_quota(state)
        except DailyLimitReached as exc:
            leftover = len(list_unpublished(state))
            print(
                f"Threads daily cap reached after {published} post(s) this run: {exc}. "
                f"Leaving {leftover} queued."
            )
            return published

        pending = list_unpublished(state)
        if not pending:
            print("Threads queue empty; drain complete.")
            break

        image, caption, metadata = pending[0]
        try:
            publish_one(access_token, channel_id, state, image, caption, metadata)
        except MissingPostFiles as exc:
            skipped_missing += 1
            print(f"Skipping vanished queue item: {exc}", file=sys.stderr)
            # Avoid tight-looping on the same missing path if glob somehow still sees it.
            time.sleep(1)
            continue
        except DailyLimitReached as exc:
            leftover = len(list_unpublished(load_state()))
            print(
                f"Threads daily limit reached after {published} post(s): {exc}. "
                f"Leaving {leftover} queued."
            )
            return published
        except FileNotFoundError as exc:
            skipped_missing += 1
            print(f"Skipping vanished queue item: {exc}", file=sys.stderr)
            continue

        published += 1
        remaining_this_run = target - published
        remaining_queue = len(list_unpublished(load_state()))
        if remaining_this_run <= 0 or time.time() >= deadline:
            print(
                f"Finished Threads tick ({published} posted). "
                f"Queue remaining: {max(0, remaining_queue)}."
            )
            break

        seconds_left = deadline - time.time()
        delay = inter_post_delay_seconds(remaining_this_run, seconds_left)
        print(
            f"Remaining this tick: {remaining_this_run}. "
            f"Sleeping {delay}s before next Threads publish."
        )
        if delay > 0:
            time.sleep(delay)

    leftover = len(list_unpublished(load_state()))
    if skipped_missing:
        print(f"Skipped {skipped_missing} vanished queue item(s) this run.")
    if leftover:
        print(
            f"Threads tick complete with {leftover} post(s) still queued; "
            "later automatic runs continue under the 240/24h soft cap."
        )
    return published


def publish_batch(access_token: str, channel_id: str) -> int:
    """Publish up to MAX_POSTS, re-scanning the queue each time (files can vanish mid-run)."""
    published = 0
    skipped_missing = 0
    target = MAX_POSTS

    while published < target:
        state = load_state()
        pending = list_unpublished(state)
        if not pending:
            if published == 0:
                print("No unpublished generated posts found for Threads.")
            break

        image, caption, metadata = pending[0]
        try:
            publish_one(access_token, channel_id, state, image, caption, metadata)
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

    if skipped_missing:
        print(f"Skipped {skipped_missing} vanished queue item(s) this run.")
    return published


def main() -> int:
    access_token = required_env("BUFFER_ACCESS_TOKEN")
    channel_id = required_env("BUFFER_THREADS_CHANNEL_ID")

    if PUBLISH_MODE == "drain" and DRAIN_WITHIN_SECONDS > 0:
        published = drain_queue(access_token, channel_id)
    else:
        published = publish_batch(access_token, channel_id)

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
