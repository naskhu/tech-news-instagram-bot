from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import bot

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,image/png,image/jpeg,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# Ensure article and image requests made by bot.py use the same browser-like headers.
bot.HEADERS.clear()
bot.HEADERS.update(BROWSER_HEADERS)

TRACKING_QUERY_PREFIXES = (
    "utm_",
    "ga_",
)
TRACKING_QUERY_KEYS = {
    "ref",
    "referrer",
    "source",
    "output",
    "guccounter",
    "guce_referrer",
    "guce_referrer_sig",
    "ncid",
    "cmpid",
    "soc_src",
    "soc_trk",
}

NON_ARTICLE_PATH_PARTS = {
    "about",
    "advertise",
    "archive",
    "author",
    "authors",
    "careers",
    "category",
    "contact",
    "cookie",
    "events",
    "feed",
    "feeds",
    "forum",
    "help",
    "login",
    "newsletter",
    "newsletters",
    "podcast",
    "privacy",
    "search",
    "shop",
    "signin",
    "sign-in",
    "subscribe",
    "tag",
    "tags",
    "terms",
    "topic",
    "topics",
    "video",
    "videos",
}

DATE_META_KEYS = (
    ("property", "article:published_time"),
    ("property", "article:modified_time"),
    ("name", "date"),
    ("name", "datePublished"),
    ("name", "pub_date"),
    ("name", "sailthru.date"),
    ("itemprop", "datePublished"),
)

IMAGE_META_KEYS = (
    ("property", "og:image"),
    ("property", "og:image:secure_url"),
    ("name", "twitter:image"),
    ("name", "twitter:image:src"),
)

TITLE_META_KEYS = (
    ("property", "og:title"),
    ("name", "twitter:title"),
)

DESCRIPTION_META_KEYS = (
    ("property", "og:description"),
    ("name", "description"),
    ("name", "twitter:description"),
)


def _session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        status=2,
        backoff_factor=0.6,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.headers.update(BROWSER_HEADERS)
    return session


