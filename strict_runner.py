from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bot

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


def main() -> None:
    config = bot.load_json(bot.CONFIG_PATH, {})
    state = bot.load_json(bot.STATE_PATH, {"processed": []})
    processed = set(state.get("processed", []))
    run_utc = datetime.now(timezone.utc).isoformat()

    # Generated media is temporary: every run starts with clean folders.
    shutil.rmtree(RUN_OUTPUT_DIR, ignore_errors=True)
    shutil.rmtree(bot.OUTPUT_DIR, ignore_errors=True)
    RUN_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    bot.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    freshness_hours = max(1, int(config.get("news_max_age_hours", 24)))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=freshness_hours)

    all_stories = bot.collect_stories(config, processed)
    stories = [story for story in all_stories if story["published"] >= cutoff]

    skipped_old = len(all_stories) - len(stories)
    if skipped_old:
        print(f"Skipped {skipped_old} article(s) older than {freshness_hours} hours.")

    if not stories:
        append_history(
            {
                "logged_utc": datetime.now(timezone.utc).isoformat(),
                "run_utc": run_utc,
                "status": "no_new_unprocessed_news",
                "maximum_news_age_hours": freshness_hours,
                "generated_count": 0,
            }
        )
        print(f"No unprocessed news from the latest {freshness_hours} hours.")
        return

    configured_limit = config.get("posts_per_run", 1)
    process_all = isinstance(configured_limit, str) and configured_limit.strip().lower() == "all"
    limit = None if process_all else max(1, int(configured_limit))
    print("Post limit: all eligible new articles." if process_all else f"Post limit: {limit}.")

    max_words = min(34, max(20, int(config.get("summary_max_words", 34))))
    generated = 0
    skipped_without_photo = 0

    for story in stories:
        article_text, image_candidates = bot.fetch_article(
            story["url"], story.get("rss_images", [])
        )
        hero, hero_url = bot.download_best_image(image_candidates)

        if hero is None or not hero_url:
            skipped_without_photo += 1
            append_history(
                story_record(
                    story,
                    "skipped_no_valid_photo",
                    run_utc,
                    image_candidates_checked=min(len(image_candidates), 12),
                )
            )
            print(f"Skipped (no valid news photo): {story['title']}")
            continue

        source_text = article_text or story["rss_text"] or story["title"]
        summary = bot.summarize(source_text, max_words=max_words)
        if not summary:
            append_history(story_record(story, "skipped_no_usable_summary", run_utc))
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

        copy_for_download(image_path, caption_path, metadata_path)

        processed.add(story["id"])
        generated += 1
        append_history(
            story_record(
                story,
                "generated",
                run_utc,
                hero_image_url=hero_url,
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

    append_history(
        {
            "logged_utc": datetime.now(timezone.utc).isoformat(),
            "run_utc": run_utc,
            "status": "run_complete",
            "generated_count": generated,
            "skipped_without_photo_count": skipped_without_photo,
            "maximum_news_age_hours": freshness_hours,
            "posts_per_run": "all" if process_all else limit,
        }
    )
    print(
        f"Created {generated} fresh post(s). "
        f"Skipped {skipped_without_photo} fresh article(s) without a valid news photo."
    )


if __name__ == "__main__":
    main()
