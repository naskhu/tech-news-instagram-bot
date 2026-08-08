"""Small Zernio REST client for Threads publishing."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any

import requests

ZERNIO_API_URL = os.getenv("ZERNIO_API_URL", "https://zernio.com/api").rstrip("/")


class ZernioError(RuntimeError):
    """Zernio rejected or could not process a publishing request."""


class ZernioRateLimitReached(ZernioError):
    """Zernio returned HTTP 429."""


class ZernioDailyLimitReached(ZernioError):
    """The destination platform's daily publishing limit was reached."""


@dataclass(frozen=True)
class CreatedPost:
    post_id: str
    status: str
    existing: bool = False


def request_id(*parts: object) -> str:
    """Stable UUID for one logical publish operation."""
    material = "\x1f".join(str(part) for part in parts)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tech-news-instagram-bot:{material}"))


def _error_text(payload: object) -> str:
    if isinstance(payload, dict):
        return str(payload.get("error") or payload.get("message") or json.dumps(payload))
    return str(payload)


def _looks_like_daily_limit(message: object) -> bool:
    text = str(message or "").lower()
    return (
        ("daily" in text and "limit" in text)
        or "250 api-published posts" in text
        or "maximum number of posts" in text
    )


def create_post(
    api_key: str,
    *,
    platform: str,
    account_id: str,
    content: str,
    image_url: str,
    scheduled_for: str | None = None,
    publish_now: bool = False,
    idempotency_key: str,
) -> CreatedPost:
    """Create one Zernio image post for one connected social account."""
    body: dict[str, Any] = {
        "content": content,
        "mediaItems": [{"type": "image", "url": image_url}],
        "platforms": [{"platform": platform, "accountId": account_id}],
    }
    if scheduled_for:
        body["scheduledFor"] = scheduled_for
        body["timezone"] = "UTC"
    elif publish_now:
        body["publishNow"] = True
    else:
        raise ValueError("Either scheduled_for or publish_now is required")

    response = requests.post(
        f"{ZERNIO_API_URL}/v1/posts",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "x-request-id": idempotency_key,
        },
        json=body,
        timeout=90,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ZernioError(
            f"Zernio returned non-JSON HTTP {response.status_code}: {response.text[:300]}"
        ) from exc

    # Content-hash dedup means the original request already succeeded. Recording
    # the existing post prevents this bot from retrying it forever.
    if response.status_code == 409 and isinstance(payload, dict):
        details = payload.get("details")
        existing_id = details.get("existingPostId") if isinstance(details, dict) else None
        if existing_id:
            return CreatedPost(str(existing_id), "duplicate", existing=True)

    message = _error_text(payload)
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        suffix = f" Retry-After={retry_after}s." if retry_after else ""
        if _looks_like_daily_limit(message):
            raise ZernioDailyLimitReached(message + suffix)
        raise ZernioRateLimitReached(message + suffix)
    if not response.ok:
        if _looks_like_daily_limit(message):
            raise ZernioDailyLimitReached(message)
        raise ZernioError(f"Zernio HTTP {response.status_code}: {message}")

    post: object = payload.get("post") if isinstance(payload, dict) else None
    if not isinstance(post, dict):
        existing = payload.get("existingPost") if isinstance(payload, dict) else None
        if isinstance(existing, dict):
            post = existing
    if not isinstance(post, dict):
        raise ZernioError(f"Unexpected Zernio create-post response: {json.dumps(payload)[:1000]}")

    post_id = str(post.get("_id") or post.get("id") or "").strip()
    if not post_id:
        raise ZernioError(f"Zernio response has no post id: {json.dumps(payload)[:1000]}")
    status = str(post.get("status") or "accepted").strip().lower()
    return CreatedPost(post_id, status)
