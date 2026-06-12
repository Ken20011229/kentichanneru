import feedparser
from dataclasses import dataclass
from datetime import datetime
from typing import List


@dataclass
class FeedItem:
    id: str
    title: str
    summary: str
    url: str
    published: datetime
    language: str
    source: str


def fetch_rss(feed_config: dict) -> List[FeedItem]:
    items = []
    for feed in feed_config["feeds"]:
        try:
            parsed = feedparser.parse(feed["url"])
            for entry in parsed.entries[: feed_config["max_items_per_feed"]]:
                published = datetime.now()
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6])
                items.append(
                    FeedItem(
                        id=entry.get("id", entry.get("link", "")),
                        title=entry.get("title", ""),
                        summary=entry.get("summary", "")[:800],
                        url=entry.get("link", ""),
                        published=published,
                        language=feed["language"],
                        source=feed["name"],
                    )
                )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"RSS fetch failed for {feed['name']}: {e}")
    return items
