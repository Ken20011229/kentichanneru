import logging
import requests
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)
HN_API = "https://hacker-news.firebaseio.com/v0"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _get(url: str) -> dict:
    return requests.get(url, timeout=10).json()


def fetch_top_stories(max_stories: int = 15, min_score: int = 100) -> list:
    try:
        story_ids = _get(f"{HN_API}/topstories.json")
    except Exception as e:
        logger.warning(f"HackerNews top stories fetch failed: {e}")
        return []

    stories = []
    for sid in story_ids[:80]:
        try:
            item = _get(f"{HN_API}/item/{sid}.json")
            if not item or item.get("type") != "story":
                continue
            if item.get("score", 0) < min_score:
                continue
            stories.append(
                {
                    "id": f"hn_{sid}",
                    "title": item.get("title", ""),
                    "summary": item.get("title", ""),
                    "url": item.get("url", f"https://news.ycombinator.com/item?id={sid}"),
                    "score": item.get("score", 0),
                    "source": "Hacker News",
                    "language": "en",
                }
            )
        except Exception as e:
            logger.debug(f"HN item {sid} fetch failed: {e}")
        if len(stories) >= max_stories:
            break

    return stories
