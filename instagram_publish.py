#!/usr/bin/env python3
"""Publish generated Tech News posts to Instagram through Meta Graph API."""

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

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))
STATE_FILE = Path(os.getenv("INSTAGRAM_STATE_FILE", "instagram-posted.json"))
REPOSITORY = os.getenv("GITHUB_REPOSITORY", "naskhu/tech-news-instagram-bot")
BRANCH = os.getenv("GITHUB_REF_NAME", "main") or "main"
MAX_POSTS = max(1, int(os.getenv("MAX_POSTS_PER_RUN", "1")))
PUBLISH_MODE = os.getenv("PUBLISH_MODE", "batch").strip().lower() or "batch"
DRAIN_WITHIN_SECONDS = max(0, int(os.getenv("DRAIN_WITHIN_SECONDS", "0")))
COMMIT_STATE_EACH_POST = os.getenv("COMMIT_STATE_EACH_POST", "").strip() == "1"
GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION", "v23.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


class DailyLimitReached(RuntimeError):
    """Instagram API publishing limit was hit; retry later."""


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


def public_image_url(image: Path) -> str:
    """Build the public raw.githubusercontent.com URL Meta will download from git."""
    encoded_path = "/".join(quote(part) for part in image.as_posix().split("/"))
    return f"https://raw.githubusercontent.com/{REPOSITORY}/{quote(BRANCH)}/{encoded_path}"


def wait_for_public_image(image_url: str, attempts: int = 12, delay_seconds: float = 5.0) -> None:
    """Wait until the committed image is publicly reachable for Meta."""
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
        or "instagram allows in a day" in text
        or "rate limit" in text
        or "publish limit" in text
        or "daily" in text and "limit" in text
        or '"code": 4' in text
        or '"code": 17' in text
        or '"code": 32' in text
        or '"code": 613' in text
    )


def graph_request(
    method: str,
    endpoint: str,
    *,
    params: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    retries: int = 4,
) -> dict[str, Any]:
    last_error: Exception | None = None
    url = f"{GRAPH_BASE}/{endpoint.lstrip('/')}"

    for attempt in range(1, retries + 1):
        try:
            response = requests.request(
                method,
                url,
                params=params,
                data=data,
                timeout=90,
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"Meta API returned non-JSON HTTP {response.status_code}: "
                    f"{response.text[:300]}"
                ) from exc

            body = json.dumps(payload)
            if is_daily_limit_error(body):
                raise DailyLimitReached(body)
            if response.ok and "error" not in payload:
                return payload
            raise RuntimeError(f"Meta API HTTP {response.status_code}: {body}")
        except DailyLimitReached:
            raise
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt == retries:
                break
            delay = 10 * attempt
            print(f"Meta API attempt {attempt} failed; retrying in {delay}s: {exc}")
            time.sleep(delay)

    raise RuntimeError(f"Meta API request failed: {last_error}")


def wait_for_container(access_token: str, creation_id: str) -> None:
    """Poll container status until FINISHED (or fail)."""
    for attempt in range(1, 21):
        payload = graph_request(
            "GET",
            creation_id,
            params={"fields": "status_code", "access_token": access_token},
        )
        status = str(payload.get("status_code", "")).upper()
        print(f"Container {creation_id} status: {status}")
        if status in {"FINISHED", "PUBLISHED"}:
            return
        if status in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Media container {creation_id} failed with status {status}")
        time.sleep(min(3 * attempt, 15))
    raise RuntimeError(f"Media container {creation_id} did not become FINISHED in time")


def publish_post(
    ig_user_id: str,
    access_token: str,
    image: Path,
    caption_file: Path,
) -> str:
    caption = caption_file.read_text(encoding="utf-8").strip()
    if not caption:
        raise RuntimeError(f"Caption is empty: {caption_file}")

    image_url = public_image_url(image)
    wait_for_public_image(image_url)

    print(f"Creating Instagram media container for {image.as_posix()}")
    container = graph_request(
        "POST",
        f"{ig_user_id}/media",
        data={
            "image_url": image_url,
            "caption": caption,
            "access_token": access_token,
        },
    )
    creation_id = str(container["id"])
    wait_for_container(access_token, creation_id)

    print(f"Publishing Instagram media container {creation_id}")
    published = graph_request(
        "POST",
        f"{ig_user_id}/media_publish",
        data={
            "creation_id": creation_id,
            "access_token": access_token,
        },
    )
    media_id = str(published["id"])
    print(f"Instagram media published: id={media_id}")
    return media_id


