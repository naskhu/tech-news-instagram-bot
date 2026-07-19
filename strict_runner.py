from __future__ import annotations

import json
from datetime import datetime, timezone

import bot


def main() -> None:
    config = bot.load_json(bot.CONFIG_PATH, {})
    state = bot.load_json(bot.STATE_PATH, {"processed": []})
    processed = set(state.get("processed", []))
    stories = bot.collect_stories(config, processed)

    if not stories:
        print("No new stories found.")
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

        bot.create_image(story, summary, hero, config, image_path)
        caption_path.write_text(bot.make_caption(story, summary, config), encoding="utf-8")

        metadata = {
            "title": story["title"],
            "summary": summary,
            "source": story["source"],
            "url": story["url"],
            "published_utc": story["published"].isoformat(),
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "hero_image_url": hero_url,
            "hero_image_used": True,
            "strict_news_photo_only": True,
            "image_candidates_checked": min(len(image_candidates), 12),
            "theme_accent_rgb": list(bot.theme_for(story["source"])),
            "image": str(image_path.relative_to(bot.ROOT)),
            "caption": str(caption_path.relative_to(bot.ROOT)),
        }
        metadata_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        processed.add(story["id"])
        generated += 1
        print(f"Generated with verified news photo: {image_path.relative_to(bot.ROOT)}")

        if generated >= limit:
            break

    # Only successfully generated stories are marked processed.
    state["processed"] = list(processed)[-1000:]
    bot.STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(
        f"Created {generated} post(s). "
        f"Skipped {skipped_without_photo} article(s) without a valid news photo."
    )


if __name__ == "__main__":
    main()
