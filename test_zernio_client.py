from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from threads_publish import generated_order_key
from zernio_client import (
    ZernioRateLimitReached,
    create_post,
    request_id,
)


class ZernioClientTests(unittest.TestCase):
    @patch("zernio_client.requests.post")
    def test_creates_scheduled_threads_post(self, post: Mock) -> None:
        response = Mock(status_code=201, ok=True)
        response.json.return_value = {
            "post": {"_id": "post-1", "status": "scheduled"}
        }
        post.return_value = response

        created = create_post(
            "secret",
            platform="threads",
            account_id="account-1",
            content="hello",
            image_url="https://example.com/image.png",
            scheduled_for="2026-08-09T01:30:00.000Z",
            idempotency_key=request_id("threads", "account-1", "image.png"),
        )

        self.assertEqual(created.post_id, "post-1")
        self.assertEqual(created.status, "scheduled")
        body = post.call_args.kwargs["json"]
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(body["platforms"][0]["platform"], "threads")
        self.assertEqual(body["scheduledFor"], "2026-08-09T01:30:00.000Z")
        self.assertNotIn("publishNow", body)
        self.assertEqual(
            headers["x-request-id"],
            request_id("threads", "account-1", "image.png"),
        )

    @patch("zernio_client.requests.post")
    def test_content_duplicate_is_recorded_as_success(self, post: Mock) -> None:
        response = Mock(status_code=409, ok=False)
        response.json.return_value = {
            "error": "duplicate",
            "details": {"existingPostId": "existing-1"},
        }
        post.return_value = response

        created = create_post(
            "secret",
            platform="threads",
            account_id="account-1",
            content="hello",
            image_url="https://example.com/image.png",
            publish_now=True,
            idempotency_key=request_id("threads", "account-1", "image.png"),
        )

        self.assertEqual(created.post_id, "existing-1")
        self.assertTrue(created.existing)

    @patch("zernio_client.requests.post")
    def test_rate_limit_includes_retry_after(self, post: Mock) -> None:
        response = Mock(status_code=429, ok=False)
        response.headers = {"Retry-After": "120"}
        response.json.return_value = {"error": "Too many requests"}
        post.return_value = response

        with self.assertRaisesRegex(ZernioRateLimitReached, "Retry-After=120s"):
            create_post(
                "secret",
                platform="threads",
                account_id="account-1",
                content="hello",
                image_url="https://example.com/image.png",
                publish_now=True,
                idempotency_key=request_id("threads", "account-1", "image.png"),
            )

    def test_fifo_uses_generated_metadata_not_file_mtime(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            older = root / "z-older.png"
            newer = root / "a-newer.png"
            older.touch()
            newer.touch()
            older.with_suffix(".json").write_text(
                '{"generated_utc":"2026-08-09T01:00:00+00:00"}',
                encoding="utf-8",
            )
            newer.with_suffix(".json").write_text(
                '{"generated_utc":"2026-08-09T02:00:00+00:00"}',
                encoding="utf-8",
            )

            ordered = sorted([newer, older], key=generated_order_key)
            self.assertEqual(ordered, [older, newer])


if __name__ == "__main__":
    unittest.main()
