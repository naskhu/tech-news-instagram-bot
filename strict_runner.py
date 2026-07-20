from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import pillow_avif  # noqa: F401
except ImportError:
    pass

from PIL import Image, ImageDraw

import bot
import feed_sources

RUN_OUTPUT_DIR = bot.ROOT / "run-output"
HISTORY_DIR = bot.ROOT / "history"
HISTORY_PATH = HISTORY_DIR / "news-log.jsonl"
SOURCE_AUDIT_PATH = HISTORY_DIR / "source-audit.json"


def copy_for_download(*paths: Path) -> None:
    for path in paths:
        relative = path.relative_to(bot.OUTPUT_DIR)
        destination = RUN_OUTPUT_DIR / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def append_history(record: dict) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as history_file:
        history_file.write(json.dumps(record, ensure_ascii=False) + "\n")


def story_record(story: dict, status: str, run_utc: str, **extra: object) -> dict:
    record = {
        "logged_utc": datetime.now(timezone.utc).isoformat(),
        "run_utc": run_utc,
        "status": status,
        "story_id": story["id"],
        "title": story["title"],
        "source": story["source"],
        "url": story["url"],
        "published_utc": story["published"].isoformat(),
        "date_source": story.get("date_source", ""),
    }
    record.update(extra)
    return record


def create_fallback_hero(story: dict) -> Image.Image:
    """Create a branded abstract background when a publisher blocks its article photo."""
    width, height = 1200, 675
    accent = bot.theme_for(story["source"])
    image = Image.new("RGB", (width, height), (10, 17, 31))
    draw = ImageDraw.Draw(image)

    for y in range(height):
        blend = y / max(height - 1, 1)
        color = tuple(
            int((1 - blend) * channel * 0.28 + blend * channel * 0.08)
            for channel in accent
        )
        draw.line((0, y, width, y), fill=color)

    muted = tuple(min(255, int(channel * 0.7 + 35)) for channel in accent)
    draw.ellipse((760, -160, 1320, 400), outline=muted, width=18)
    draw.ellipse((850, 40, 1240, 430), outline=accent, width=8)
    draw.rounded_rectangle((70, 90, 560, 500), radius=52, outline=muted, width=10)
    draw.line((0, 565, width, 565), fill=accent, width=12)
    return image


