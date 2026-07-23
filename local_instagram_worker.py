"""Publish generated Instagram posts from the local machine.

This worker uses the unofficial ``instagrapi`` client. It does not run inside
GitHub Actions. Run it only on a trusted always-on computer, Raspberry Pi, VPS,
or Android/Termux device. Instagram may request login verification or restrict
accounts that use unofficial automation.

Use this when Meta Content Publishing is unavailable and Buffer is not an option.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from instagrapi import Client
from instagrapi.exceptions import ChallengeRequired, LoginRequired

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
STATE_FILE = ROOT / ".local-instagram-posted.json"
REMOTE_STATE_FILE = ROOT / "instagram-posted.json"
SESSION_FILE = ROOT / ".instagram-session.json"
KEYCHAIN_SERVICE = "com.news.world.tech.instagram-worker"

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


def load_local_state() -> dict[str, Any]:
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


def save_local_state(state: dict[str, Any]) -> None:
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def load_remote_posted() -> set[str]:
    """Skip posts already published by Buffer/Meta via GitHub Actions."""
    if not REMOTE_STATE_FILE.exists():
        return set()
    try:
        data = json.loads(REMOTE_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    posted = data.get("posted", {})
    return set(posted.keys()) if isinstance(posted, dict) else set()


def list_unpublished(posted: set[str]) -> list[tuple[Path, Path]]:
    if not OUTPUT_DIR.exists():
        return []
    candidates: list[tuple[Path, Path]] = []
    for image in sorted(OUTPUT_DIR.glob("**/*.png")):
        relative = image.relative_to(ROOT).as_posix()
        caption = image.with_suffix(".txt")
        if relative not in posted and caption.exists():
            candidates.append((image, caption))
    return candidates


def harden_env_file(path: Path) -> None:
    """Restrict .env to the current user only (owner read/write)."""
    if path.exists():
        os.chmod(path, 0o600)


def keychain_get_password(username: str) -> str | None:
    """Read the Instagram password from the macOS login Keychain."""
    if sys.platform != "darwin":
        return None
    result = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-a",
            username,
            "-s",
            KEYCHAIN_SERVICE,
            "-w",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    password = result.stdout.rstrip("\n")
    return password or None


def keychain_store_password(username: str, password: str) -> None:
    """Store/update the Instagram password in the macOS login Keychain."""
    if sys.platform != "darwin":
        raise RuntimeError("Keychain storage is only supported on macOS.")
    result = subprocess.run(
        [
            "security",
            "add-generic-password",
            "-a",
            username,
            "-s",
            KEYCHAIN_SERVICE,
            "-w",
            password,
            "-U",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"Could not store password in Keychain: {detail}")


def resolve_credentials() -> tuple[str, str, str | None]:
    """Prefer Keychain for the password; keep username in local .env."""
    env_path = ROOT / ".env"
    load_dotenv(env_path)
    harden_env_file(env_path)

    username = os.getenv("INSTAGRAM_USERNAME", "").strip()
    verification_code = os.getenv("INSTAGRAM_VERIFICATION_CODE", "").strip() or None
    if not username:
        raise RuntimeError("Set INSTAGRAM_USERNAME in a local .env file.")

    password = keychain_get_password(username)
    source = "macOS Keychain"
    if not password:
        password = os.getenv("INSTAGRAM_PASSWORD", "")
        source = ".env (INSTAGRAM_PASSWORD)"
    if not password:
        raise RuntimeError(
            "No Instagram password found. On macOS run:\n"
            "  python local_instagram_worker.py --store-password\n"
            "Or set INSTAGRAM_PASSWORD temporarily in .env (not recommended)."
        )

    LOGGER.info("Loaded credentials for %s from %s", username, source)
    if source.startswith(".env"):
        LOGGER.warning(
            "Password is in .env. Prefer Keychain so other users cannot read it: "
            "python local_instagram_worker.py --store-password"
        )
    return username, password, verification_code


def store_password_interactively() -> int:
    env_path = ROOT / ".env"
    load_dotenv(env_path)
    harden_env_file(env_path)

    username = os.getenv("INSTAGRAM_USERNAME", "").strip()
    if not username:
        username = input("Instagram username: ").strip()
    if not username:
        raise RuntimeError("Username is required.")

    password = getpass.getpass("Instagram password (hidden): ")
    confirm = getpass.getpass("Confirm password (hidden): ")
    if not password:
        raise RuntimeError("Password cannot be empty.")
    if password != confirm:
        raise RuntimeError("Passwords do not match.")

    keychain_store_password(username, password)

    # Keep only non-secret identity settings in .env.
    lines = [
        f"INSTAGRAM_USERNAME={username}",
        "INSTAGRAM_VERIFICATION_CODE=",
        "# Password is stored in macOS Keychain (not in this file).",
        "",
    ]
    env_path.write_text("\n".join(lines), encoding="utf-8")
    harden_env_file(env_path)
    LOGGER.info(
        "Password stored in Keychain service %s for account %s",
        KEYCHAIN_SERVICE,
        username,
    )
    LOGGER.info("Updated %s without a password field", env_path.name)
    return 0


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


def inter_post_delay_seconds(remaining_after: int, seconds_left: float) -> int:
    if remaining_after <= 0 or seconds_left <= 30:
        return 0
    average = max(60, int((seconds_left * 0.9) / remaining_after))
    low = max(45, int(average * 0.45))
    high = max(low + 1, min(int(average * 1.35), 600))
    return random.randint(low, high)


def publish_posts(
    *,
    dry_run: bool,
    pull: bool,
    max_posts: int | None,
    drain_within_seconds: int,
) -> int:
    username, password, verification_code = resolve_credentials()

    if pull:
        git_pull()

    state = load_local_state()
    posted = set(state["posted"]) | load_remote_posted()
    pending = list_unpublished(posted)
    if not pending:
        LOGGER.info("No unpublished image/caption pair found")
        return 0

    if max_posts is not None:
        pending = pending[: max(1, max_posts)]

    LOGGER.info("Queued unpublished posts: %s", len(pending))
    if dry_run:
        for image, _caption in pending:
            LOGGER.info("Dry run next: %s", image.relative_to(ROOT))
        LOGGER.info("Dry run enabled; nothing was uploaded")
        return 0

    client = login(username, password, verification_code)
    deadline = time.time() + drain_within_seconds if drain_within_seconds > 0 else None
    if drain_within_seconds > 0:
        initial_delay = random.randint(0, min(180, max(0, drain_within_seconds // 12)))
        LOGGER.info(
            "Drain mode for %ss with initial delay %ss",
            drain_within_seconds,
            initial_delay,
        )
        if initial_delay:
            time.sleep(initial_delay)

    published = 0
    for index, (image, caption_file) in enumerate(pending):
        if deadline is not None and time.time() >= deadline:
            LOGGER.info(
                "Drain window ended after %s post(s); %s remain for later runs",
                published,
                len(pending) - index,
            )
            break

        caption = caption_file.read_text(encoding="utf-8").strip()
        if not caption:
            raise RuntimeError(f"Caption is empty: {caption_file}")

        relative = image.relative_to(ROOT).as_posix()
        LOGGER.info("Publishing: %s", relative)
        try:
            media = client.photo_upload(image, caption)
        except ChallengeRequired as exc:
            raise RuntimeError(
                "Instagram requested a security challenge. Open Instagram, approve the "
                "login, then run the worker again."
            ) from exc

        state["posted"].append(relative)
        state["last_media_pk"] = str(media.pk)
        save_local_state(state)
        posted.add(relative)
        published += 1
        LOGGER.info("Published successfully: %s", relative)

        remaining = len(pending) - index - 1
        if remaining <= 0:
            break
        if deadline is None:
            # Single-batch mode: short random pause between posts.
            delay = random.randint(45, 180)
        else:
            delay = inter_post_delay_seconds(remaining, deadline - time.time())
        LOGGER.info("Sleeping %ss before next random publish (%s remaining)", delay, remaining)
        if delay > 0:
            time.sleep(delay)

    LOGGER.info("Finished local publish run. Posted %s item(s).", published)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish generated Instagram posts from the local git queue"
    )
    parser.add_argument(
        "--store-password",
        action="store_true",
        help="Prompt for the Instagram password and store it in macOS Keychain",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not upload")
    parser.add_argument(
        "--no-pull",
        action="store_true",
        help="Do not run git pull before checking the queue",
    )
    parser.add_argument(
        "--max-posts",
        type=int,
        default=None,
        help="Maximum posts to publish this run (default: all queued)",
    )
    parser.add_argument(
        "--drain-within-minutes",
        type=int,
        default=55,
        help="Spread posts randomly across this many minutes (0 disables drain timing)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.store_password:
            return store_password_interactively()
        drain_seconds = max(0, int(args.drain_within_minutes)) * 60
        return publish_posts(
            dry_run=args.dry_run,
            pull=not args.no_pull,
            max_posts=args.max_posts,
            drain_within_seconds=drain_seconds,
        )
    except Exception as exc:
        LOGGER.error("Worker failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
