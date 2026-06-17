"""
Fetch CTR and average view duration from YouTube Analytics API.
Requires yt-analytics.readonly scope.
"""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def build_analytics_service(credentials):
    from googleapiclient.discovery import build
    return build("youtubeAnalytics", "v2", credentials=credentials)


def fetch_channel_ctr(analytics_service, days_back: int = 90) -> float:
    """
    Returns channel-wide impressionClickThroughRate.
    impressionClickThroughRate is not available per-video, only at channel level.
    """
    end_date   = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    start_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    try:
        resp = analytics_service.reports().query(
            ids="channel==MINE",
            startDate=start_date,
            endDate=end_date,
            metrics="impressions,impressionClickThroughRate",
        ).execute()
        rows = resp.get("rows", [])
        if rows:
            return float(rows[0][1])
    except Exception as e:
        logger.warning(f"Channel CTR fetch failed: {e}")
    return 0.0


def fetch_video_analytics(analytics_service, video_ids: list[str], days_back: int = 90) -> dict:
    """
    Returns {video_id: {ctr, avg_view_duration_sec, views, watch_time_min}} for each id.
    Analytics data has a 2-3 day delay so we look back 90 days.
    """
    if not video_ids:
        return {}

    end_date   = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    start_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")

    # Fetch channel-wide CTR (not available per-video via Analytics API)
    channel_ctr = fetch_channel_ctr(analytics_service, days_back)

    results = {}
    # Analytics API accepts comma-separated video filter, max ~50 at a time
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i+50]
        filters = "video==" + ",".join(batch)
        try:
            resp = analytics_service.reports().query(
                ids="channel==MINE",
                startDate=start_date,
                endDate=end_date,
                metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage",
                dimensions="video",
                filters=filters,
            ).execute()

            headers = [h["name"] for h in resp.get("columnHeaders", [])]
            for row in resp.get("rows", []):
                row_dict = dict(zip(headers, row))
                vid = row_dict.get("video")
                if not vid:
                    continue
                results[vid] = {
                    "views":                 int(row_dict.get("views", 0)),
                    "watch_time_min":        float(row_dict.get("estimatedMinutesWatched", 0)),
                    "avg_view_duration_sec": float(row_dict.get("averageViewDuration", 0)),
                    "avg_view_percentage":   float(row_dict.get("averageViewPercentage", 0)),
                    "ctr":                   channel_ctr,
                }
        except Exception as e:
            logger.warning(f"Analytics API fetch failed for batch: {e}")

    logger.info(f"Fetched analytics for {len(results)}/{len(video_ids)} videos (channel CTR={channel_ctr:.3f})")
    return results
