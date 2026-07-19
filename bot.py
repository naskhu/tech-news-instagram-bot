from __future__ import annotations

import hashlib
import html
import io
import json
import math
import random
import re
import textwrap
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageStat

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
OUTPUT_DIR = ROOT / "output"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TechNewsInstagramBot/3.0; +https://github.com/naskhu/tech-news-instagram-bot)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
}

STOPWORDS = {
    "a", "about", "after", "again", "against", "all", "also", "am", "an", "and", "any", "are", "as", "at",
    "be", "because", "been", "before", "being", "between", "both", "but", "by", "can", "could", "did", "do",
    "does", "doing", "down", "during", "each", "few", "for", "from", "further", "had", "has", "have", "having",
    "he", "her", "here", "hers", "herself", "him", "himself", "his", "how", "i", "if", "in", "into", "is",
    "it", "its", "itself", "just", "me", "more", "most", "my", "myself", "no", "nor", "not", "now", "of",
    "off", "on", "once", "only", "or", "other", "our", "ours", "ourselves", "out", "over", "own", "same",
    "she", "should", "so", "some", "such", "than", "that", "the", "their", "theirs", "them", "themselves",
    "then", "there", "these", "they", "this", "those", "through", "to", "too", "under", "until", "up", "very",
    "was", "we", "were", "what", "when", "where", "which", "while", "who", "whom", "why", "will", "with",
    "you", "your", "yours", "yourself", "yourselves",
}

SOURCE_THEMES: dict[str, tuple[int, int, int]] = {
    "TechCrunch": (16, 185, 110),
    "The Verge": (226, 42, 166),
    "Ars Technica": (245, 91, 46),
    "MIT Technology Review": (225, 45, 54),
    "Google Blog": (66, 133, 244),
    "Microsoft Blog": (0, 164, 239),
}

