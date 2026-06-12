import logging
import praw

logger = logging.getLogger(__name__)

_PLACEHOLDER_VALUES = {"", "...", "your_key_here", "YOUR_KEY_HERE"}


def fetch_reddit(config: dict) -> list:
    if not config.get("enabled"):
        return []
    client_id = config.get("client_id", "")
    client_secret = config.get("client_secret", "")
    if client_id in _PLACEHOLDER_VALUES or client_secret in _PLACEHOLDER_VALUES:
        logger.debug("Reddit credentials not configured — skipping")
        return []
    try:
        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=config["user_agent"],
        )
    except Exception as e:
        logger.warning(f"Reddit client init failed: {e}")
        return []

    results = []
    for sub_name in config.get("subreddits", []):
        try:
            sub = reddit.subreddit(sub_name)
            posts = list(getattr(sub, config.get("sort", "hot"))(limit=config.get("max_posts_per_sub", 5)))
            for post in posts:
                if post.is_self and not post.selftext:
                    continue
                results.append(
                    {
                        "id": f"reddit_{post.id}",
                        "title": post.title,
                        "summary": (post.selftext[:600] if post.selftext else post.title),
                        "url": f"https://reddit.com{post.permalink}",
                        "score": post.score,
                        "upvote_ratio": post.upvote_ratio,
                        "source": f"Reddit r/{sub_name}",
                        "language": "en",
                    }
                )
        except Exception as e:
            logger.warning(f"Reddit r/{sub_name} fetch failed: {e}")
    return results
