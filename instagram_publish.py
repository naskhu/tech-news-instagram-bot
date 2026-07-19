#!/usr/bin/env python3
"""Publish generated Tech News posts to Instagram through Meta Graph API."""

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
GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION", "v23.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


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
    encoded_path = "/".join(quote(part) for part in image.as_posix().split("/"))
    return f"https://raw.githubusercontent.com/{REPOSITORY}/{quote(BRANCH)}/{encoded_path}"


def graph_post(endpoint: str, payload: dict[str, str], retries: int = 4) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(
                f"{GRAPH_BASE}/{endpoint}",
                data=payload,
                timeout=90,
            )
            data = response.json()
            if response.ok and "error" not in data:
                return data
            raise RuntimeError(f"Meta API HTTP {response.status_code}: {json.dumps(data)}")
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt == retries:
                break
            delay = 15 * attempt
            print(f"Meta API attempt {attempt} failed; retrying in {delay}s: {exc}")
            time.sleep(delay)
    raise RuntimeError(f"Meta API request failed: {last_error}")


def publish_post(ig_user_id: str, access_token: str, image: Path, caption_file: Path) -> str:
    caption = caption_file.read_text(encoding="utf-8").strip()
    if not caption:
        raise RuntimeError(f"Caption is empty: {caption_file}")

    image_url = public_image_url(image)
    print(f"Creating Instagram media container for {image.as_posix()}")
    container = graph_post(
        f"{ig_user_id}/media",
        {
            "image_url": image_url,
            "caption": caption,
            "access_token": access_token,
        },
    )
    creation_id = str(container["id"])

    print(f"Publishing Instagram media container {creation_id}")
    published = graph_post(
        f"{ig_user_id}/media_publish",
        {
            "creation_id": creation_id,
            "access_token": access_token,
        },
    )
    return str(published["id"])


def main() -> int:
    ig_user_id = required_env("INSTAGRAM_IG_USER_ID")
    access_token = required_env("INSTAGRAM_ACCESS_TOKEN")
    state = load_state()
    posts = discover_posts(state)

    if not posts:
        print("No unpublished generated posts found.")
        return 0

    for image, caption, metadata in posts:
        media_id = publish_post(ig_user_id, access_token, image, caption)
        state["posted"][image.as_posix()] = {
            "instagram_media_id": media_id,
            "caption_file": caption.as_posix(),
            "metadata_file": metadata.as_posix() if metadata else None,
            "image_url": public_image_url(image),
            "published_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        save_state(state)
        print(f"Published {image.as_posix()} as Instagram media {media_id}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # Keep Actions logs concise and actionable.
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
