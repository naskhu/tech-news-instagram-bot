#!/usr/bin/env python3
"""Delete all queued/scheduled Buffer posts across connected channels.

Usage:
  export BUFFER_ACCESS_TOKEN=...
  # optional filters:
  #   BUFFER_CHANNEL_ID=...
  #   BUFFER_THREADS_CHANNEL_ID=id1,id2
  #   BUFFER_ORGANIZATION_ID=...
  #   CLEAR_STATUSES=scheduled,sending,error,draft,needs_approval
  python3 clear_buffer_queues.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import requests

BUFFER_API_URL = os.getenv("BUFFER_API_URL", "https://api.buffer.com")
DEFAULT_CLEAR_STATUSES = ("scheduled", "sending", "error", "draft", "needs_approval")
# Known org for this project; used when account discovery is unavailable.
DEFAULT_ORGANIZATION_ID = "6a61ca7301d13814db599d28"


class RateLimitReached(RuntimeError):
    """Buffer API rate limit was hit; retry after the window resets."""


DELETE_MUTATION = """
mutation DeletePost($input: DeletePostInput!) {
  deletePost(input: $input) {
    __typename
    ... on DeletePostSuccess {
      id
    }
    ... on MutationError {
      message
    }
  }
}
"""

POSTS_QUERY = """
query Posts($input: PostsInput!, $first: Int!, $after: String) {
  posts(input: $input, first: $first, after: $after) {
    edges {
      cursor
      node {
        id
        status
        text
        dueAt
        channel {
          id
          name
          displayName
          service
        }
      }
    }
    pageInfo {
      endCursor
      hasNextPage
    }
  }
}
"""


def graphql(token: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.post(
        BUFFER_API_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables or {}},
        timeout=90,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Buffer API non-JSON HTTP {response.status_code}: {response.text[:300]}"
        ) from exc
    if not response.ok:
        body = json.dumps(payload)[:2000]
        if response.status_code == 429 or "rate_limit" in body.lower() or "too many requests" in body.lower():
            raise RateLimitReached(body)
        raise RuntimeError(f"Buffer API HTTP {response.status_code}: {body}")
    if payload.get("errors"):
        body = json.dumps(payload["errors"])[:2000]
        if "rate_limit" in body.lower() or "too many requests" in body.lower():
            raise RateLimitReached(body)
        raise RuntimeError(f"Buffer GraphQL errors: {body}")
    return payload.get("data") or {}


def discover_org_id(token: str, channel_ids: list[str] | None = None) -> str:
    configured = os.getenv("BUFFER_ORGANIZATION_ID", "").strip()
    if configured:
        return configured

    # Prefer the known project org immediately when the API is rate-limited,
    # instead of burning more discovery calls.
    rate_limited = False

    # Preferred: resolve org from a known channel id (works with publish secrets).
    candidates = list(channel_ids or [])
    for key in ("BUFFER_CHANNEL_ID", "BUFFER_THREADS_CHANNEL_ID"):
        value = os.getenv(key, "").strip()
        if not value:
            continue
        candidates.extend(part.strip() for part in value.split(",") if part.strip())
    seen: set[str] = set()
    for channel_id in candidates:
        if not channel_id or channel_id in seen:
            continue
        seen.add(channel_id)
        try:
            data = graphql(
                token,
                """
                query($input: ChannelInput!) {
                  channel(input: $input) {
                    id
                    organizationId
                  }
                }
                """,
                {"input": {"id": channel_id}},
            )
        except RateLimitReached as exc:
            rate_limited = True
            print(f"channel org lookup rate-limited for {channel_id}: {exc}", file=sys.stderr)
            break
        except RuntimeError as exc:
            print(f"channel org lookup failed for {channel_id}: {exc}", file=sys.stderr)
            continue
        channel = data.get("channel") or {}
        org_id = str(channel.get("organizationId") or "").strip()
        if org_id:
            return org_id

    if not rate_limited:
        # Fallback: account organizations list.
        for query in (
            "query { account { id organizations { id name } } }",
            "query { organizations { id name } }",
        ):
            try:
                data = graphql(token, query)
            except RateLimitReached as exc:
                rate_limited = True
                print(f"Org discovery rate-limited: {exc}", file=sys.stderr)
                break
            except RuntimeError as exc:
                print(f"Org discovery query failed: {exc}", file=sys.stderr)
                continue
            orgs: list[Any] = []
            if isinstance(data.get("account"), dict):
                orgs = data["account"].get("organizations") or []
            elif isinstance(data.get("organizations"), list):
                orgs = data["organizations"]
            if orgs and isinstance(orgs[0], dict) and orgs[0].get("id"):
                return str(orgs[0]["id"])

    if DEFAULT_ORGANIZATION_ID:
        print(f"Falling back to default organization id {DEFAULT_ORGANIZATION_ID}")
        return DEFAULT_ORGANIZATION_ID
    raise RuntimeError("Could not discover BUFFER_ORGANIZATION_ID")


def parse_channel_ids() -> list[str] | None:
    """Return configured channel ids, or None to clear every channel in the org."""
    raw_parts: list[str] = []
    for key in ("BUFFER_CHANNEL_ID", "BUFFER_THREADS_CHANNEL_ID"):
        value = os.getenv(key, "").strip()
        if value:
            raw_parts.extend(value.split(","))
    ids = [part.strip() for part in raw_parts if part.strip()]
    return ids or None


def parse_statuses() -> list[str]:
    raw = os.getenv("CLEAR_STATUSES", ",".join(DEFAULT_CLEAR_STATUSES)).strip()
    statuses = [part.strip() for part in raw.split(",") if part.strip()]
    return statuses or list(DEFAULT_CLEAR_STATUSES)


def list_queue_posts(
    token: str,
    org_id: str,
    statuses: list[str],
    channel_ids: list[str] | None,
) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        filters: dict[str, Any] = {"status": statuses}
        if channel_ids:
            filters["channelIds"] = channel_ids
        data = graphql(
            token,
            POSTS_QUERY,
            {
                "input": {"organizationId": org_id, "filter": filters},
                "first": 50,
                "after": after,
            },
        )
        connection = data.get("posts") or {}
        edges = connection.get("edges") or []
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            if isinstance(node, dict) and node.get("id"):
                posts.append(node)
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        after = page.get("endCursor")
        if not after:
            break
    return posts


def delete_post(token: str, post_id: str) -> None:
    data = graphql(token, DELETE_MUTATION, {"input": {"id": post_id}})
    result = data.get("deletePost") or {}
    typename = str(result.get("__typename") or "")
    if typename == "DeletePostSuccess" or result.get("id"):
        return
    message = result.get("message") or json.dumps(result)
    raise RuntimeError(f"deletePost failed for {post_id}: {message}")


def main() -> int:
    token = os.getenv("BUFFER_ACCESS_TOKEN", "").strip()
    if not token:
        print("Set BUFFER_ACCESS_TOKEN first.", file=sys.stderr)
        return 1

    try:
        channel_ids = parse_channel_ids()
        statuses = parse_statuses()
        org_id = discover_org_id(token, channel_ids)
        print(f"Organization: {org_id}")
        print(f"Statuses to clear: {', '.join(statuses)}")
        if channel_ids:
            print(f"Channels: {', '.join(channel_ids)}")
        else:
            print("Channels: all organization channels")

        posts = list_queue_posts(token, org_id, statuses, channel_ids)
        print(f"Found {len(posts)} queued post(s) to delete.")
        if not posts:
            return 0

        deleted = 0
        failed = 0
        for index, post in enumerate(posts, start=1):
            post_id = str(post["id"])
            channel = post.get("channel") or {}
            label = channel.get("displayName") or channel.get("name") or channel.get("id") or "?"
            status = post.get("status")
            preview = " ".join(str(post.get("text") or "").split())[:80]
            try:
                delete_post(token, post_id)
                deleted += 1
                print(
                    f"[{index}/{len(posts)}] deleted {post_id} "
                    f"status={status} channel={label!r} {preview!r}"
                )
            except RateLimitReached:
                raise
            except RuntimeError as exc:
                failed += 1
                print(f"[{index}/{len(posts)}] FAILED {post_id}: {exc}", file=sys.stderr)
            if index < len(posts):
                time.sleep(0.25)

        print(f"Done. deleted={deleted} failed={failed}")
        return 1 if failed else 0
    except RateLimitReached as exc:
        print(
            f"Buffer rate limit reached: {exc}. "
            "Retry after the API window resets (often ~24h).",
            file=sys.stderr,
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
