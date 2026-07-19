from __future__ import annotations

import hashlib
import html
import json
import math
import random
import re
import textwrap
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
OUTPUT_DIR = ROOT / "output"

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
    "you", "your", "yours", "yourself", "yourselves"
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TechNewsInstagramBot/1.0; +https://github.com/naskhu/tech-news-instagram-bot)"
}


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def clean_text(value: str) -> str:
    soup = BeautifulSoup(html.unescape(value or ""), "html.parser")
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def story_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def parse_date(entry: Any) -> datetime:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            return datetime(*value[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def fetch_article_text(url: str) -> str:
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        paragraphs = [clean_text(p.get_text(" ", strip=True)) for p in soup.find_all("p")]
        paragraphs = [p for p in paragraphs if len(p.split()) >= 8]
        return " ".join(paragraphs[:20])
    except requests.RequestException:
        return ""


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)
    return [p.strip() for p in parts if 7 <= len(p.split()) <= 55]


def summarize(text: str, max_words: int) -> str:
    sentences = split_sentences(text)
    if not sentences:
        words = text.split()
        return " ".join(words[:max_words]).rstrip(".,;:") + ("…" if len(words) > max_words else "")

    words = re.findall(r"[A-Za-z][A-Za-z0-9'-]+", text.lower())
    frequencies = Counter(w for w in words if w not in STOPWORDS and len(w) > 2)
    if not frequencies:
        return " ".join(sentences[0].split()[:max_words])

    max_frequency = max(frequencies.values())
    weights = {word: count / max_frequency for word, count in frequencies.items()}
    ranked: list[tuple[float, int, str]] = []

    for index, sentence in enumerate(sentences):
        sentence_words = re.findall(r"[A-Za-z][A-Za-z0-9'-]+", sentence.lower())
        meaningful = [w for w in sentence_words if w in weights]
        if not meaningful:
            continue
        score = sum(weights[w] for w in meaningful) / math.sqrt(len(sentence_words) or 1)
        if index == 0:
            score *= 1.12
        ranked.append((score, index, sentence))

    ranked.sort(reverse=True)
    chosen: list[tuple[int, str]] = []
    word_count = 0
    for _, index, sentence in ranked:
        sentence_len = len(sentence.split())
        if chosen and word_count + sentence_len > max_words:
            continue
        chosen.append((index, sentence))
        word_count += sentence_len
        if word_count >= max_words * 0.75 or len(chosen) == 2:
            break

    if not chosen:
        chosen = [(0, sentences[0])]

    summary = " ".join(sentence for _, sentence in sorted(chosen))
    words = summary.split()
    if len(words) > max_words:
        summary = " ".join(words[:max_words]).rstrip(".,;:") + "…"
    return summary


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
            rss_text = clean_text(entry.get("summary", "") or entry.get("description", ""))
            stories.append({
                "id": sid,
                "title": title,
                "url": url,
                "source": feed["name"],
                "published": parse_date(entry),
                "rss_text": rss_text,
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
                     start_size: int, min_size: int, bold: bool, spacing: int = 14) -> tuple[ImageFont.ImageFont, list[str]]:
    for size in range(start_size, min_size - 1, -2):
        font = find_font(size, bold=bold)
        avg_char_width = max(draw.textlength("ABCDEFGHIJKLMNOPQRSTUVWXYZ", font=font) / 26, 1)
        width_chars = max(12, int(max_width / avg_char_width))
        lines = textwrap.wrap(text, width=width_chars, break_long_words=False)
        bbox = draw.multiline_textbbox((0, 0), "\n".join(lines), font=font, spacing=spacing)
        if bbox[2] - bbox[0] <= max_width and bbox[3] - bbox[1] <= max_height:
            return font, lines
    font = find_font(min_size, bold=bold)
    return font, textwrap.wrap(text, width=28, break_long_words=False)


def generate_background(seed_text: str) -> Image.Image:
    seed = int(hashlib.sha256(seed_text.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    image = Image.new("RGB", (1080, 1080))
    pixels = image.load()
    base_a = (8, 18, 40)
    base_b = (18, 55, 88)
    for y in range(1080):
        t = y / 1079
        for x in range(1080):
            radial = max(0.0, 1.0 - math.hypot(x - 820, y - 220) / 900)
            pixels[x, y] = tuple(
                int(base_a[i] * (1 - t) + base_b[i] * t + radial * (26 if i == 2 else 10))
                for i in range(3)
            )

    draw = ImageDraw.Draw(image, "RGBA")
    for _ in range(26):
        x1, y1 = rng.randint(-100, 1180), rng.randint(-100, 1180)
        if rng.random() < 0.5:
            x2, y2 = x1 + rng.randint(80, 320), y1
        else:
            x2, y2 = x1, y1 + rng.randint(80, 320)
        draw.line((x1, y1, x2, y2), fill=(75, 210, 255, 28), width=rng.randint(2, 5))
        draw.ellipse((x2 - 6, y2 - 6, x2 + 6, y2 + 6), fill=(105, 225, 255, 75))

    draw.ellipse((720, -130, 1160, 310), fill=(61, 178, 255, 35))
    draw.ellipse((-180, 730, 280, 1190), fill=(95, 80, 255, 25))
    return image


def create_image(story: dict[str, Any], summary: str, config: dict[str, Any], output_path: Path) -> None:
    image = generate_background(story["title"])
    draw = ImageDraw.Draw(image, "RGBA")
    margin = 72

    draw.rounded_rectangle((48, 48, 1032, 1032), radius=38, fill=(5, 12, 28, 188), outline=(105, 220, 255, 90), width=2)
    draw.rounded_rectangle((72, 72, 430, 132), radius=22, fill=(55, 205, 245, 220))

    brand_font = find_font(25, bold=True)
    draw.text((96, 89), config["brand_name"], font=brand_font, fill=(5, 18, 35, 255))

    source_font = find_font(24, bold=True)
    source_label = story["source"].upper()
    draw.text((76, 177), source_label, font=source_font, fill=(90, 220, 255, 255))

    title_font, title_lines = fit_wrapped_text(draw, story["title"], 900, 330, 72, 44, True, spacing=12)
    title_text = "\n".join(title_lines)
    draw.multiline_text((76, 220), title_text, font=title_font, fill=(255, 255, 255, 255), spacing=12)
    title_box = draw.multiline_textbbox((76, 220), title_text, font=title_font, spacing=12)

    divider_y = min(title_box[3] + 42, 620)
    draw.rounded_rectangle((76, divider_y, 275, divider_y + 7), radius=4, fill=(63, 211, 250, 255))

    summary_y = divider_y + 38
    summary_font, summary_lines = fit_wrapped_text(draw, summary, 900, 260, 38, 28, False, spacing=12)
    summary_text = "\n".join(summary_lines)
    draw.multiline_text((76, summary_y), summary_text, font=summary_font, fill=(218, 230, 243, 255), spacing=12)

    footer_y = 940
    draw.line((76, footer_y - 24, 1004, footer_y - 24), fill=(255, 255, 255, 38), width=2)
    footer_font = find_font(23)
    date_text = story["published"].strftime("%d %b %Y")
    draw.text((76, footer_y), date_text, font=footer_font, fill=(175, 194, 213, 255))
    handle = config.get("instagram_handle", "")
    handle_width = draw.textlength(handle, font=footer_font)
    draw.text((1004 - handle_width, footer_y), handle, font=footer_font, fill=(175, 194, 213, 255))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, "PNG", optimize=True)


def make_caption(story: dict[str, Any], summary: str, config: dict[str, Any]) -> str:
    hashtags = " ".join(config.get("hashtags", []))
    return (
        f"{story['title']}\n\n"
        f"{summary}\n\n"
        f"Source: {story['source']}\n"
        f"Read more: {story['url']}\n\n"
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
        article_text = fetch_article_text(story["url"])
        source_text = article_text or story["rss_text"] or story["title"]
        summary = summarize(source_text, max_words=max_words)
        if not summary:
            continue

        date_folder = story["published"].astimezone(timezone.utc).strftime("%Y-%m-%d")
        base_name = slugify(story["title"])
        folder = OUTPUT_DIR / date_folder
        image_path = folder / f"{base_name}.png"
        caption_path = folder / f"{base_name}.txt"
        metadata_path = folder / f"{base_name}.json"

        create_image(story, summary, config, image_path)
        caption_path.write_text(make_caption(story, summary, config), encoding="utf-8")
        metadata = {
            "title": story["title"],
            "summary": summary,
            "source": story["source"],
            "url": story["url"],
            "published_utc": story["published"].isoformat(),
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "image": str(image_path.relative_to(ROOT)),
            "caption": str(caption_path.relative_to(ROOT)),
        }
        metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

        processed.add(story["id"])
        generated += 1
        print(f"Generated: {image_path.relative_to(ROOT)}")
        if generated >= limit:
            break

    state["processed"] = list(processed)[-1000:]
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"Created {generated} post(s).")


if __name__ == "__main__":
    main()
