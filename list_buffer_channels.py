#!/usr/bin/env python3
"""List Buffer channels so you can find the Threads channel id.

Usage:
  export BUFFER_ACCESS_TOKEN=...
  # optional: export BUFFER_ORGANIZATION_ID=...
  python3 list_buffer_channels.py
"""

from __future__ import annotations

import json
import os
import sys

import requests

BUFFER_API_URL = os.getenv("BUFFER_API_URL", "https://api.buffer.com")


def graphql(token: str, query: str, variables: dict | None = None) -> dict:
    response = requests.post(
        BUFFER_API_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables or {}},
        timeout=60,
    )
    payload = response.json()
    if not response.ok or payload.get("errors"):
        raise RuntimeError(json.dumps(payload, indent=2)[:2000])
    return payload.get("data") or {}


def main() -> int:
    token = os.getenv("BUFFER_ACCESS_TOKEN", "").strip()
    if not token:
        print("Set BUFFER_ACCESS_TOKEN first.", file=sys.stderr)
        return 1

    org_id = os.getenv("BUFFER_ORGANIZATION_ID", "").strip()
    if not org_id:
        # Best-effort discovery of organizations.
        for query in (
            "query { account { id organizations { id name } } }",
            "query { organizations { id name } }",
        ):
            try:
                data = graphql(token, query)
            except RuntimeError as exc:
                print(f"Org discovery query failed: {exc}", file=sys.stderr)
                continue
            orgs = []
            if isinstance(data.get("account"), dict):
                orgs = data["account"].get("organizations") or []
            elif isinstance(data.get("organizations"), list):
                orgs = data["organizations"]
            if orgs:
                print("Organizations:")
                for org in orgs:
                    print(f"  {org.get('id')}  {org.get('name')}")
                org_id = str(orgs[0].get("id") or "").strip()
                break

    if not org_id:
        print(
            "Could not discover organization id. Set BUFFER_ORGANIZATION_ID and retry.",
            file=sys.stderr,
        )
        return 1

    data = graphql(
        token,
        """
        query($input: ChannelsInput!) {
          channels(input: $input) {
            id
            name
            displayName
            service
            type
            isDisconnected
            externalLink
          }
        }
        """,
        {"input": {"organizationId": org_id}},
    )
    channels = data.get("channels") or []
    print(f"\nChannels for organization {org_id}:")
    threads_ids: list[str] = []
    for channel in channels:
        service = str(channel.get("service") or channel.get("type") or "").lower()
        label = channel.get("displayName") or channel.get("name") or ""
        line = (
            f"  {channel.get('id')}  service={service}  "
            f"name={label!r}  disconnected={channel.get('isDisconnected')}"
        )
        print(line)
        if "thread" in service or "thread" in label.lower():
            threads_ids.append(str(channel.get("id")))

    if threads_ids:
        print("\nLikely Threads channel id(s):")
        for channel_id in threads_ids:
            print(f"  {channel_id}")
        print("\nSet GitHub secret BUFFER_THREADS_CHANNEL_ID to one of these.")
    else:
        print(
            "\nNo obvious Threads channel found. In Buffer: connect Threads, "
            "then rerun this script.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
