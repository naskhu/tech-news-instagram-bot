#!/usr/bin/env python3
"""Publish generated Tech News posts to Instagram through Buffer's GraphQL API."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
STATE_FILE = Path(os.getenv("INSTAGRAM_STATE_FILE", "instagram-posted.json"))
REPOSITORY = os.getenv("GITHUB_REPOSITORY", "naskhu/tech-news-instagram-bot")
BRANCH = os.getenv("GITHUB_REF_NAME", "main") or "main"
MAX_POSTS = max(1, int(os.getenv("MAX_POSTS_PER_RUN", "1")))
PUBLISH_MODE = os.getenv("PUBLISH_MODE", "batch").strip().lower() or "batch"
DRAIN_WITHIN_SECONDS = max(0, int(os.getenv("DRAIN_WITHIN_SECONDS", "0")))
COMMIT_STATE_EACH_POST = os.getenv("COMMIT_STATE_EACH_POST", "").strip() == "1"
# Buffer's documented Instagram posts/reels/stories limit per rolling 24 hours.
DAILY_LIMIT = max(1, int(os.getenv("INSTAGRAM_DAILY_LIMIT", "50")))
BUFFER_API_URL = os.getenv("BUFFER_API_URL", "https://api.buffer.com")

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
    """Instagram/Buffer daily scheduling limit was hit; retry later."""


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
        if not caption.exists():
            print(f"Skipping {relative}: matching caption file is missing", file=sys.stderr)
            continue

        candidates.append((image, caption, metadata if metadata.exists() else None))

    return candidates


def discover_posts(state: dict[str, Any]) -> list[tuple[Path, Path, Path | None]]:
    return list_unpublished(state)[:MAX_POSTS]


def parse_published_at(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def count_posted_last_24h(state: dict[str, Any]) -> int:
    cutoff = time.time() - 24 * 60 * 60
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
        ts = parse_published_at(entry.get("published_at_utc"))
        if ts is not None and ts >= cutoff:
            count += 1
    return count


def assert_daily_quota(state: dict[str, Any]) -> None:
    used = count_posted_last_24h(state)
    if used >= DAILY_LIMIT:
        raise DailyLimitReached(
            f"Rolling 24h Instagram cap reached ({used}/{DAILY_LIMIT}). "
            "Resume after older posts age out of the window."
        )
    print(f"Rolling 24h Buffer usage: {used}/{DAILY_LIMIT}")


def public_image_url(image: Path) -> str:
    """Build the public raw.githubusercontent.com URL Buffer will download from git."""
    encoded_path = "/".join(quote(part) for part in image.as_posix().split("/"))
    return f"https://raw.githubusercontent.com/{REPOSITORY}/{quote(BRANCH)}/{encoded_path}"


def wait_for_public_image(image_url: str, attempts: int = 12, delay_seconds: float = 5.0) -> None:
    """Wait until the committed image is publicly reachable for Buffer."""
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

        print(
            f"Waiting for git-hosted image (attempt {attempt}/{attempts}): {last_error}"
        )
        time.sleep(delay_seconds)

    raise RuntimeError(
        f"Image is not publicly available from git yet: {image_url} ({last_error})"
    )


def is_daily_limit_error(message: object) -> bool:
    text = str(message or "").lower()
    return (
        "maximum number of posts" in text
        or ("instagram allows in a day" in text)
        or ("daily" in text and "limit" in text and "instagram" in text)
    )


def buffer_graphql(access_token: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
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
) -> str:
    caption = caption_file.read_text(encoding="utf-8").strip()
    if not caption:
        raise RuntimeError(f"Caption is empty: {caption_file}")

    image_url = public_image_url(image)
    wait_for_public_image(image_url)

    print(f"Creating Buffer Instagram post for {image.as_posix()}")
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
                    "instagram": {
                        "type": "post",
                        "shouldShareToFeed": True,
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
        raise RuntimeError(f"Buffer rejected post: {message}")
    if typename != "PostActionSuccess" or not result.get("post", {}).get("id"):
        raise RuntimeError(f"Unexpected Buffer createPost response: {json.dumps(result)}")

    post_id = str(result["post"]["id"])
    status = str(result["post"].get("status") or "").strip().lower()
    print(f"Buffer post created: id={post_id} status={status or 'unknown'}")
    if status in {"error", "failed", "rejected"}:
        raise RuntimeError(
            f"Buffer created post {post_id} but status={status}. "
            "Check Buffer → Instagram channel (reconnect Instagram / plan limits)."
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
        "publisher": "buffer",
        "caption_file": caption.as_posix(),
        "metadata_file": metadata.as_posix() if metadata else None,
        "image_url": public_image_url(image),
        "published_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    save_state(state)


def commit_state_to_git() -> None:
    """Persist publish progress after each post during long drain runs."""
    if not COMMIT_STATE_EACH_POST:
        return

    subprocess.run(
        ["git", "config", "user.name", "github-actions[bot]"],
        check=False,
    )
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
        ["git", "commit", "-m", "Record published Instagram post"],
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
            print("Publishing state pushed to git.")
            return
        time.sleep(attempt * 3)
    print("WARNING: could not push publishing state after this post", file=sys.stderr)


def inter_post_delay_seconds(remaining_after: int, seconds_left: float) -> int:
    """Spread remaining posts across the leftover time window with jitter."""
    if remaining_after <= 0 or seconds_left <= 30:
        return 0
    average = max(45, int((seconds_left * 0.9) / remaining_after))
    low = max(30, int(average * 0.45))
    high = max(low + 1, min(int(average * 1.35), 600))
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
    post_id = publish_post(access_token, channel_id, image, caption)
    record_post(state, channel_id, image, caption, metadata, post_id)
    print(f"Published {image.as_posix()} as Buffer post {post_id}")
    commit_state_to_git()


def drain_queue(access_token: str, channel_id: str) -> int:
    deadline = time.time() + DRAIN_WITHIN_SECONDS
    target = MAX_POSTS
    initial_delay = random.randint(0, min(180, max(0, DRAIN_WITHIN_SECONDS // 10)))
    print(
        f"Drain mode: publish up to {target} post(s) randomly within {DRAIN_WITHIN_SECONDS}s "
        f"(initial delay {initial_delay}s, daily cap {DAILY_LIMIT}/24h)"
    )
    if initial_delay:
        time.sleep(initial_delay)

    published = 0
    while published < target and time.time() < deadline:
        state = load_state()
        try:
            assert_daily_quota(state)
        except DailyLimitReached as exc:
            leftover = len(list_unpublished(state))
            print(
                f"Daily cap reached after {published} post(s) this run: {exc}. "
                f"Leaving {leftover} queued for the next day."
            )
            return published

        pending = list_unpublished(state)
        if not pending:
            print("Queue empty; drain complete.")
            break

        image, caption, metadata = pending[0]
        try:
            publish_one(access_token, channel_id, state, image, caption, metadata)
        except DailyLimitReached as exc:
            leftover = len(list_unpublished(load_state()))
            print(
                f"Instagram daily limit reached after {published} post(s) this run: {exc}. "
                f"Leaving {leftover} queued for later automatic runs."
            )
            return published

        published += 1
        remaining_this_run = target - published
        remaining_queue = len(pending) - 1
        if remaining_this_run <= 0 or remaining_queue <= 0:
            print(
                f"Finished this tick ({published} posted). "
                f"Queue remaining: {max(0, remaining_queue)}."
            )
            break

        seconds_left = deadline - time.time()
        delay = inter_post_delay_seconds(remaining_this_run, seconds_left)
        print(
            f"Remaining this tick: {remaining_this_run}. "
            f"Sleeping {delay}s before next random publish."
        )
        if delay > 0:
            time.sleep(delay)

    leftover = len(list_unpublished(load_state()))
    if leftover:
        print(
            f"Tick complete with {leftover} post(s) still queued; "
            "later automatic runs continue under the 50/24h cap."
        )
    return published


def publish_batch(access_token: str, channel_id: str) -> int:
    state = load_state()
    posts = discover_posts(state)
    if not posts:
        print("No unpublished generated posts found.")
        return 0

    published = 0
    for image, caption, metadata in posts:
        try:
            publish_one(access_token, channel_id, state, image, caption, metadata)
        except DailyLimitReached as exc:
            leftover = len(list_unpublished(load_state()))
            print(
                f"Instagram daily limit reached after {published} post(s) this run: {exc}. "
                f"Leaving {leftover} queued for later automatic runs."
            )
            return published
        published += 1
    return published


def main() -> int:
    access_token = required_env("BUFFER_ACCESS_TOKEN")
    channel_id = required_env("BUFFER_CHANNEL_ID")

    if PUBLISH_MODE == "drain" and DRAIN_WITHIN_SECONDS > 0:
        published = drain_queue(access_token, channel_id)
    else:
        published = publish_batch(access_token, channel_id)

    print(f"Finished publish run. Posted {published} item(s).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DailyLimitReached as exc:
        # Safety net if raised outside drain/batch handlers.
        print(
            f"Instagram daily limit reached: {exc}. "
            "Queued posts will retry on later automatic runs.",
            file=sys.stderr,
        )
        raise SystemExit(0)
    except Exception as exc:  # Keep Actions logs concise and actionable.
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
