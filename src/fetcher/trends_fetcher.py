"""trends_fetcher.py — Japan trending topics via RSS + Google Trends fallback."""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Reliable Japanese news RSS feeds that reflect what's trending
_JP_TRENDING_FEEDS = [
    ("https://news.yahoo.co.jp/rss/topics/top-picks.xml",     "Yahoo!ニュース 主要",  2.5),
    ("https://news.yahoo.co.jp/rss/topics/domestic.xml",      "Yahoo!ニュース 国内",  2.2),
    ("https://news.yahoo.co.jp/rss/topics/world.xml",         "Yahoo!ニュース 国際",  2.0),
    ("https://news.yahoo.co.jp/rss/topics/entertainment.xml", "Yahoo!ニュース エンタメ", 1.8),
    ("https://news.yahoo.co.jp/rss/topics/sports.xml",        "Yahoo!ニュース スポーツ", 1.8),
    ("http://news.livedoor.com/topics/rss/top.xml",           "livedoor ニュース",    1.6),
    ("https://www3.nhk.or.jp/rss/news/cat0.xml",              "NHK NEWS",            2.0),
]


def fetch_trending_jp(max_topics: int = 20) -> list[dict]:
    """
    Fetch trending JP topics from Yahoo News + livedoor RSS.
    These are more reliable than Google Trends API for production use.
    """
    import feedparser
    items = []
    now = datetime.now(timezone.utc).isoformat()

    for url, source_name, base_score in _JP_TRENDING_FEEDS:
        if len(items) >= max_topics:
            break
        try:
            feed = feedparser.parse(url)
            for rank, entry in enumerate(feed.entries[:5], start=1):
                title   = entry.get("title", "").strip()
                summary = entry.get("summary", entry.get("description", "")).strip()
                link    = entry.get("link", url)
                if not title:
                    continue
                # Clean HTML tags from summary
                import re
                summary = re.sub(r"<[^>]+>", "", summary)[:200]
                if not summary:
                    summary = f"{source_name}のトレンドニュース: {title}"
                # Score decays by rank within feed
                score = base_score * (1.0 - rank * 0.08)
                items.append({
                    "id":                f"jp_trend_{hash(title) & 0xFFFFFF:06x}_{now[:10]}",
                    "title":             title,
                    "summary":           summary,
                    "url":               link,
                    "source":            source_name,
                    "language":          "ja",
                    "needs_translation": False,
                    "score":             round(score, 3),
                    "trend_rank":        len(items) + 1,
                })
                if len(items) >= max_topics:
                    break
        except Exception as e:
            logger.debug(f"Trending RSS fetch failed ({source_name}): {e}")

    logger.info(f"JP Trending RSS: fetched {len(items)} topics")
    return items


def fetch_trending_realtime_jp(max_topics: int = 10) -> list[dict]:
    """
    Try Google Trends realtime as a bonus source.
    Falls back silently if unavailable.
    """
    try:
        from pytrends.request import TrendReq
    except ImportError:
        return []

    items = []
    try:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        session = requests.Session()
        try:
            retry = Retry(total=1, backoff_factor=0.3,
                          allowed_methods=frozenset(["GET", "POST"]))
        except TypeError:
            retry = Retry(total=1, backoff_factor=0.3,
                          method_whitelist=frozenset(["GET", "POST"]))
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        pt  = TrendReq(hl="ja-JP", tz=-540, timeout=(8, 20), requests_args={"verify": True})
        now = datetime.now(timezone.utc).isoformat()

        rt = pt.realtime_trending_searches(pn="JP")
        if rt is not None and not rt.empty:
            for rank, (_, row) in enumerate(rt.iterrows(), start=1):
                title   = str(row.get("title", "")).strip()
                snippet = str(row.get("description", "")).strip() or f"リアルタイムトレンド: {title}"
                if not title or title == "nan":
                    continue
                import re
                snippet = re.sub(r"<[^>]+>", "", snippet)[:200]
                items.append({
                    "id":                f"trends_rt_{hash(title) & 0xFFFFFF:06x}_{now[:10]}",
                    "title":             title,
                    "summary":           snippet,
                    "url":               f"https://trends.google.co.jp/trends/explore?q={title}&geo=JP",
                    "source":            "Google Trends RT",
                    "language":          "ja",
                    "needs_translation": False,
                    "score":             max(1.2, 3.5 - rank * 0.2),
                    "trend_rank":        rank,
                })
                if len(items) >= max_topics:
                    break

        if items:
            logger.info(f"Google Trends Realtime JP: {len(items)} topics")

    except Exception as e:
        logger.debug(f"Google Trends realtime unavailable (non-fatal): {e}")

    return items
