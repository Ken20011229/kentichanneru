import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_LOG = "data/video_log.json"


def _load(path: str) -> list:
    if Path(path).exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not read video log: {e}")
    return []


def _save(path: str, records: list):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def log_video(video_id: str, metadata: dict, log_file: str = _DEFAULT_LOG):
    """Record a newly uploaded video."""
    records = _load(log_file)
    records.append({
        "video_id":        video_id,
        "shorts_id":       metadata.get("shorts_id"),
        "channel_id":      metadata.get("channel_id"),
        "channel_name":    metadata.get("channel_name"),
        "title":           metadata.get("title"),
        "bgm":             metadata.get("bgm"),
        "thumbnail_style": metadata.get("thumbnail_style"),
        "uploaded_at":     datetime.now(timezone.utc).isoformat(),
        "stats_updated_at": None,
        "views":        None,
        "likes":        None,
        "comments":     None,
        "shorts_views": None,
        "shorts_likes": None,
    })
    _save(log_file, records)
    logger.info(f"Logged video {video_id} to {log_file}")


def update_analytics(analytics_service, log_file: str = _DEFAULT_LOG):
    """Fetch CTR and avg view duration from YouTube Analytics API."""
    from src.analytics.yt_analytics import fetch_video_analytics
    records = _load(log_file)
    if not records:
        return

    video_ids = [r["video_id"] for r in records if r.get("video_id")]
    if not video_ids:
        return

    analytics = fetch_video_analytics(analytics_service, video_ids)
    now = datetime.now(timezone.utc).isoformat()
    for r in records:
        vid = r.get("video_id")
        if vid and vid in analytics:
            a = analytics[vid]
            r.update({
                "ctr":                   a.get("ctr", 0),
                "avg_view_duration_sec": a.get("avg_view_duration_sec", 0),
                "avg_view_percentage":   a.get("avg_view_percentage", 0),
                "watch_time_min":        a.get("watch_time_min", 0),
                "analytics_updated_at":  now,
            })

    _save(log_file, records)
    logger.info(f"Updated analytics for {len(analytics)} videos.")


def update_stats(service, log_file: str = _DEFAULT_LOG):
    """Fetch latest stats from YouTube API for all logged videos."""
    records = _load(log_file)
    if not records:
        logger.info("No videos in log to update.")
        return

    # Collect all video IDs (main + shorts) that need updating
    ids_to_fetch = set()
    for r in records:
        if r.get("video_id"):
            ids_to_fetch.add(r["video_id"])
        if r.get("shorts_id"):
            ids_to_fetch.add(r["shorts_id"])

    if not ids_to_fetch:
        return

    # Fetch in batches of 50 (API limit)
    stats_map = {}
    ids_list = list(ids_to_fetch)
    for i in range(0, len(ids_list), 50):
        batch = ids_list[i:i+50]
        try:
            resp = service.videos().list(
                part="statistics",
                id=",".join(batch),
            ).execute()
            for item in resp.get("items", []):
                s = item.get("statistics", {})
                stats_map[item["id"]] = {
                    "views":    int(s.get("viewCount", 0)),
                    "likes":    int(s.get("likeCount", 0)),
                    "comments": int(s.get("commentCount", 0)),
                }
        except Exception as e:
            logger.warning(f"Stats fetch failed for batch: {e}")

    # Update records
    now = datetime.now(timezone.utc).isoformat()
    for r in records:
        vid = r.get("video_id")
        sid = r.get("shorts_id")
        if vid and vid in stats_map:
            r.update({
                "views":    stats_map[vid]["views"],
                "likes":    stats_map[vid]["likes"],
                "comments": stats_map[vid]["comments"],
                "stats_updated_at": now,
            })
        if sid and sid in stats_map:
            r.update({
                "shorts_views": stats_map[sid]["views"],
                "shorts_likes": stats_map[sid]["likes"],
            })

    _save(log_file, records)
    logger.info(f"Updated stats for {len(stats_map)} videos.")


def get_all(log_file: str = _DEFAULT_LOG) -> list:
    return _load(log_file)