def canonicalize_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""

    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    clean_query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lower = key.lower()
        if lower in TRACKING_QUERY_KEYS or any(lower.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        clean_query.append((key, value))

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")

    return urlunsplit(("https", host, path, urlencode(clean_query, doseq=True), ""))


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def _same_allowed_host(url: str, source: dict[str, Any]) -> bool:
    allowed = source.get("allowed_hosts")
    if not allowed:
        roots = []
        for candidate in source.get("page_urls", []):
            roots.append(_host(candidate))
        for candidate in source.get("rss_urls", []):
            roots.append(_host(candidate))
        allowed = [item for item in roots if item]
    current = _host(url)
    return current in {str(item).lower().removeprefix("www.") for item in allowed}


def _looks_like_article_url(url: str, source: dict[str, Any]) -> bool:
    if not url or not _same_allowed_host(url, source):
        return False

    path = urlsplit(url).path.strip("/")
    if not path or len(path) < 10:
        return False
    lower = path.lower()
    if lower.endswith((".xml", ".rss", ".atom", ".json", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg")):
        return False

    segments = [segment for segment in lower.split("/") if segment]
    if any(segment in NON_ARTICLE_PATH_PARTS for segment in segments):
        return False

    include_paths = source.get("include_paths", [])
    if include_paths and not any(marker.lower() in f"/{lower}/" for marker in include_paths):
        return False

    exclude_paths = source.get("exclude_paths", [])
    if any(marker.lower() in f"/{lower}/" for marker in exclude_paths):
        return False

    return True


def _first_meta(soup: BeautifulSoup, candidates: tuple[tuple[str, str], ...]) -> str:
    for attr, value in candidates:
        tag = soup.find("meta", attrs={attr: value})
        if tag:
            content = str(tag.get("content", "")).strip()
            if content:
                return content
    return ""


def _all_meta(soup: BeautifulSoup, candidates: tuple[tuple[str, str], ...], base_url: str) -> list[str]:
    result: list[str] = []
    for attr, value in candidates:
        for tag in soup.find_all("meta", attrs={attr: value}):
            content = str(tag.get("content", "")).strip()
            if content:
                absolute = urljoin(base_url, content)
                if absolute not in result:
                    result.append(absolute)
    return result


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass

    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass

    match = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if match:
        year, month, day, hour, minute, second = match.groups()
        return datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second or 0),
            tzinfo=timezone.utc,
        )
    return None


def _json_ld_objects(soup: BeautifulSoup) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            objects.append(value)
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            walk(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return objects


def _article_metadata(
    session: requests.Session,
    url: str,
    source: dict[str, Any],
    fallback_title: str = "",
    rss_text: str = "",
    rss_images: list[str] | None = None,
    rss_published: datetime | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        response = session.get(url, timeout=25, allow_redirects=True)
    except requests.RequestException as exc:
        return None, f"request_error:{type(exc).__name__}"

    if response.status_code >= 400:
        return None, f"http_{response.status_code}"

    final_url = canonicalize_url(response.url)
    if not final_url or not _same_allowed_host(final_url, source):
        return None, "redirected_offsite"

    content_type = response.headers.get("content-type", "").lower()
    if "html" not in content_type and "xhtml" not in content_type and content_type:
        return None, f"not_html:{content_type.split(';', 1)[0]}"

    soup = BeautifulSoup(response.text, "html.parser")

    canonical_tag = soup.find("link", rel=lambda value: value and "canonical" in value)
    canonical = canonicalize_url(urljoin(final_url, str(canonical_tag.get("href", "")))) if canonical_tag else final_url
    if canonical and _same_allowed_host(canonical, source):
        final_url = canonical

    if not _looks_like_article_url(final_url, source):
        return None, "not_article_path"

    title = _first_meta(soup, TITLE_META_KEYS)
    if not title:
        heading = soup.find("h1")
        title = heading.get_text(" ", strip=True) if heading else fallback_title
    title = bot.clean_text(title)
    if len(title.split()) < 4:
        return None, "missing_title"

    description = bot.clean_text(_first_meta(soup, DESCRIPTION_META_KEYS) or rss_text)

    published = rss_published
    date_source = "rss" if rss_published else ""

    for attr, key in DATE_META_KEYS:
        tag = soup.find("meta", attrs={attr: key})
        if tag:
            candidate = _parse_datetime(tag.get("content"))
            if candidate:
                published = candidate
                date_source = "meta"
                break

    json_objects = _json_ld_objects(soup)
    is_article = False
    for obj in json_objects:
        obj_type = obj.get("@type")
        types = obj_type if isinstance(obj_type, list) else [obj_type]
        if any(str(item).lower() in {"article", "newsarticle", "reportagenewsarticle", "blogposting", "review"} for item in types):
            is_article = True
        if not published:
            for key in ("datePublished", "dateCreated", "dateModified", "uploadDate"):
                candidate = _parse_datetime(obj.get(key))
                if candidate:
                    published = candidate
                    date_source = "json_ld"
                    break
        if not description:
            description = bot.clean_text(str(obj.get("description", "")))
        if not title:
            title = bot.clean_text(str(obj.get("headline") or obj.get("name") or ""))

    og_type = _first_meta(soup, (("property", "og:type"),)).lower()
    if og_type == "article":
        is_article = True

    if not published:
        time_tag = soup.find("time", datetime=True)
        if time_tag:
            published = _parse_datetime(time_tag.get("datetime"))
            if published:
                date_source = "time"

    if not is_article and not (published and len(title.split()) >= 5):
        return None, "no_article_metadata"

    if not published:
        published = datetime.now(timezone.utc)
        date_source = "assumed_now"

    images = list(rss_images or [])
    for candidate in _all_meta(soup, IMAGE_META_KEYS, final_url):
        if candidate not in images:
            images.append(candidate)

    for tag in soup.find_all("link", attrs={"rel": "preload", "as": "image"}):
        candidate = urljoin(final_url, str(tag.get("href", "")).strip())
        if candidate and candidate not in images:
            images.append(candidate)

    return {
        "id": bot.story_id(final_url),
        "title": title,
        "url": final_url,
        "source": source["name"],
        "published": published,
        "rss_text": description,
        "rss_images": images[:20],
        "date_source": date_source,
    }, None


def _anchor_title(anchor: Any) -> str:
    candidates: list[str] = []

    heading = anchor.find(["h1", "h2", "h3", "h4"])
    if heading:
        candidates.append(heading.get_text(" ", strip=True))

    for attr in ("aria-label", "title"):
        value = str(anchor.get(attr, "")).strip()
        if value:
            candidates.append(value)

    image = anchor.find("img")
    if image:
        alt = str(image.get("alt", "")).strip()
        if alt:
            candidates.append(alt)

    candidates.append(anchor.get_text(" ", strip=True))

    for value in candidates:
        cleaned = bot.clean_text(value)
        if len(cleaned) >= 24 and len(cleaned.split()) >= 4:
            return cleaned
    return ""


def _collect_page_links(
    session: requests.Session,
    source: dict[str, Any],
    audit: dict[str, Any],
) -> list[tuple[str, str]]:
    page_urls = source.get("page_urls", [])
    max_links = max(10, int(source.get("max_page_articles", 30)))
    links: list[tuple[str, str]] = []
    seen: set[str] = set()

    for page_url in page_urls:
        page_result = {"url": page_url, "status": None, "links": 0, "error": ""}
        try:
            response = session.get(page_url, timeout=25, allow_redirects=True)
            page_result["status"] = response.status_code
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as exc:
            page_result["error"] = f"{type(exc).__name__}:{exc}"
            audit["page_attempts"].append(page_result)
            continue

        for anchor in soup.find_all("a", href=True):
            absolute = canonicalize_url(urljoin(response.url, str(anchor.get("href", "")).strip()))
            if not absolute or absolute in seen or not _looks_like_article_url(absolute, source):
                continue
            title = _anchor_title(anchor)
            if not title:
                continue
            seen.add(absolute)
            links.append((absolute, title))
            page_result["links"] += 1
            if len(links) >= max_links:
                break

        audit["page_attempts"].append(page_result)
        if len(links) >= max_links:
            break

    return links


def _collect_rss(
    session: requests.Session,
    source: dict[str, Any],
    audit: dict[str, Any],
) -> list[dict[str, Any]]:
    stories: list[dict[str, Any]] = []
    max_entries = max(20, int(source.get("max_entries", 100)))

    for rss_url in source.get("rss_urls", []):
        attempt = {
            "url": rss_url,
            "status": None,
            "content_type": "",
            "entries": 0,
            "error": "",
            "redirected_to": "",
        }
        try:
            response = session.get(rss_url, timeout=25, allow_redirects=True)
            attempt["status"] = response.status_code
            attempt["content_type"] = response.headers.get("content-type", "")
            attempt["redirected_to"] = response.url
            response.raise_for_status()
        except requests.RequestException as exc:
            attempt["error"] = f"{type(exc).__name__}:{exc}"
            audit["rss_attempts"].append(attempt)
            continue

        parsed = feedparser.parse(response.content)
        entries = list(parsed.entries[:max_entries])
        attempt["entries"] = len(entries)
        if getattr(parsed, "bozo", False):
            attempt["error"] = f"parse:{getattr(parsed, 'bozo_exception', '')}"

        for entry in entries:
            raw_url = str(entry.get("link", "")).strip()
            url = canonicalize_url(raw_url)
            title = bot.clean_text(str(entry.get("title", "")))
            if not url or not title or not _looks_like_article_url(url, source):
                continue

            published = bot.parse_date(entry)
            has_date = any(entry.get(key) for key in ("published_parsed", "updated_parsed", "created_parsed"))
            images = bot.image_candidates_from_entry(entry)
            description = bot.clean_text(entry.get("summary", "") or entry.get("description", ""))

            stories.append({
                "id": bot.story_id(url),
                "title": title,
                "url": url,
                "source": source["name"],
                "published": published,
                "rss_text": description,
                "rss_images": images,
                "date_source": "rss" if has_date else "rss_assumed_now",
            })

        audit["rss_attempts"].append(attempt)

    return stories


def _merge_story(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = dict(existing)
    if incoming.get("date_source") not in {"assumed_now", "rss_assumed_now"}:
        result["published"] = incoming["published"]
        result["date_source"] = incoming.get("date_source", result.get("date_source", ""))
    if len(incoming.get("title", "")) > len(result.get("title", "")):
        result["title"] = incoming["title"]
    if len(incoming.get("rss_text", "")) > len(result.get("rss_text", "")):
        result["rss_text"] = incoming["rss_text"]

    images = list(result.get("rss_images", []))
    for image in incoming.get("rss_images", []):
        if image not in images:
            images.append(image)
    result["rss_images"] = images[:20]
    result["url"] = incoming.get("url") or result.get("url")
    result["id"] = bot.story_id(result["url"])
    return result


def collect_stories(
    config: dict[str, Any],
    processed: set[str],
    cutoff: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    all_stories: dict[str, dict[str, Any]] = {}
    audits: list[dict[str, Any]] = []
    session = _session()

    for source in config.get("feeds", []):
        normalized = dict(source)
        if "rss_urls" not in normalized:
            url = normalized.get("url")
            normalized["rss_urls"] = [url] if url and normalized.get("type", "rss") != "html" else []
        if "page_urls" not in normalized:
            page_urls: list[str] = []
            if normalized.get("fallback_url"):
                page_urls.append(normalized["fallback_url"])
            elif normalized.get("type") == "html" and normalized.get("url"):
                page_urls.append(normalized["url"])
            normalized["page_urls"] = page_urls

        audit: dict[str, Any] = {
            "source": normalized["name"],
            "rss_attempts": [],
            "page_attempts": [],
            "rss_candidates": 0,
            "page_links": 0,
            "page_articles": 0,
            "eligible_new": 0,
            "already_processed": 0,
            "older_than_window": 0,
            "metadata_errors": {},
        }

        rss_stories = _collect_rss(session, normalized, audit)
        audit["rss_candidates"] = len(rss_stories)

        source_map: dict[str, dict[str, Any]] = {}
        for story in rss_stories:
            source_map[story["id"]] = story

        page_links = _collect_page_links(session, normalized, audit)
        audit["page_links"] = len(page_links)

        if page_links:
            workers = min(8, max(2, int(config.get("metadata_workers", 6))))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(_article_metadata, session, url, normalized, title): (url, title)
                    for url, title in page_links
                }
                for future in as_completed(futures):
                    metadata, error = future.result()
                    if error:
                        audit["metadata_errors"][error] = audit["metadata_errors"].get(error, 0) + 1
                        continue
                    if metadata is None:
                        continue
                    audit["page_articles"] += 1
                    sid = metadata["id"]
                    if sid in source_map:
                        source_map[sid] = _merge_story(source_map[sid], metadata)
                    else:
                        source_map[sid] = metadata

        for story in source_map.values():
            sid = story["id"]
            if sid in processed:
                audit["already_processed"] += 1
                continue
            if story["published"] < cutoff:
                audit["older_than_window"] += 1
                continue
            audit["eligible_new"] += 1
            if sid in all_stories:
                all_stories[sid] = _merge_story(all_stories[sid], story)
            else:
                all_stories[sid] = story

        print(
            f"Checked {audit['source']}: "
            f"rss={audit['rss_candidates']}, page_links={audit['page_links']}, "
            f"page_articles={audit['page_articles']}, processed={audit['already_processed']}, "
            f"old={audit['older_than_window']}, eligible_new={audit['eligible_new']}"
        )
        audits.append(audit)

    stories = sorted(all_stories.values(), key=lambda item: item["published"], reverse=True)
    return stories, audits