def save_state(state: dict, processed_order: list[str], run_utc: str, generated: int) -> None:
    state["processed"] = list(dict.fromkeys(processed_order))[-10000:]
    state["last_run_utc"] = run_utc
    state["last_run_generated"] = generated
    bot.STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main() -> None:
    config = bot.load_json(bot.CONFIG_PATH, {})
    state = bot.load_json(bot.STATE_PATH, {"processed": []})
    processed_order = list(dict.fromkeys(state.get("processed", [])))
    processed = set(processed_order)
    run_utc = datetime.now(timezone.utc).isoformat()

    shutil.rmtree(RUN_OUTPUT_DIR, ignore_errors=True)
    shutil.rmtree(bot.OUTPUT_DIR, ignore_errors=True)
    RUN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    bot.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    freshness_hours = max(1, int(config.get("news_max_age_hours", 72)))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=freshness_hours)

    stories, source_audit = feed_sources.collect_stories(config, processed, cutoff)

    SOURCE_AUDIT_PATH.write_text(
        json.dumps(
            {
                "run_utc": run_utc,
                "freshness_hours": freshness_hours,
                "cutoff_utc": cutoff.isoformat(),
                "eligible_story_count": len(stories),
                "sources": source_audit,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(
        f"Source audit complete: {len(source_audit)} source(s), "
        f"{len(stories)} eligible unprocessed article(s)."
    )

    if not stories:
        save_state(state, processed_order, run_utc, 0)
        append_history(
            {
                "logged_utc": datetime.now(timezone.utc).isoformat(),
                "run_utc": run_utc,
                "status": "no_new_unprocessed_news",
                "maximum_news_age_hours": freshness_hours,
                "generated_count": 0,
                "source_audit": str(SOURCE_AUDIT_PATH.relative_to(bot.ROOT)),
            }
        )
        print(
            f"No unprocessed news from the latest {freshness_hours} hours. "
            f"See {SOURCE_AUDIT_PATH.relative_to(bot.ROOT)} for per-source reasons."
        )
        return

    configured_limit = config.get("posts_per_run", "all")
    process_all = (
        isinstance(configured_limit, str)
        and configured_limit.strip().lower() == "all"
    )
    limit = None if process_all else max(1, int(configured_limit))
    print(
        "Post limit: all eligible new articles."
        if process_all else f"Post limit: {limit}."
    )

    max_words = min(42, max(20, int(config.get("summary_max_words", 34))))
    generated = 0
    fallback_generated = 0

    for story in stories:
        article_text, image_candidates = bot.fetch_article(
            story["url"], story.get("rss_images", [])
        )
        hero, hero_url = bot.download_best_image(image_candidates)
        used_fallback = hero is None or not hero_url

        if used_fallback:
            hero = create_fallback_hero(story)
            hero_url = ""
            fallback_generated += 1
            print(f"Using branded fallback image: {story['title']}")

        source_text = article_text or story.get("rss_text") or story["title"]
        summary = bot.summarize(source_text, max_words=max_words)
        if not summary:
            summary = bot.summarize(story["title"], max_words=max_words)

        date_folder = story["published"].astimezone(timezone.utc).strftime("%Y-%m-%d")
        base_name = bot.slugify(story["title"])
        folder = bot.OUTPUT_DIR / date_folder
        image_path = folder / f"{base_name}.png"
        caption_path = folder / f"{base_name}.txt"
        metadata_path = folder / f"{base_name}.json"

        folder.mkdir(parents=True, exist_ok=True)
        bot.create_image(story, summary, hero, config, image_path)
        caption_path.write_text(
            bot.make_caption(story, summary, config),
            encoding="utf-8",
        )

        metadata = {
            "title": story["title"],
            "summary": summary,
            "source": story["source"],
            "url": story["url"],
            "published_utc": story["published"].isoformat(),
            "date_source": story.get("date_source", ""),
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "maximum_news_age_hours": freshness_hours,
            "hero_image_url": hero_url,
            "hero_image_used": not used_fallback,
            "fallback_image_used": used_fallback,
            "current_run_download_only": True,
            "image_candidates_checked": min(len(image_candidates), 20),
            "theme_accent_rgb": list(bot.theme_for(story["source"])),
            "image": str(image_path.relative_to(bot.ROOT)),
            "caption": str(caption_path.relative_to(bot.ROOT)),
        }
        metadata_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        copy_for_download(image_path, caption_path, metadata_path)

        if story["id"] not in processed:
            processed.add(story["id"])
            processed_order.append(story["id"])
        generated += 1

        append_history(
            story_record(
                story,
                "generated_fallback" if used_fallback else "generated",
                run_utc,
                hero_image_url=hero_url,
                fallback_image_used=used_fallback,
                image=str(image_path.relative_to(bot.ROOT)),
                caption=str(caption_path.relative_to(bot.ROOT)),
            )
        )
        print(f"Generated fresh news post: {image_path.relative_to(bot.ROOT)}")

        if limit is not None and generated >= limit:
            break

    save_state(state, processed_order, run_utc, generated)

    append_history(
        {
            "logged_utc": datetime.now(timezone.utc).isoformat(),
            "run_utc": run_utc,
            "status": "run_complete",
            "generated_count": generated,
            "fallback_generated_count": fallback_generated,
            "maximum_news_age_hours": freshness_hours,
            "posts_per_run": "all" if process_all else limit,
            "source_audit": str(SOURCE_AUDIT_PATH.relative_to(bot.ROOT)),
        }
    )
    print(
        f"Created {generated} fresh post(s). "
        f"Used branded fallback images for {fallback_generated} article(s)."
    )


if __name__ == "__main__":
    main()
