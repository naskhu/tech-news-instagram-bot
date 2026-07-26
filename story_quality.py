#!/usr/bin/env python3
"""Filter and rank stories so generation favors real tech news growth content."""

from __future__ import annotations

import re
from typing import Any

# Prefer these when breaking ties on freshness.
DEFAULT_PRIORITY_SOURCES = (
    "TechCrunch",
    "The Verge",
    "Ars Technica",
    "Wired",
    "CNET",
    "VentureBeat",
    "BleepingComputer",
    "MIT Technology Review",
    "9to5Mac",
    "9to5Google",
    "Tom's Hardware",
    "Engadget",
    "Reuters Technology",
    "The Next Web",
    "The Information",
    "Google Blog",
    "Microsoft Blog",
    "PCMag News",
)

# Soft-deprioritize high-volume lifestyle/gadget roundup sources.
DEFAULT_LOW_PRIORITY_SOURCES = (
    "Lifehacker",
    "TechRadar",
    "Gizmodo",
)

# Title patterns that rarely grow a tech-news audience on Threads/Instagram.
DEFAULT_SKIP_TITLE_PATTERNS = (
    r"\bwordle\b",
    r"\bquordle\b",
    r"\bspelling bee\b",
    r"\bconnections\b.+\b(hints?|answers?|nyt|today)\b",
    r"\b(hints?|answers?|nyt|today)\b.+\bconnections\b",
    r"\bstrands\b.+\b(hints?|answers?|nyt|today)\b",
    r"\b(hints?|answers?|nyt|today)\b.+\bstrands\b",
    r"\bhints?\b.+\banswers?\b",
    r"\banswers?\b.+\bhints?\b",
    r"\brecipe[s]?\b",
    r"\bninja creami\b",
    r"\bhow to watch\b",
    r"\bfree live streams?\b",
    r"\blive streams?\b.+\bfree\b",
    r"\bonline for free\b",
    r"\bpre-?season friendly\b",
    r"\btour de france\b",
    r"\bshark week\b",
    r"\bwalking dead\b",
    r"\bboxing\b.+\blive\b",
    r"\bnyt\b.+\bgame\b",
    r"\bgame #\d+",
    r"\bcrossword\b",
    r"\bsudoku\b",
)


def _compiled_patterns(config: dict[str, Any] | None = None) -> list[re.Pattern[str]]:
    raw = None
    if isinstance(config, dict):
        raw = config.get("skip_title_patterns")
    patterns = raw if isinstance(raw, list) and raw else list(DEFAULT_SKIP_TITLE_PATTERNS)
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(str(pattern), re.IGNORECASE))
        except re.error:
            continue
    return compiled


def is_low_quality_story(story: dict[str, Any], config: dict[str, Any] | None = None) -> str | None:
    """Return a skip reason if the story should not be generated, else None."""
    title = str(story.get("title") or "").strip()
    if not title:
        return "empty_title"
    for pattern in _compiled_patterns(config):
        if pattern.search(title):
            return f"title_filter:{pattern.pattern}"
    return None


def source_rank(source: str, config: dict[str, Any] | None = None) -> int:
    """Lower is better. Priority sources first, then neutral, then low-priority."""
    priority = DEFAULT_PRIORITY_SOURCES
    low = DEFAULT_LOW_PRIORITY_SOURCES
    if isinstance(config, dict):
        if isinstance(config.get("priority_sources"), list) and config["priority_sources"]:
            priority = tuple(str(item) for item in config["priority_sources"])
        if isinstance(config.get("low_priority_sources"), list) and config["low_priority_sources"]:
            low = tuple(str(item) for item in config["low_priority_sources"])

    name = str(source or "").strip()
    if name in priority:
        return priority.index(name)
    if name in low:
        return 1000 + low.index(name)
    return 500


def filter_and_rank_stories(
    stories: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Drop junk titles and sort by source priority, then newest."""
    kept: list[dict[str, Any]] = []
    skipped = 0
    for story in stories:
        reason = is_low_quality_story(story, config)
        if reason:
            skipped += 1
            continue
        kept.append(story)

    kept.sort(
        key=lambda item: (
            source_rank(str(item.get("source") or ""), config),
            # Newest first within the same priority band.
            -item["published"].timestamp() if item.get("published") is not None else 0,
        )
    )
    return kept, skipped
