"""bgm_fetcher.py — Download CC0 BGM from Freesound API."""
import hashlib
import logging
import os

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://freesound.org/apiv2/search/text/"

# BGM mood → Freesound search query
_MOOD_QUERIES: dict[str, str] = {
    "upbeat":    "upbeat background music positive energetic",
    "calm":      "calm ambient relaxing background music",
    "tense":     "tense suspense dramatic background",
    "inspiring": "inspiring uplifting motivational background",
    "neutral":   "gentle background ambient music",
    "sad":       "melancholic emotional ambient music",
    "epic":      "epic cinematic orchestral background",
    "tech":      "electronic technology future ambient",
    "news":      "news broadcast background music professional",
    "science":   "science documentary ambient electronic",
}


class FreesoundBGMFetcher:
    def __init__(self, api_key: str, cache_dir: str = "data/bgm"):
        self.api_key  = api_key
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _search(self, query: str) -> list[dict]:
        resp = requests.get(_SEARCH_URL, params={
            "query":     query,
            "filter":    'license:"Creative Commons 0" duration:[60 TO 360]',
            "sort":      "rating_desc",
            "fields":    "id,name,previews,license,duration",
            "page_size": 10,
            "format":    "json",
            "token":     self.api_key,
        }, timeout=20)
        resp.raise_for_status()
        return resp.json().get("results", [])

    def _download(self, url: str, path: str) -> bool:
        r = requests.get(url, timeout=90)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
        return True

    def fetch_bgm(self, mood: str) -> str | None:
        """Return local path to a BGM file matching the mood. Downloads if not cached."""
        query = _MOOD_QUERIES.get(mood, _MOOD_QUERIES["neutral"])

        # Cache hit: any file for this mood
        cached = [f for f in os.listdir(self.cache_dir)
                  if f.startswith(f"bgm_{mood}_") and f.endswith(".mp3")]
        if cached:
            path = os.path.join(self.cache_dir, cached[0])
            logger.info(f"BGM cache hit: {cached[0]}")
            return path

        try:
            results = self._search(query)
        except Exception as e:
            logger.warning(f"Freesound search failed: {e}")
            return None

        if not results:
            # Retry without CC0 filter (accept Attribution too)
            try:
                resp = requests.get(_SEARCH_URL, params={
                    "query":     query,
                    "filter":    "duration:[60 TO 360]",
                    "sort":      "rating_desc",
                    "fields":    "id,name,previews,license,duration",
                    "page_size": 10,
                    "format":    "json",
                    "token":     self.api_key,
                }, timeout=20)
                resp.raise_for_status()
                results = [r for r in resp.json().get("results", [])
                           if "Noncommercial" not in r.get("license", "")]
            except Exception:
                return None

        for track in results:
            preview_url = track.get("previews", {}).get("preview-hq-mp3")
            if not preview_url:
                continue
            fname = f"bgm_{mood}_{track['id']}.mp3"
            path  = os.path.join(self.cache_dir, fname)
            if os.path.exists(path):
                return path
            try:
                self._download(preview_url, path)
                logger.info(
                    f"BGM downloaded: {fname} | {track.get('name')} "
                    f"({track.get('duration', '?'):.0f}s)"
                )
                return path
            except Exception as e:
                logger.warning(f"BGM download failed for {track['id']}: {e}")

        logger.warning(f"No downloadable BGM found for mood '{mood}'")
        return None
