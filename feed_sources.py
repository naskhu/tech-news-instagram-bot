from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

import bot


def _allowed_fallback_url(url: str, feed: dict) -> bool:
    parsed = urlparse(url)
    base = urlparse(feed.get("fallback_url", feed["url"]))
    if parsed.netloc.lower().removeprefix("www.") != base.netloc.lower().removeprefix("www."):
        return False
    path = parsed.path.rstrip("/")
    if not path or path == base.path.rstrip("/"):
        return False
    include_paths = feed.get("fallback_include_paths", [])
    return not include_paths or any(marker in path for marker in include_paths)


def _collect_html_fallback(feed: dict, processed: set[str], limit: int) -> list[dict]:
    fallback_url = feed.get("fallback_url")
    if not fallback_url:
        return []

    try:
        response = requests.get(fallback_url, headers=bot.HEADERS, timeout=25)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"HTML fallback error [{feed['name']}]: {exc}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    stories: list[dict] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        url = urljoin(fallback_url, str(anchor.get("href", "")).strip()).split("#", 1)[0]
        if url in seen or not _allowed_fallback_url(url, feed):
            continue
        title = bot.clean_text(
            str(anchor.get("aria-label") or anchor.get("title") or anchor.get_text(" ", strip=True))
        )
        if len(title.split()) < 5 or len(title) < 28:
            continue
        sid = bot.story_id(url)
        if sid in processed:
            continue
        seen.add(url)
        stories.append({
            "id": sid,
            "title": title,
            "url": url,
            "source": feed["name"],
            "published": datetime.now(timezone.utc),
            "rss_text": "",
            "rss_images": [],
        })
        if len(stories) >= limit:
            break

    return stories


def collect_rss_stories(config: dict, processed: set[str]) -> list[dict]:
    all_stories: list[dict] = []

    for feed in config.get("feeds", []):
        if feed.get("type", "rss") != "rss":
            continue

        limit = max(12, int(feed.get("max_entries", config.get("feed_max_entries", 50))))
        parsed = feedparser.parse(feed["url"], request_headers=bot.HEADERS)
        entries = list(parsed.entries[:limit])
        fresh: list[dict] = []

        for entry in entries:
            url = str(entry.get("link", "")).strip()
            title = bot.clean_text(str(entry.get("title", "")))
            if not url or not title:
                continue
            sid = bot.story_id(url)
            if sid in processed:
                continue
            fresh.append({
                "id": sid,
                "title": title,
                "url": url,
                "source": feed["name"],
                "published": bot.parse_date(entry),
                "rss_text": bot.clean_text(entry.get("summary", "") or entry.get("description", "")),
                "rss_images": bot.image_candidates_from_entry(entry),
            })

        fallback: list[dict] = []
        if feed.get("fallback_url"):
            fallback = _collect_html_fallback(feed, processed, limit)

        combined: dict[str, dict] = {}
        for story in fresh + fallback:
            combined.setdefault(story["id"], story)

        bozo = bool(getattr(parsed, "bozo", False))
        error_text = str(getattr(parsed, "bozo_exception", "")) if bozo else "none"
        print(
            f"Checked {feed['name']}: feed_entries={len(entries)}, "
            f"new_rss={len(fresh)}, html_fallback={len(fallback)}, "
            f"parse_error={error_text}"
        )
        all_stories.extend(combined.values())

    all_stories.sort(key=lambda item: item["published"], reverse=True)
    return all_stories
