"""competitor_analyzer.py — Find top-performing competitor videos per genre.

Uses YouTube Data API search.list (cost: 100 quota units/call) to find
high-view Japanese videos published recently in each channel's genre, then
extracts title patterns to feed into prompt_hints.json as extra context for
script/title generation. Complements the channel's own historical CTR data
(from evolver.py), which is sparse until the channel accumulates videos.
"""
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_RESULTS_PER_GENRE = 15
_RAW_FETCH_COUNT = 50  # search cost is flat per call regardless of maxResults, so fetch generously
_LOOKBACK_DAYS = 21
_INSIGHTS_FILE = "data/competitor_insights.json"

# Matches hiragana, katakana, or kanji — used to filter out non-Japanese
# results, since regionCode/relevanceLanguage are relevance hints only and
# order=viewCount still surfaces globally-viral foreign videos.
_JAPANESE_RE = re.compile(r"[぀-ヿ一-鿿]")


def fetch_top_competitor_videos(
    youtube_service, query: str, category_id: str = None, max_results: int = _RESULTS_PER_GENRE
) -> list[dict]:
    """Search YouTube for the top-viewed Japanese-titled videos matching query in the last _LOOKBACK_DAYS days."""
    published_after = (datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kwargs = dict(
        part="snippet",
        q=query,
        type="video",
        order="viewCount",
        regionCode="JP",
        relevanceLanguage="ja",
        publishedAfter=published_after,
        maxResults=_RAW_FETCH_COUNT,
    )
    if category_id:
        kwargs["videoCategoryId"] = category_id

    try:
        resp = youtube_service.search().list(**kwargs).execute()
    except Exception as e:
        logger.warning(f"Competitor search failed for '{query}': {e}")
        return []

    items = [it for it in resp.get("items", []) if _JAPANESE_RE.search(it["snippet"]["title"])]
    video_ids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
    if not video_ids:
        return []

    try:
        stats_resp = youtube_service.videos().list(part="statistics", id=",".join(video_ids)).execute()
        views_map = {v["id"]: int(v["statistics"].get("viewCount", 0)) for v in stats_resp.get("items", [])}
    except Exception as e:
        logger.warning(f"Competitor stats fetch failed: {e}")
        views_map = {}

    results = []
    for it in items:
        vid = it.get("id", {}).get("videoId")
        if not vid:
            continue
        results.append({
            "video_id": vid,
            "title":    it["snippet"]["title"],
            "channel":  it["snippet"]["channelTitle"],
            "views":    views_map.get(vid, 0),
        })
    results.sort(key=lambda r: -r["views"])
    return results[:max_results]


def analyze_all_genres(youtube_service, channels: list[dict]) -> dict:
    """Run a competitor search for every configured channel genre.

    Returns {channel_id: {top_titles, top_bracket_patterns, avg_views, sample_size}}.
    """
    genre_insights = {}
    for ch in channels:
        query = ch.get("name", ch["id"])
        videos = fetch_top_competitor_videos(youtube_service, query, ch.get("youtube_category_id"))
        if not videos:
            continue

        bracket_counts: Counter = Counter()
        for v in videos:
            m = re.search(r"[【\[](.+?)[】\]]", v["title"])
            if m:
                bracket_counts[m.group(1)] += v["views"]

        genre_insights[ch["id"]] = {
            "top_titles":           [v["title"] for v in videos[:8]],
            "top_bracket_patterns": [k for k, _ in bracket_counts.most_common(5)],
            "avg_views":            round(sum(v["views"] for v in videos) / len(videos)),
            "sample_size":          len(videos),
        }
        logger.info(
            f"[{ch['id']}] competitor avg_views={genre_insights[ch['id']]['avg_views']} "
            f"top_patterns={genre_insights[ch['id']]['top_bracket_patterns']}"
        )

    return genre_insights


def run(youtube_service, channels: list[dict], out_file: str = _INSIGHTS_FILE) -> dict:
    """Analyze all genres and write results to out_file. Returns the insights dict."""
    import json
    from pathlib import Path

    insights = analyze_all_genres(youtube_service, channels)
    insights["updated_at"] = datetime.now(timezone.utc).isoformat()

    Path(out_file).parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(insights, f, ensure_ascii=False, indent=2)

    logger.info(f"Competitor insights written for {len(insights) - 1} genres -> {out_file}")
    return insights