BAD_IMAGE_TERMS = {
    "logo", "icon", "avatar", "author", "profile", "favicon", "sprite", "placeholder", "default",
    "blank", "newsletter", "subscribe", "podcast", "advert", "banner", "tracking", "pixel", "badge",
}


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def clean_text(value: str) -> str:
    soup = BeautifulSoup(html.unescape(value or ""), "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def story_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def parse_date(entry: Any) -> datetime:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            return datetime(*value[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def extract_meta_content(soup: BeautifulSoup, candidates: list[tuple[str, str]]) -> list[str]:
    results: list[str] = []
    for attr, value in candidates:
        for tag in soup.find_all("meta", attrs={attr: value}):
            content = str(tag.get("content", "")).strip()
            if content and content not in results:
                results.append(content)
    return results


def image_candidates_from_entry(entry: Any) -> list[str]:
    candidates: list[str] = []
    for key in ("media_content", "media_thumbnail"):
        for item in entry.get(key, []) or []:
            url = str(item.get("url", "")).strip()
            if url and url not in candidates:
                candidates.append(url)
    for enclosure in entry.get("enclosures", []) or []:
        if str(enclosure.get("type", "")).startswith("image/"):
            url = str(enclosure.get("href", "")).strip()
            if url and url not in candidates:
                candidates.append(url)
    return candidates


def fetch_article(url: str, rss_candidates: list[str]) -> tuple[str, list[str]]:
    candidates = list(rss_candidates)
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        meta_images = extract_meta_content(
            soup,
            [
                ("property", "og:image"),
                ("property", "og:image:secure_url"),
                ("name", "twitter:image"),
                ("name", "twitter:image:src"),
            ],
        )
        for image_url in meta_images:
            absolute = urljoin(url, image_url)
            if absolute not in candidates:
                candidates.append(absolute)

        for tag in soup.find_all("img"):
            raw = str(tag.get("src") or tag.get("data-src") or tag.get("data-lazy-src") or "").strip()
            if not raw:
                continue
            width = int(tag.get("width") or 0) if str(tag.get("width") or "").isdigit() else 0
            height = int(tag.get("height") or 0) if str(tag.get("height") or "").isdigit() else 0
            classes = " ".join(tag.get("class", []))
            alt = str(tag.get("alt", ""))
            descriptor = f"{raw} {classes} {alt}".lower()
            if any(term in descriptor for term in BAD_IMAGE_TERMS):
                continue
            if width and height and (width < 600 or height < 320):
                continue
            absolute = urljoin(url, raw)
            if absolute not in candidates:
                candidates.append(absolute)
            if len(candidates) >= 12:
                break

        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
            tag.decompose()
        paragraphs = [clean_text(p.get_text(" ", strip=True)) for p in soup.find_all("p")]
        paragraphs = [p for p in paragraphs if len(p.split()) >= 8]
        return " ".join(paragraphs[:24]), candidates
    except requests.RequestException:
        return "", candidates


def is_valid_news_image(image: Image.Image, url: str) -> bool:
    parsed = urlparse(url)
    descriptor = f"{parsed.path} {parsed.query}".lower()
    if any(term in descriptor for term in BAD_IMAGE_TERMS):
        return False
    if image.width < 700 or image.height < 380:
        return False
    ratio = image.width / max(image.height, 1)
    if ratio < 0.75 or ratio > 2.8:
        return False
    if image.width * image.height < 420_000:
        return False

    sample = ImageOps.fit(image.convert("RGB"), (96, 96), method=Image.Resampling.BILINEAR)
    stats = ImageStat.Stat(sample)
    brightness = sum(stats.mean) / 3
    contrast = sum(stats.stddev) / 3
    if contrast < 10:
        return False
    if brightness > 245 or brightness < 8:
        return False
    return True


def download_best_image(candidates: list[str]) -> tuple[Image.Image | None, str]:
    for url in candidates[:12]:
        try:
            response = requests.get(url, headers=HEADERS, timeout=20)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if content_type and "image" not in content_type:
                continue
            if len(response.content) > 14 * 1024 * 1024:
                continue
            image = Image.open(io.BytesIO(response.content))
            image.load()
            image = ImageOps.exif_transpose(image).convert("RGB")
            if is_valid_news_image(image, url):
                return image, url
        except (requests.RequestException, OSError, ValueError):
            continue
    return None, ""


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)
    return [part.strip() for part in parts if 7 <= len(part.split()) <= 55]


def summarize(text: str, max_words: int) -> str:
    sentences = split_sentences(text)
    if not sentences:
        words = text.split()
        return " ".join(words[:max_words]).rstrip(".,;:") + ("…" if len(words) > max_words else "")

    words = re.findall(r"[A-Za-z][A-Za-z0-9'-]+", text.lower())
    frequencies = Counter(word for word in words if word not in STOPWORDS and len(word) > 2)
    if not frequencies:
        return " ".join(sentences[0].split()[:max_words])

    maximum = max(frequencies.values())
    weights = {word: count / maximum for word, count in frequencies.items()}
    ranked: list[tuple[float, int, str]] = []
    for index, sentence in enumerate(sentences):
        sentence_words = re.findall(r"[A-Za-z][A-Za-z0-9'-]+", sentence.lower())
        meaningful = [word for word in sentence_words if word in weights]
        if meaningful:
            score = sum(weights[word] for word in meaningful) / math.sqrt(len(sentence_words) or 1)
            if index == 0:
                score *= 1.12
            ranked.append((score, index, sentence))

    ranked.sort(reverse=True)
    chosen: list[tuple[int, str]] = []
    count = 0
    for _, index, sentence in ranked:
        length = len(sentence.split())
        if chosen and count + length > max_words:
            continue
        chosen.append((index, sentence))
        count += length
        if count >= max_words * 0.7 or len(chosen) == 2:
            break

    result = " ".join(sentence for _, sentence in sorted(chosen or [(0, sentences[0])]))
    result_words = result.split()
    if len(result_words) > max_words:
        result = " ".join(result_words[:max_words]).rstrip(".,;:") + "…"
    return result


