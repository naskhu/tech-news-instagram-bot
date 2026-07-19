from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import bot


def _allowed_article(url: str, source: dict) -> bool:
    parsed = urlparse(url)
    base = urlparse(source["url"])
    if parsed.netloc.lower().removeprefix("www.") != base.netloc.lower().removeprefix("www."):
        return False
    path = parsed.path.rstrip("/")
    if path == base.path.rstrip("/"):
        return False
    include_paths = source.get("include_paths", [])
    return not include_paths or any(marker in path for marker in include_paths)


def collect_direct_stories(config: dict, processed: set[str]) -> list[dict]:
    stories: list[dict] = []
    seen_urls: set[str] = set()

    for source in config.get("feeds", []):
        if source.get("type") != "html":
            continue

        discovered = 0
        try:
            response = requests.get(source["url"], headers=bot.HEADERS, timeout=25)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as exc:
            print(f"Source error [{source['name']}]: {exc}")
            continue

        for anchor in soup.find_all("a", href=True):
            url = urljoin(source["url"], str(anchor.get("href", "")).strip()).split("#", 1)[0]
            if url in seen_urls or not _allowed_article(url, source):
                continue

            title = bot.clean_text(
                str(anchor.get("aria-label") or anchor.get("title") or anchor.get_text(" ", strip=True))
            )
            if len(title.split()) < 5 or len(title) < 28:
                continue

            sid = bot.story_id(url)
            if sid in processed:
                continue

            seen_urls.add(url)
            stories.append(
                {
                    "id": sid,
                    "title": title,
                    "url": url,
                    "source": source["name"],
                    "published": datetime.now(timezone.utc),
                    "rss_text": "",
                    "rss_images": [],
                }
            )
            discovered += 1
            if discovered >= int(source.get("max_articles", 20)):
                break

        print(f"Checked {source['name']}: {discovered} new article link(s) found.")

    return stories
