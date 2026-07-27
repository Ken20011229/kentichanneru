import hashlib
import logging
import os

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


class PexelsClient:
    BASE_URL = "https://api.pexels.com/v1"

    def __init__(self, api_key: str):
        self.headers = {"Authorization": api_key}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def search(self, query: str, per_page: int = 10,
               orientation: str = "landscape") -> list[dict]:
        resp = requests.get(
            f"{self.BASE_URL}/search",
            headers=self.headers,
            params={"query": query, "per_page": per_page, "orientation": orientation},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("photos", [])

    def download(self, photo: dict, output_dir: str) -> str:
        url = photo["src"]["large2x"]
        fname = hashlib.md5(url.encode()).hexdigest() + ".jpg"
        out_path = os.path.join(output_dir, fname)
        if not os.path.exists(out_path):
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            os.makedirs(output_dir, exist_ok=True)
            with open(out_path, "wb") as f:
                f.write(r.content)
        return out_path

    def fetch_images_for_keywords(self, keywords: list[str], output_dir: str,
                                  total: int = 8,
                                  orientation: str = "landscape") -> list[str]:
        """キーワードごとに交互に1枚ずつ拾って、話題の偏りを避けつつ total 枚集める。"""
        if not keywords:
            return []
        per_keyword = max(2, -(-total // len(keywords)))
        by_keyword: list[list[str]] = []
        for kw in keywords:
            try:
                photos = self.search(kw, per_page=per_keyword, orientation=orientation)
                by_keyword.append([self.download(p, output_dir) for p in photos[:per_keyword]])
            except Exception as e:
                logger.warning(f"Pexels search failed for '{kw}': {e}")
                by_keyword.append([])

        paths, seen = [], set()
        for rank in range(per_keyword):
            for bucket in by_keyword:
                if rank < len(bucket) and bucket[rank] not in seen:
                    seen.add(bucket[rank])
                    paths.append(bucket[rank])
        logger.info(
            f"Pexels: {len(paths)} {orientation} photo(s) for {len(keywords)} keyword(s)"
        )
        return paths[:total]
