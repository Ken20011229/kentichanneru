import logging
import requests
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)
GNEWS_API = "https://gnews.io/api/v4"


_PLACEHOLDER_VALUES = {"", "...", "your_key_here", "YOUR_KEY_HERE"}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=20))
def fetch_gnews(config: dict) -> list:
    if not config.get("enabled"):
        return []
    api_key = config.get("api_key", "")
    if not api_key or api_key in _PLACEHOLDER_VALUES:
        logger.debug("GNews API key not configured — skipping")
        return []
    results = []
    for topic in config.get("topics", ["technology"]):
        try:
            resp = requests.get(
                f"{GNEWS_API}/top-headlines",
                params={
                    "token": api_key,
                    "lang": config.get("language", "ja"),
                    "country": config.get("country", "jp"),
                    "topic": topic,
                    "max": config.get("max_results", 10),
                },
                timeout=15,
            )
            resp.raise_for_status()
            for article in resp.json().get("articles", []):
                results.append(
                    {
                        "id": f"gnews_{article.get('url', '')}",
                        "title": article.get("title", ""),
                        "summary": article.get("description", "")[:800],
                        "url": article.get("url", ""),
                        "source": article.get("source", {}).get("name", "GNews"),
                        "language": config.get("language", "ja"),
                    }
                )
        except Exception as e:
            logger.warning(f"GNews fetch failed for topic '{topic}': {e}")
    return results
