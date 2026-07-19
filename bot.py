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
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
OUTPUT_DIR = ROOT / "output"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TechNewsInstagramBot/2.0; +https://github.com/naskhu/tech-news-instagram-bot)",
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

SOURCE_THEMES: dict[str, dict[str, tuple[int, int, int]]] = {
    "TechCrunch": {"accent": (15, 200, 110), "accent2": (4, 120, 75)},
    "The Verge": {"accent": (232, 34, 170), "accent2": (94, 44, 180)},
    "Ars Technica": {"accent": (255, 79, 36), "accent2": (156, 34, 18)},
    "MIT Technology Review": {"accent": (230, 35, 45), "accent2": (120, 15, 20)},
    "Google Blog": {"accent": (66, 133, 244), "accent2": (52, 168, 83)},
    "Microsoft Blog": {"accent": (0, 164, 239), "accent2": (127, 186, 0)},
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


def extract_meta_content(soup: BeautifulSoup, *keys: tuple[str, str]) -> str:
    for attr, value in keys:
        tag = soup.find("meta", attrs={attr: value})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return ""


def fetch_article(url: str) -> tuple[str, str]:
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        image_url = extract_meta_content(
            soup,
            ("property", "og:image"),
            ("property", "og:image:secure_url"),
            ("name", "twitter:image"),
            ("name", "twitter:image:src"),
        )
        if image_url:
            image_url = urljoin(url, image_url)

        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
            tag.decompose()
        paragraphs = [clean_text(p.get_text(" ", strip=True)) for p in soup.find_all("p")]
        paragraphs = [p for p in paragraphs if len(p.split()) >= 8]
        return " ".join(paragraphs[:24]), image_url
    except requests.RequestException:
        return "", ""


def download_image(url: str) -> Image.Image | None:
    if not url:
        return None
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()
        if len(response.content) > 12 * 1024 * 1024:
            return None
        image = Image.open(io.BytesIO(response.content))
        image.load()
        image = ImageOps.exif_transpose(image).convert("RGB")
        if image.width < 500 or image.height < 280:
            return None
        return image
    except (requests.RequestException, OSError, ValueError):
        return None


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
        if not meaningful:
            continue
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
        if count >= max_words * 0.75 or len(chosen) == 2:
            break

    if not chosen:
        chosen = [(0, sentences[0])]
    result = " ".join(sentence for _, sentence in sorted(chosen))
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


def fit_wrapped_text(draw: ImageDraw.ImageDraw, text: str, max_width: int, max_height: int,
                     start_size: int, min_size: int, bold: bool, spacing: int = 12) -> tuple[ImageFont.ImageFont, list[str]]:
    for size in range(start_size, min_size - 1, -2):
        font = find_font(size, bold)
        average = max(draw.textlength("ABCDEFGHIJKLMNOPQRSTUVWXYZ", font=font) / 26, 1)
        width_chars = max(12, int(max_width / average))
        lines = textwrap.wrap(text, width=width_chars, break_long_words=False)
        box = draw.multiline_textbbox((0, 0), "\n".join(lines), font=font, spacing=spacing)
        if box[2] - box[0] <= max_width and box[3] - box[1] <= max_height:
            return font, lines
    font = find_font(min_size, bold)
    return font, textwrap.wrap(text, width=28, break_long_words=False)


def theme_for(source: str) -> dict[str, tuple[int, int, int]]:
    return SOURCE_THEMES.get(source, {"accent": (62, 211, 250), "accent2": (74, 95, 245)})


def generated_background(seed_text: str, accent: tuple[int, int, int]) -> Image.Image:
    seed = int(hashlib.sha256(seed_text.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    image = Image.new("RGB", (1080, 1080), (7, 13, 29))
    draw = ImageDraw.Draw(image, "RGBA")
    for y in range(1080):
        t = y / 1079
        colour = tuple(int(7 * (1 - t) + max(20, channel // 3) * t) for channel in accent)
        draw.line((0, y, 1080, y), fill=colour)
    for _ in range(28):
        x = rng.randint(-100, 1100)
        y = rng.randint(-100, 1100)
        radius = rng.randint(30, 180)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(*accent, rng.randint(8, 25)))
    return image.filter(ImageFilter.GaussianBlur(1.2))


def cover_crop(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.45))


def source_mark(source: str) -> str:
    parts = re.findall(r"[A-Za-z0-9]+", source)
    if len(parts) >= 2:
        return "".join(part[0] for part in parts[:2]).upper()
    return source[:2].upper()


def create_image(story: dict[str, Any], summary: str, hero: Image.Image | None,
                 config: dict[str, Any], output_path: Path) -> None:
    theme = theme_for(story["source"])
    accent = theme["accent"]
    accent2 = theme["accent2"]

    if hero is not None:
        image = cover_crop(hero, (1080, 1080))
        image = ImageEnhance.Color(image).enhance(0.90)
        image = ImageEnhance.Contrast(image).enhance(1.06)
        image = ImageEnhance.Brightness(image).enhance(0.78)
    else:
        image = generated_background(story["title"], accent)

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay, "RGBA")
    for y in range(1080):
        if y < 300:
            alpha = int(35 + y / 300 * 20)
        else:
            alpha = int(min(235, 55 + ((y - 300) / 780) ** 1.25 * 210))
        overlay_draw.line((0, y, 1080, y), fill=(4, 8, 18, alpha))
    overlay_draw.rectangle((0, 0, 1080, 14), fill=(*accent, 255))
    image = Image.alpha_composite(image.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(image, "RGBA")

    # Source badge with a simple monogram rather than copying publisher artwork.
    badge_y = 64
    draw.rounded_rectangle((64, badge_y, 570, badge_y + 82), radius=24, fill=(6, 12, 25, 205), outline=(*accent, 180), width=2)
    draw.ellipse((82, badge_y + 13, 138, badge_y + 69), fill=(*accent, 255))
    initials_font = find_font(22, True)
    initials = source_mark(story["source"])
    ibox = draw.textbbox((0, 0), initials, font=initials_font)
    draw.text((110 - (ibox[2] - ibox[0]) / 2, badge_y + 41 - (ibox[3] - ibox[1]) / 2 - 2), initials,
              font=initials_font, fill=(255, 255, 255, 255))
    source_font = find_font(27, True)
    draw.text((158, badge_y + 23), story["source"].upper(), font=source_font, fill=(255, 255, 255, 255))

    brand_font = find_font(22, True)
    brand = config.get("brand_name", "TECH NEWS DAILY")
    brand_width = draw.textlength(brand, font=brand_font)
    draw.rounded_rectangle((1016 - brand_width - 34, 70, 1016, 132), radius=20, fill=(*accent2, 215))
    draw.text((999 - brand_width, 88), brand, font=brand_font, fill=(255, 255, 255, 255))

    title_font, title_lines = fit_wrapped_text(draw, story["title"], 920, 350, 72, 44, True, spacing=12)
    title_text = "\n".join(title_lines)
    title_box = draw.multiline_textbbox((78, 470), title_text, font=title_font, spacing=12)
    title_y = min(680, 790 - (title_box[3] - title_box[1]))
    draw.multiline_text((78, title_y + 3), title_text, font=title_font, fill=(0, 0, 0, 150), spacing=12)
    draw.multiline_text((76, title_y), title_text, font=title_font, fill=(255, 255, 255, 255), spacing=12)

    divider_y = min(825, title_y + (title_box[3] - title_box[1]) + 28)
    draw.rounded_rectangle((76, divider_y, 260, divider_y + 8), radius=4, fill=(*accent, 255))

    summary_font, summary_lines = fit_wrapped_text(draw, summary, 920, 135, 31, 25, False, spacing=9)
    summary_text = "\n".join(summary_lines)
    draw.multiline_text((76, divider_y + 28), summary_text, font=summary_font, fill=(232, 238, 246, 255), spacing=9)

    footer_y = 1000
    draw.line((76, footer_y - 34, 1004, footer_y - 34), fill=(255, 255, 255, 45), width=2)
    footer_font = find_font(23)
    date_text = story["published"].strftime("%d %b %Y")
    draw.text((76, footer_y), date_text, font=footer_font, fill=(210, 220, 232, 255), anchor="ls")
    handle = config.get("instagram_handle", "@naskhu")
    draw.text((1004, footer_y), handle, font=find_font(25, True), fill=(*accent, 255), anchor="rs")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(output_path, "PNG", optimize=True)


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
    max_words = max(20, int(config.get("summary_max_words", 42)))
    generated = 0

    for story in stories:
        article_text, hero_url = fetch_article(story["url"])
        source_text = article_text or story["rss_text"] or story["title"]
        summary = summarize(source_text, max_words=max_words)
        if not summary:
            continue

        hero = download_image(hero_url)
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
            "theme_accent_rgb": list(theme_for(story["source"])["accent"]),
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