def record_post(
    state: dict[str, Any],
    ig_user_id: str,
    image: Path,
    caption: Path,
    metadata: Path | None,
    media_id: str,
) -> None:
    state["posted"][image.as_posix()] = {
        "instagram_media_id": media_id,
        "ig_user_id": ig_user_id,
        "publisher": "meta",
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
    ig_user_id: str,
    access_token: str,
    state: dict[str, Any],
    image: Path,
    caption: Path,
    metadata: Path | None,
) -> None:
    media_id = publish_post(ig_user_id, access_token, image, caption)
    record_post(state, ig_user_id, image, caption, metadata, media_id)
    print(f"Published {image.as_posix()} as Instagram media {media_id}")
    commit_state_to_git()


def drain_queue(ig_user_id: str, access_token: str) -> int:
    deadline = time.time() + DRAIN_WITHIN_SECONDS
    initial_delay = random.randint(0, min(300, max(0, DRAIN_WITHIN_SECONDS // 12)))
    print(
        f"Drain mode: publish all pending posts randomly within {DRAIN_WITHIN_SECONDS}s "
        f"(initial delay {initial_delay}s)"
    )
    if initial_delay:
        time.sleep(initial_delay)

    published = 0
    while time.time() < deadline:
        state = load_state()
        pending = list_unpublished(state)
        if not pending:
            print("Queue empty; drain complete.")
            break

        image, caption, metadata = pending[0]
        try:
            publish_one(ig_user_id, access_token, state, image, caption, metadata)
        except DailyLimitReached as exc:
            leftover = len(list_unpublished(load_state()))
            print(
                f"Instagram publish limit reached after {published} post(s) this run: {exc}. "
                f"Leaving {leftover} queued for later automatic runs."
            )
            return published

        published += 1

        remaining = len(pending) - 1
        if remaining <= 0:
            print("Queue empty after this post; drain complete.")
            break

        seconds_left = deadline - time.time()
        delay = inter_post_delay_seconds(remaining, seconds_left)
        print(
            f"Remaining unpublished: {remaining}. "
            f"Sleeping {delay}s before next random publish."
        )
        if delay > 0:
            time.sleep(delay)

    leftover = len(list_unpublished(load_state()))
    if leftover:
        print(
            f"Drain window ended with {leftover} post(s) still queued; "
            "the next automatic run will continue."
        )
    return published


def publish_batch(ig_user_id: str, access_token: str) -> int:
    state = load_state()
    posts = discover_posts(state)
    if not posts:
        print("No unpublished generated posts found.")
        return 0

    published = 0
    for image, caption, metadata in posts:
        try:
            publish_one(ig_user_id, access_token, state, image, caption, metadata)
        except DailyLimitReached as exc:
            leftover = len(list_unpublished(load_state()))
            print(
                f"Instagram publish limit reached after {published} post(s) this run: {exc}. "
                f"Leaving {leftover} queued for later automatic runs."
            )
            return published
        published += 1
    return published


def main() -> int:
    ig_user_id = required_env("INSTAGRAM_IG_USER_ID")
    access_token = required_env("INSTAGRAM_ACCESS_TOKEN")

    if PUBLISH_MODE == "drain" and DRAIN_WITHIN_SECONDS > 0:
        published = drain_queue(ig_user_id, access_token)
    else:
        published = publish_batch(ig_user_id, access_token)

    print(f"Finished publish run. Posted {published} item(s).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DailyLimitReached as exc:
        print(
            f"Instagram publish limit reached: {exc}. "
            "Queued posts will retry on later automatic runs.",
            file=sys.stderr,
        )
        raise SystemExit(0)
    except Exception as exc:  # Keep Actions logs concise and actionable.
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
