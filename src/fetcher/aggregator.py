import logging
from src.fetcher.rss_fetcher import fetch_rss, FeedItem
from src.fetcher.gnews_fetcher import fetch_gnews
from src.fetcher.reddit_fetcher import fetch_reddit
from src.fetcher.hackernews_fetcher import fetch_top_stories

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


def fetch_and_select_item(config: dict, dedup) -> dict | None:
    all_items: list[dict] = []

    if config["sources"]["rss"]["enabled"]:
        for fi in fetch_rss(config["sources"]["rss"]):
            all_items.append(_rss_to_dict(fi))

    if config["sources"]["gnews"]["enabled"]:
        for article in fetch_gnews(config["sources"]["gnews"]):
            all_items.append({**article, "needs_translation": article.get("language") != "ja", "score": 1.2})

    if config["sources"]["reddit"]["enabled"]:
        for post in fetch_reddit(config["sources"]["reddit"]):
            hn_score = 1.0 + post.get("upvote_ratio", 0.5)
            all_items.append({**post, "needs_translation": True, "score": hn_score})

    if config["sources"]["hackernews"]["enabled"]:
        hn_cfg = config["sources"]["hackernews"]
        for story in fetch_top_stories(
            max_stories=hn_cfg.get("max_stories", 15),
            min_score=hn_cfg.get("min_score", 100),
        ):
            normalized = min(story.get("score", 100) / 500.0, 2.0)
            all_items.append({**story, "needs_translation": True, "score": normalized})

    logger.info(f"Fetched {len(all_items)} total items from all sources")

    unseen = [i for i in all_items if not dedup.is_seen(i["id"])]
    if not unseen:
        logger.info("All fetched items have already been processed")
        return None

    unseen.sort(key=lambda x: x.get("score", 0), reverse=True)
    selected = unseen[0]
    logger.info(f"Selected item: '{selected['title']}' from {selected['source']}")
    return selected
