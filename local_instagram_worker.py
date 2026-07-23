"""Publish generated Instagram posts from the local machine.

This worker uses the unofficial ``instagrapi`` client. It does not run inside
GitHub Actions. Run it only on a trusted always-on computer, Raspberry Pi, VPS,
or Android/Termux device. Instagram may request login verification or restrict
accounts that use unofficial automation.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from instagrapi import Client
from instagrapi.exceptions import ChallengeRequired, LoginRequired

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
STATE_FILE = ROOT / ".local-instagram-posted.json"
SESSION_FILE = ROOT / ".instagram-session.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("instagram-worker")


def git_pull() -> None:
    """Download newly generated posts before checking the queue."""
    result = subprocess.run(
        ["git", "pull", "--ff-only"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git pull failed: {result.stderr.strip()}")
    if result.stdout.strip():
        LOGGER.info(result.stdout.strip())


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"posted": []}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read {STATE_FILE.name}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("posted", []), list):
        raise RuntimeError(f"Invalid state file: {STATE_FILE}")
    data.setdefault("posted", [])
    return data


def save_state(state: dict[str, Any]) -> None:
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def find_next_post(posted: set[str]) -> tuple[Path, Path] | None:
    if not OUTPUT_DIR.exists():
        return None

    for image in sorted(OUTPUT_DIR.glob("**/*.png")):
        relative = image.relative_to(ROOT).as_posix()
        caption = image.with_suffix(".txt")
        if relative not in posted and caption.exists():
            return image, caption
    return None


def login(username: str, password: str, verification_code: str | None) -> Client:
    client = Client()
    client.delay_range = [2, 5]

    if SESSION_FILE.exists():
        try:
            client.load_settings(SESSION_FILE)
            client.login(username, password, verification_code=verification_code)
            client.get_timeline_feed()
            LOGGER.info("Reused saved Instagram session")
            return client
        except (LoginRequired, Exception) as exc:  # reset stale sessions
            LOGGER.warning("Saved session could not be reused: %s", exc)
            client = Client()
            client.delay_range = [2, 5]

    client.login(username, password, verification_code=verification_code)
    client.dump_settings(SESSION_FILE)
    LOGGER.info("Created a new Instagram session")
    return client


def publish_one(*, dry_run: bool, pull: bool) -> int:
    load_dotenv(ROOT / ".env")

    username = os.getenv("INSTAGRAM_USERNAME", "").strip()
    password = os.getenv("INSTAGRAM_PASSWORD", "")
    verification_code = os.getenv("INSTAGRAM_VERIFICATION_CODE", "").strip() or None

    if not username or not password:
        raise RuntimeError(
            "Set INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD in a local .env file."
        )

    if pull:
        git_pull()

    state = load_state()
    posted = set(state["posted"])
    queued = find_next_post(posted)
    if queued is None:
        LOGGER.info("No unpublished image/caption pair found")
        return 0

    image, caption_file = queued
    caption = caption_file.read_text(encoding="utf-8").strip()
    if not caption:
        raise RuntimeError(f"Caption is empty: {caption_file}")

    LOGGER.info("Next post: %s", image.relative_to(ROOT))
    if dry_run:
        LOGGER.info("Dry run enabled; nothing was uploaded")
        return 0

    try:
        client = login(username, password, verification_code)
        media = client.photo_upload(image, caption)
    except ChallengeRequired as exc:
        raise RuntimeError(
            "Instagram requested a security challenge. Open Instagram, approve the "
            "login, then run the worker again."
        ) from exc

    relative = image.relative_to(ROOT).as_posix()
    state["posted"].append(relative)
    state["last_media_pk"] = str(media.pk)
    save_state(state)
    LOGGER.info("Published successfully: %s", relative)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish the next generated post")
    parser.add_argument("--dry-run", action="store_true", help="Do not upload")
    parser.add_argument(
        "--no-pull",
        action="store_true",
        help="Do not run git pull before checking the queue",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return publish_one(dry_run=args.dry_run, pull=not args.no_pull)
    except Exception as exc:
        LOGGER.error("Worker failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
