#!/usr/bin/env python3
"""Publish generated Tech News posts to Instagram through Buffer's GraphQL API."""

from __future__ import annotations

import json
import os
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


def discover_posts(state: dict[str, Any]) -> list[tuple[Path, Path, Path | None]]:
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

    return candidates[:MAX_POSTS]


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
        raise RuntimeError(f"Buffer API HTTP {response.status_code}: {json.dumps(payload)}")
    if payload.get("errors"):
        raise RuntimeError(f"Buffer GraphQL errors: {json.dumps(payload['errors'])}")
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
            }
        },
    )

    result = (payload.get("data") or {}).get("createPost") or {}
    typename = result.get("__typename")
    if typename == "MutationError" or result.get("message"):
        raise RuntimeError(f"Buffer rejected post: {result.get('message') or result}")
    if typename != "PostActionSuccess" or not result.get("post", {}).get("id"):
        raise RuntimeError(f"Unexpected Buffer createPost response: {json.dumps(result)}")

    post_id = str(result["post"]["id"])
    print(f"Buffer post created: id={post_id} status={result['post'].get('status')}")
    return post_id


def main() -> int:
    access_token = required_env("BUFFER_ACCESS_TOKEN")
    channel_id = required_env("BUFFER_CHANNEL_ID")
    state = load_state()
    posts = discover_posts(state)

    if not posts:
        print("No unpublished generated posts found.")
        return 0

    for image, caption, metadata in posts:
        post_id = publish_post(access_token, channel_id, image, caption)
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
        print(f"Published {image.as_posix()} as Buffer post {post_id}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # Keep Actions logs concise and actionable.
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
