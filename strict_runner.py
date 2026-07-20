from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw

import bot
import direct_sources

RUN_OUTPUT_DIR = bot.ROOT / "run-output"
HISTORY_DIR = bot.ROOT / "history"
HISTORY_PATH = HISTORY_DIR / "news-log.jsonl"


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


def main() -> None:
    config = bot.load_json(bot.CONFIG_PATH, {})
    state = bot.load_json(bot.STATE_PATH, {"processed": []})
    processed = set(state.get("processed", []))
    run_utc = datetime.now(timezone.utc).isoformat()

    shutil.rmtree(RUN_OUTPUT_DIR, ignore_errors=True)
    shutil.rmtree(bot.OUTPUT_DIR, ignore_errors=True)
    RUN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    bot.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    freshness_hours = max(1, int(config.get("news_max_age_hours", 24)))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=freshness_hours)

    rss_config = dict(config)
    rss_config["feeds"] = [
        feed for feed in config.get("feeds", [])
        if feed.get("type", "rss") == "rss"
    ]
    rss_stories = bot.collect_stories(rss_config, processed)
    direct_stories = direct_sources.collect_direct_stories(config, processed)

    unique_stories: dict[str, dict] = {}
    for story in rss_stories + direct_stories:
        unique_stories.setdefault(story["id"], story)

    all_stories = sorted(
        unique_stories.values(),
        key=lambda item: item["published"],
        reverse=True,
    )
    stories = [story for story in all_stories if story["published"] >= cutoff]

    print(
        f"Source check complete: {len(rss_stories)} RSS candidate(s), "
        f"{len(direct_stories)} direct-page candidate(s), "
        f"{len(stories)} eligible new article(s)."
    )

    skipped_old = len(all_stories) - len(stories)
    if skipped_old:
        print(f"Skipped {skipped_old} article(s) older than {freshness_hours} hours.")

    if not stories:
        append_history({
            "logged_utc": datetime.now(timezone.utc).isoformat(),
            "run_utc": run_utc,
            "status": "no_new_unprocessed_news",
            "maximum_news_age_hours": freshness_hours,
            "generated_count": 0,
        })
        print(f"No unprocessed news from the latest {freshness_hours} hours.")
        return

    configured_limit = config.get("posts_per_run", 1)
    process_all = (
        isinstance(configured_limit, str)
        and configured_limit.strip().lower() == "all"
    )
    limit = None if process_all else max(1, int(configured_limit))
    print(
        "Post limit: all eligible new articles."
        if process_all else f"Post limit: {limit}."
    )

    max_words = min(34, max(20, int(config.get("summary_max_words", 34))))
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
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "maximum_news_age_hours": freshness_hours,
            "hero_image_url": hero_url,
            "hero_image_used": not used_fallback,
            "fallback_image_used": used_fallback,
            "strict_news_photo_only": False,
            "current_run_download_only": True,
            "image_candidates_checked": min(len(image_candidates), 12),
            "theme_accent_rgb": list(bot.theme_for(story["source"])),
            "image": str(image_path.relative_to(bot.ROOT)),
            "caption": str(caption_path.relative_to(bot.ROOT)),
        }
        metadata_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        copy_for_download(image_path, caption_path, metadata_path)
        processed.add(story["id"])
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

    state["processed"] = list(processed)[-5000:]
    state["last_run_utc"] = run_utc
    state["last_run_generated"] = generated
    bot.STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")

    append_history({
        "logged_utc": datetime.now(timezone.utc).isoformat(),
        "run_utc": run_utc,
        "status": "run_complete",
        "generated_count": generated,
        "fallback_generated_count": fallback_generated,
        "maximum_news_age_hours": freshness_hours,
        "posts_per_run": "all" if process_all else limit,
    })
    print(
        f"Created {generated} fresh post(s). "
        f"Used branded fallback images for {fallback_generated} article(s)."
    )


if __name__ == "__main__":
    main()
