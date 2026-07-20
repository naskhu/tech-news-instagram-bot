"""Runtime registrations loaded automatically by Python."""

try:
    import pillow_avif  # noqa: F401
except ImportError:
    pass

try:
    import bot
    import feed_sources

    bot.collect_stories = feed_sources.collect_rss_stories
except Exception as exc:
    print(f"Runtime extension warning: {exc}")
