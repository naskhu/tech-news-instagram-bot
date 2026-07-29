#!/usr/bin/env python3
"""Publish generated Tech News posts to Instagram through Buffer's GraphQL API."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from publish_limits import (
    DAILY_LIMIT,
    before_posting_window,
    cooldown_active,
    count_posted_today,
    quota_left_today,
    seconds_until_local_midnight,
    today_folder_name,
    today_local,
)

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
STATE_FILE = Path(os.getenv("INSTAGRAM_STATE_FILE", "instagram-posted.json"))
REPOSITORY = os.getenv("GITHUB_REPOSITORY", "naskhu/tech-news-instagram-bot")
BRANCH = os.getenv("GITHUB_REF_NAME", "main") or "main"
MAX_POSTS = max(1, int(os.getenv("MAX_POSTS_PER_RUN", "1")))
PUBLISH_MODE = os.getenv("PUBLISH_MODE", "batch").strip().lower() or "batch"
DRAIN_WITHIN_SECONDS = max(0, int(os.getenv("DRAIN_WITHIN_SECONDS", "0")))
COMMIT_STATE_EACH_POST = os.getenv("COMMIT_STATE_EACH_POST", "").strip() == "1"
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


class RateLimitReached(RuntimeError):
    """Buffer API rate limit was hit; retry after the window resets."""


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
    today = today_folder_name()
    day_dir = OUTPUT_DIR / today

    # Only today's generated folder; never publish previous-day leftovers.
    images = sorted(day_dir.glob("*.png")) if day_dir.is_dir() else []
    for image in images:
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


def assert_daily_quota(state: dict[str, Any]) -> None:
    paused, pause_reason = cooldown_active()
    if paused:
        raise DailyLimitReached(pause_reason)

    too_early, early_reason = before_posting_window()
    if too_early:
        raise DailyLimitReached(early_reason)

    used = count_posted_today(state)
    left = quota_left_today(state)
    if left <= 0:
        raise DailyLimitReached(
            f"Calendar-day Instagram cap reached ({used}/{DAILY_LIMIT} on "
            f"{today_local().isoformat()}). Resume after local midnight."
        )
    print(f"Calendar-day Buffer usage: {used}/{DAILY_LIMIT} ({left} left today)")


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


def is_rate_limit_error(message: object) -> bool:
    text = str(message or "").lower()
    return (
        "rate_limit_exceeded" in text
        or "too many requests" in text
        or '"code": "rate_limit' in text
        or "http 429" in text
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
        if response.status_code == 429 or is_rate_limit_error(body):
            raise RateLimitReached(body)
        raise RuntimeError(f"Buffer API HTTP {response.status_code}: {body}")
    if payload.get("errors"):
        body = json.dumps(payload["errors"])
        if is_daily_limit_error(body):
            raise DailyLimitReached(body)
        if is_rate_limit_error(body):
            raise RateLimitReached(body)
        raise RuntimeError(f"Buffer GraphQL errors: {body}")
    return payload


def sync_state_from_origin() -> dict[str, Any]:
    """Refresh instagram-posted.json from origin/main before choosing the next item.

    Prevents a long drain (or a failed mid-run push) from republishing an image
    that another Actions tick already sent via Buffer shareNow.
    """
    if not COMMIT_STATE_EACH_POST:
        return load_state()

    subprocess.run(["git", "fetch", "origin", "main"], check=False)
    # Keep local uncommitted state if present; otherwise take remote file.
    if STATE_FILE.exists():
        local = load_state()
    else:
        local = {"posted": {}}

    show = subprocess.run(
        ["git", "show", f"origin/main:{STATE_FILE.as_posix()}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if show.returncode != 0 or not show.stdout.strip():
        return local

    try:
        remote = json.loads(show.stdout)
    except json.JSONDecodeError:
        return local
    if not isinstance(remote, dict):
        return local
    remote.setdefault("posted", {})
    if not isinstance(remote["posted"], dict):
        remote["posted"] = {}

    # Merge: remote wins on conflicts (already on main), then keep any newer local keys.
    merged_posted = dict(remote["posted"])
    for key, entry in local.get("posted", {}).items():
        if key not in merged_posted:
            merged_posted[key] = entry
    local["posted"] = merged_posted
    save_state(local)
    return local


def is_successful_buffer_status(status: object) -> bool:
    text = str(status or "").strip().lower()
    if not text:
        # Legacy records with no status field count as successful publishes.
        return True
    return text not in {"error", "failed", "rejected"}


def publish_post(
    access_token: str,
    channel_id: str,
    image: Path,
    caption_file: Path,
) -> tuple[str, str]:
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
        if is_rate_limit_error(message):
            raise RateLimitReached(str(message))
        raise RuntimeError(f"Buffer rejected post: {message}")
    if typename != "PostActionSuccess" or not result.get("post", {}).get("id"):
        raise RuntimeError(f"Unexpected Buffer createPost response: {json.dumps(result)}")

    post_id = str(result["post"]["id"])
    status = str(result["post"].get("status") or "").strip().lower() or "unknown"
    print(f"Buffer post created: id={post_id} status={status}")
    return post_id, status


def record_post(
    state: dict[str, Any],
    channel_id: str,
    image: Path,
    caption: Path,
    metadata: Path | None,
    post_id: str,
    *,
    buffer_status: str = "sent",
) -> None:
    entry: dict[str, Any] = {
        "buffer_post_id": post_id,
        "buffer_status": buffer_status,
        "channel_id": channel_id,
        "publisher": "buffer",
        "caption_file": caption.as_posix(),
        "metadata_file": metadata.as_posix() if metadata else None,
        "image_url": public_image_url(image),
        "published_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if not is_successful_buffer_status(buffer_status):
        # Still mark as handled so drain/schedule ticks do not create a second IG post.
        entry["publish_result"] = "buffer_error_not_retried"
    state["posted"][image.as_posix()] = entry
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
    for attempt in range(1, 5):
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
    raise RuntimeError(
        "Could not push Instagram publishing state after Buffer create. "
        "Stopping this run to avoid double-posting the same image."
    )


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
) -> str:
    assert_daily_quota(state)
    post_id, buffer_status = publish_post(access_token, channel_id, image, caption)
    record_post(
        state,
        channel_id,
        image,
        caption,
        metadata,
        post_id,
        buffer_status=buffer_status,
    )
    if is_successful_buffer_status(buffer_status):
        print(f"Published {image.as_posix()} as Buffer post {post_id}")
    else:
        print(
            f"Buffer returned status={buffer_status} for {image.as_posix()} "
            f"(post {post_id}); marking handled so it will not be resent.",
            file=sys.stderr,
        )
    commit_state_to_git()
    refreshed = load_state()
    state.clear()
    state.update(refreshed)
    return buffer_status


def drain_queue(access_token: str, channel_id: str) -> int:
    deadline = time.time() + DRAIN_WITHIN_SECONDS
    target = MAX_POSTS
    initial_delay = random.randint(0, min(180, max(0, DRAIN_WITHIN_SECONDS // 10)))
    print(
        f"Drain mode: publish up to {target} post(s) randomly within {DRAIN_WITHIN_SECONDS}s "
        f"(initial delay {initial_delay}s, calendar-day cap {DAILY_LIMIT})"
    )
    if initial_delay:
        time.sleep(initial_delay)

    published = 0
    while published < target and time.time() < deadline:
        state = sync_state_from_origin()
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
            status = publish_one(access_token, channel_id, state, image, caption, metadata)
        except DailyLimitReached as exc:
            leftover = len(list_unpublished(load_state()))
            print(
                f"Instagram daily limit reached after {published} post(s) this run: {exc}. "
                f"Leaving {leftover} queued for later automatic runs."
            )
            return published
        except RateLimitReached as exc:
            leftover = len(list_unpublished(load_state()))
            print(
                f"Buffer rate limit hit after {published} post(s): {exc}. "
                f"Leaving {leftover} queued for later automatic runs."
            )
            return published

        if is_successful_buffer_status(status):
            published += 1
        remaining_this_run = target - published
        remaining_queue = len(list_unpublished(load_state()))
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
            "later automatic runs continue under the calendar-day cap."
        )
    return published


def publish_batch(access_token: str, channel_id: str) -> int:
    state = sync_state_from_origin()
    posts = discover_posts(state)
    if not posts:
        print("No unpublished generated posts found.")
        return 0

    published = 0
    for image, caption, metadata in posts:
        state = sync_state_from_origin()
        if image.as_posix() in state.get("posted", {}):
            print(f"Skipping {image.as_posix()}: already recorded after state sync")
            continue
        try:
            status = publish_one(access_token, channel_id, state, image, caption, metadata)
        except DailyLimitReached as exc:
            leftover = len(list_unpublished(load_state()))
            print(
                f"Instagram daily limit reached after {published} post(s) this run: {exc}. "
                f"Leaving {leftover} queued for later automatic runs."
            )
            return published
        except RateLimitReached as exc:
            leftover = len(list_unpublished(load_state()))
            print(
                f"Buffer rate limit hit after {published} post(s): {exc}. "
                f"Leaving {leftover} queued for later automatic runs."
            )
            return published
        if is_successful_buffer_status(status):
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
    except RateLimitReached as exc:
        print(
            f"Buffer rate limit reached: {exc}. "
            "Queued posts will retry after the API window resets.",
            file=sys.stderr,
        )
        raise SystemExit(0)
    except Exception as exc:  # Keep Actions logs concise and actionable.
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
