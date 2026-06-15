import logging
from src.fetcher.rss_fetcher import fetch_rss, FeedItem
from src.fetcher.gnews_fetcher import fetch_gnews
from src.fetcher.reddit_fetcher import fetch_reddit
from src.fetcher.hackernews_fetcher import fetch_top_stories
from src.fetcher.trends_fetcher import fetch_trending_jp, fetch_trending_realtime_jp

logger = logging.getLogger(__name__)


def _rss_to_dict(item: FeedItem) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "summary": item.summary,
        "url": item.url,
        "source": item.source,
        "language": item.language,
        "needs_translation": item.language != "ja",
        "score": 1.0,
    }


def _fetch_channel_items(config: dict, channel: dict) -> list[dict]:
    """Fetch items from channel-specific sources."""
    items: list[dict] = []

    # Channel RSS feeds
    channel_feeds = channel.get("rss_feeds", [])
    if channel_feeds:
        try:
            rss_cfg = {
                "enabled": True,
                "feeds": channel_feeds,
                "max_items_per_feed": config["sources"]["rss"].get("max_items_per_feed", 10),
            }
            for fi in fetch_rss(rss_cfg):
                items.append(_rss_to_dict(fi))
        except Exception as e:
            logger.warning(f"Channel RSS fetch error: {e}")

    # Channel subreddits
    channel_subs = channel.get("subreddits", [])
    if channel_subs and config["sources"]["reddit"]["enabled"]:
        try:
            reddit_cfg = {
                **config["sources"]["reddit"],
                "subreddits": channel_subs,
            }
            for post in fetch_reddit(reddit_cfg):
                score = 1.0 + post.get("upvote_ratio", 0.5)
                items.append({**post, "needs_translation": True, "score": score})
        except Exception as e:
            logger.warning(f"Channel Reddit fetch error: {e}")

    # HackerNews (if channel opts in)
    if channel.get("use_hackernews", False) and config["sources"]["hackernews"]["enabled"]:
        try:
            hn_cfg = config["sources"]["hackernews"]
            for story in fetch_top_stories(
                max_stories=hn_cfg.get("max_stories", 15),
                min_score=hn_cfg.get("min_score", 100),
            ):
                normalized = min(story.get("score", 100) / 500.0, 2.0)
                items.append({**story, "needs_translation": True, "score": normalized})
        except Exception as e:
            logger.warning(f"Channel HackerNews fetch error: {e}")

    return items


def _fetch_global_items(config: dict) -> list[dict]:
    """Fetch items from global (fallback) sources."""
    items: list[dict] = []

    if config["sources"]["rss"]["enabled"]:
        for fi in fetch_rss(config["sources"]["rss"]):
            items.append(_rss_to_dict(fi))

    if config["sources"]["gnews"]["enabled"]:
        for article in fetch_gnews(config["sources"]["gnews"]):
            items.append({**article, "needs_translation": article.get("language") != "ja", "score": 1.2})

    if config["sources"]["reddit"]["enabled"]:
        for post in fetch_reddit(config["sources"]["reddit"]):
            score = 1.0 + post.get("upvote_ratio", 0.5)
            items.append({**post, "needs_translation": True, "score": score})

    if config["sources"]["hackernews"]["enabled"]:
        hn_cfg = config["sources"]["hackernews"]
        for story in fetch_top_stories(
            max_stories=hn_cfg.get("max_stories", 15),
            min_score=hn_cfg.get("min_score", 100),
        ):
            normalized = min(story.get("score", 100) / 500.0, 2.0)
            items.append({**story, "needs_translation": True, "score": normalized})

    # Google Trends JP (always enabled as supplemental source)
    trends_cfg = config.get("sources", {}).get("google_trends", {})
    if trends_cfg.get("enabled", True):
        max_t = trends_cfg.get("max_topics", 20)
        for trend in fetch_trending_jp(max_topics=max_t):
            items.append(trend)
        for trend in fetch_trending_realtime_jp(max_topics=10):
            items.append(trend)

    return items


def fetch_and_select_item(config: dict, dedup, channel: dict = None) -> dict | None:
    all_items: list[dict] = []

    if channel:
        all_items = _fetch_channel_items(config, channel)
        if not all_items:
            logger.warning(f"No items from channel '{channel.get('id')}' sources — falling back to global")

    if not all_items:
        all_items = _fetch_global_items(config)

    logger.info(f"Fetched {len(all_items)} total items from all sources")

    unseen = [i for i in all_items if not dedup.is_seen(i["id"])]
    if not unseen:
        logger.info("All fetched items have already been processed")
        return None

    unseen.sort(key=lambda x: x.get("score", 0), reverse=True)
    selected = unseen[0]
    logger.info(f"Selected item: '{selected['title']}' from {selected['source']}")
    return selected