def collect_stories(config: dict[str, Any], processed: set[str]) -> list[dict[str, Any]]:
    stories: list[dict[str, Any]] = []
    for feed in config["feeds"]:
        parsed = feedparser.parse(feed["url"], request_headers=HEADERS)
        for entry in parsed.entries[:12]:
            url = entry.get("link", "").strip()
            title = clean_text(entry.get("title", ""))
            if not url or not title:
                continue
            sid = story_id(url)
            if sid in processed:
                continue
            stories.append({
                "id": sid,
                "title": title,
                "url": url,
                "source": feed["name"],
                "published": parse_date(entry),
                "rss_text": clean_text(entry.get("summary", "") or entry.get("description", "")),
                "rss_images": image_candidates_from_entry(entry),
            })
    stories.sort(key=lambda item: item["published"], reverse=True)
    return stories


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:70] or "tech-news"


def find_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def wrap_by_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        if draw.textlength(test, font=font) <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def fit_text(draw: ImageDraw.ImageDraw, text: str, max_width: int, max_lines: int,
             start_size: int, min_size: int, bold: bool) -> tuple[ImageFont.ImageFont, list[str]]:
    for size in range(start_size, min_size - 1, -2):
        font = find_font(size, bold)
        lines = wrap_by_width(draw, text, font, max_width)
        if len(lines) <= max_lines:
            return font, lines
    font = find_font(min_size, bold)
    lines = wrap_by_width(draw, text, font, max_width)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return font, lines


def theme_for(source: str) -> tuple[int, int, int]:
    return SOURCE_THEMES.get(source, (84, 153, 255))


