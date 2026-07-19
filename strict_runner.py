from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bot

RUN_OUTPUT_DIR = bot.ROOT / "run-output"


def copy_for_download(*paths: Path) -> None:
    for path in paths:
        relative = path.relative_to(bot.OUTPUT_DIR)
        destination = RUN_OUTPUT_DIR / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def main() -> None:
    config = bot.load_json(bot.CONFIG_PATH, {})
    state = bot.load_json(bot.STATE_PATH, {"processed": []})
    processed = set(state.get("processed", []))

    # The downloadable artifact must contain files from this run only.
    shutil.rmtree(RUN_OUTPUT_DIR, ignore_errors=True)
    RUN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    freshness_hours = max(1, int(config.get("news_max_age_hours", 24)))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=freshness_hours)

    all_stories = bot.collect_stories(config, processed)
    stories = [story for story in all_stories if story["published"] >= cutoff]

    skipped_old = len(all_stories) - len(stories)
    if skipped_old:
        print(f"Skipped {skipped_old} article(s) older than {freshness_hours} hours.")

    if not stories:
        print(f"No unprocessed news from the latest {freshness_hours} hours.")
        return

    limit = max(1, int(config.get("posts_per_run", 1)))
    max_words = min(34, max(20, int(config.get("summary_max_words", 34))))
    generated = 0
    skipped_without_photo = 0

    for story in stories:
        article_text, image_candidates = bot.fetch_article(
            story["url"], story.get("rss_images", [])
        )
        hero, hero_url = bot.download_best_image(image_candidates)

        # Strict mode: never generate a fallback graphic.
        if hero is None or not hero_url:
            skipped_without_photo += 1
            print(f"Skipped (no valid news photo): {story['title']}")
            continue

        source_text = article_text or story["rss_text"] or story["title"]
        summary = bot.summarize(source_text, max_words=max_words)
        if not summary:
            print(f"Skipped (no usable summary): {story['title']}")
            continue

        date_folder = story["published"].astimezone(timezone.utc).strftime("%Y-%m-%d")
        base_name = bot.slugify(story["title"])
        folder = bot.OUTPUT_DIR / date_folder
        image_path = folder / f"{base_name}.png"
        caption_path = folder / f"{base_name}.txt"
        metadata_path = folder / f"{base_name}.json"

        folder.mkdir(parents=True, exist_ok=True)
        bot.create_image(story, summary, hero, config, image_path)
        caption_path.write_text(bot.make_caption(story, summary, config), encoding="utf-8")

        metadata = {
            "title": story["title"],
            "summary": summary,
            "source": story["source"],
            "url": story["url"],
            "published_utc": story["published"].isoformat(),
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "maximum_news_age_hours": freshness_hours,
            "hero_image_url": hero_url,
            "hero_image_used": True,
            "strict_news_photo_only": True,
            "current_run_download_only": True,
            "image_candidates_checked": min(len(image_candidates), 12),
            "theme_accent_rgb": list(bot.theme_for(story["source"])),
            "image": str(image_path.relative_to(bot.ROOT)),
            "caption": str(caption_path.relative_to(bot.ROOT)),
        }
        metadata_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Only these newly generated files are placed in the downloadable artifact.
        copy_for_download(image_path, caption_path, metadata_path)

        processed.add(story["id"])
        generated += 1
        print(f"Generated fresh news post: {image_path.relative_to(bot.ROOT)}")

        if generated >= limit:
            break

    # Only successfully generated stories are marked processed.
    state["processed"] = list(processed)[-1000:]
    bot.STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(
        f"Created {generated} fresh post(s). "
        f"Skipped {skipped_without_photo} fresh article(s) without a valid news photo."
    )


if __name__ == "__main__":
    main()
