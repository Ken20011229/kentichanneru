"""trends_fetcher.py — Japan trending topics via RSS + Google Trends fallback."""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Reliable Japanese news/trend RSS feeds — broad genre coverage
_JP_TRENDING_FEEDS = [
    # ── 総合ニュース ────────────────────────────────────────────────
    ("https://news.yahoo.co.jp/rss/topics/top-picks.xml",     "Yahoo!ニュース 主要",       2.5),
    ("https://news.yahoo.co.jp/rss/topics/domestic.xml",      "Yahoo!ニュース 国内",       2.2),
    ("https://news.yahoo.co.jp/rss/topics/world.xml",         "Yahoo!ニュース 国際",       2.0),
    ("https://www3.nhk.or.jp/rss/news/cat0.xml",              "NHK NEWS",                 2.0),
    ("http://news.livedoor.com/topics/rss/top.xml",           "livedoor ニュース",         1.7),
    # ── エンタメ・芸能 ─────────────────────────────────────────────
    ("https://news.yahoo.co.jp/rss/topics/entertainment.xml", "Yahoo!ニュース エンタメ",   2.0),
    ("https://natalie.mu/music/feed/news",                    "音楽ナタリー",              1.8),
    ("https://natalie.mu/comic/feed/news",                    "コミックナタリー",          1.7),
    ("https://natalie.mu/eiga/feed/news",                     "映画ナタリー",              1.6),
    # ── スポーツ ────────────────────────────────────────────────────
    ("https://news.yahoo.co.jp/rss/topics/sports.xml",        "Yahoo!ニュース スポーツ",   2.0),
    # ── テクノロジー・IT ────────────────────────────────────────────
    ("https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml",     "ITmedia NEWS",             1.9),
    ("https://gigazine.net/news/rss_2.0/",                    "GIGAZINE",                 1.8),
    ("https://japan.cnet.com/rss/index.rdf",                  "CNET Japan",               1.7),
    ("https://ascii.jp/rss.xml",                              "ASCII.jp",                 1.6),
    # ── 科学・医療・健康 ────────────────────────────────────────────
    ("https://news.yahoo.co.jp/rss/topics/science.xml",       "Yahoo!ニュース 科学",       1.8),
    ("https://news.mynavi.jp/rss/index.rss",                  "Mynavi News",              1.6),
    # ── 経済・ビジネス ──────────────────────────────────────────────
    ("https://news.yahoo.co.jp/rss/topics/business.xml",      "Yahoo!ニュース 経済",       1.9),
    ("https://toyokeizai.net/list/feed/rss",                  "東洋経済オンライン",        1.8),
    # ── 映画 ────────────────────────────────────────────────────────
    ("https://eiga.com/rss/news.rss",                         "映画.com",                 1.8),
    ("https://www.cinemacafe.net/sys/feed/",                  "シネマカフェ",              1.6),
    # ── アニメ・マンガ ──────────────────────────────────────────────
    ("https://animeanime.jp/rss/index.rdf",                   "アニメ!アニメ!",            1.8),
    # ── ゲーム ──────────────────────────────────────────────────────
    ("https://www.4gamer.net/games/rss/4gamer_1024.rdf",      "4Gamer.net",               1.8),
    ("https://game.watch.impress.co.jp/data/rss/1.0/gmw/feed.rdf", "Game Watch",          1.7),
    # ── 旅行 ────────────────────────────────────────────────────────
    ("https://tabizine.jp/feed/",                             "TABIZINE",                 1.6),
    # ── 教育・学習 ──────────────────────────────────────────────────
    ("https://resemom.jp/feed/",                              "ReseMom",                  1.6),
]


def fetch_trending_jp(max_topics: int = 80) -> list[dict]:
    """
    Fetch trending JP topics from diverse genre RSS feeds.
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