def generated_background(seed_text: str, accent: tuple[int, int, int]) -> Image.Image:
    seed = int(hashlib.sha256(seed_text.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    image = Image.new("RGB", (1080, 1080), (11, 15, 24))
    draw = ImageDraw.Draw(image, "RGBA")
    for y in range(1080):
        t = y / 1079
        colour = tuple(int(12 * (1 - t) + max(22, channel // 4) * t) for channel in accent)
        draw.line((0, y, 1080, y), fill=colour)
    for _ in range(20):
        x = rng.randint(-120, 1200)
        y = rng.randint(-120, 1200)
        radius = rng.randint(90, 260)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(*accent, rng.randint(6, 18)))
    return image.filter(ImageFilter.GaussianBlur(22))


def create_image(story: dict[str, Any], summary: str, hero: Image.Image | None,
                 config: dict[str, Any], output_path: Path) -> None:
    accent = theme_for(story["source"])

    if hero is not None:
        image = ImageOps.fit(hero, (1080, 1080), method=Image.Resampling.LANCZOS, centering=(0.5, 0.42))
        image = ImageEnhance.Color(image).enhance(0.92)
        image = ImageEnhance.Contrast(image).enhance(1.06)
        image = ImageEnhance.Brightness(image).enhance(0.82)
    else:
        image = generated_background(story["title"], accent)

    canvas = image.convert("RGBA")
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay, "RGBA")
    for y in range(1080):
        if y < 360:
            alpha = int(28 + (y / 360) * 22)
        else:
            alpha = int(min(242, 50 + ((y - 360) / 720) ** 1.35 * 230))
        overlay_draw.line((0, y, 1080, y), fill=(3, 7, 14, alpha))
    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas, "RGBA")

    margin = 72
    content_width = 936

    # Clean source wordmark only; no logo, badge, or Tech News branding.
    source_font = find_font(30, True)
    source_text = story["source"].upper()
    draw.text((margin, 72), source_text, font=source_font, fill=(255, 255, 255, 255))
    source_width = draw.textlength(source_text, font=source_font)
    draw.rounded_rectangle((margin, 118, margin + min(source_width, 250), 124), radius=3, fill=(*accent, 255))

    # Fixed editorial grid keeps every post aligned.
    title_font, title_lines = fit_text(draw, story["title"], content_width, 4, 70, 44, True)
    title_spacing = 10
    title_line_height = title_font.getbbox("Ag")[3] - title_font.getbbox("Ag")[1]
    title_height = len(title_lines) * title_line_height + max(0, len(title_lines) - 1) * title_spacing
    title_y = 625 - title_height
    title_y = max(330, title_y)

    for index, line in enumerate(title_lines):
        y = title_y + index * (title_line_height + title_spacing)
        draw.text((margin + 2, y + 3), line, font=title_font, fill=(0, 0, 0, 145))
        draw.text((margin, y), line, font=title_font, fill=(255, 255, 255, 255))

    divider_y = title_y + title_height + 30
    draw.rounded_rectangle((margin, divider_y, margin + 96, divider_y + 7), radius=4, fill=(*accent, 255))

    summary_font, summary_lines = fit_text(draw, summary, content_width, 3, 31, 25, False)
    summary_line_height = summary_font.getbbox("Ag")[3] - summary_font.getbbox("Ag")[1]
    summary_y = divider_y + 29
    for index, line in enumerate(summary_lines):
        y = summary_y + index * (summary_line_height + 10)
        draw.text((margin, y), line, font=summary_font, fill=(235, 239, 245, 255))

    footer_line_y = 972
    draw.line((margin, footer_line_y, 1008, footer_line_y), fill=(255, 255, 255, 52), width=2)
    footer_y = 1005
    date_font = find_font(22)
    handle_font = find_font(25, True)
    draw.text((margin, footer_y), story["published"].strftime("%d %b %Y"), font=date_font,
              fill=(220, 226, 235, 255), anchor="ls")
    draw.text((1008, footer_y), config.get("instagram_handle", "@naskhu"), font=handle_font,
              fill=(255, 255, 255, 255), anchor="rs")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output_path, "PNG", optimize=True)


def make_caption(story: dict[str, Any], summary: str, config: dict[str, Any]) -> str:
    hashtags = " ".join(config.get("hashtags", []))
    handle = config.get("instagram_handle", "@naskhu")
    return (
        f"{story['title']}\n\n"
        f"{summary}\n\n"
        f"Source: {story['source']}\n"
        f"Read more: {story['url']}\n\n"
        f"Follow {handle} for more tech news.\n\n"
        f"{hashtags}"
    )


def main() -> None:
    config = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {"processed": []})
    processed = set(state.get("processed", []))
    stories = collect_stories(config, processed)

    if not stories:
        print("No new stories found.")
        return

    limit = max(1, int(config.get("posts_per_run", 1)))
    max_words = min(34, max(20, int(config.get("summary_max_words", 34))))
    generated = 0

    for story in stories:
        article_text, image_candidates = fetch_article(story["url"], story.get("rss_images", []))
        source_text = article_text or story["rss_text"] or story["title"]
        summary = summarize(source_text, max_words=max_words)
        if not summary:
            continue

        hero, hero_url = download_best_image(image_candidates)
        date_folder = story["published"].astimezone(timezone.utc).strftime("%Y-%m-%d")
        base_name = slugify(story["title"])
        folder = OUTPUT_DIR / date_folder
        image_path = folder / f"{base_name}.png"
        caption_path = folder / f"{base_name}.txt"
        metadata_path = folder / f"{base_name}.json"

        create_image(story, summary, hero, config, image_path)
        caption_path.write_text(make_caption(story, summary, config), encoding="utf-8")
        metadata = {
            "title": story["title"],
            "summary": summary,
            "source": story["source"],
            "url": story["url"],
            "published_utc": story["published"].isoformat(),
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "hero_image_url": hero_url or None,
            "hero_image_used": hero is not None,
            "image_candidates_checked": min(len(image_candidates), 12),
            "theme_accent_rgb": list(theme_for(story["source"])),
            "image": str(image_path.relative_to(ROOT)),
            "caption": str(caption_path.relative_to(ROOT)),
        }
        metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

        processed.add(story["id"])
        generated += 1
        print(f"Generated: {image_path.relative_to(ROOT)} | hero image: {'yes' if hero else 'fallback'}")
        if generated >= limit:
            break

    state["processed"] = list(processed)[-1000:]
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"Created {generated} post(s).")


if __name__ == "__main__":
    main()
