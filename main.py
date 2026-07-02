import argparse
import logging
import logging.handlers
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from src.config_loader import load_config


def setup_logging(config: dict):
    log_cfg = config.get("logging", {})
    os.makedirs("logs", exist_ok=True)
    root = logging.getLogger()
    root.setLevel(log_cfg.get("level", "INFO"))

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        log_cfg.get("file", "logs/autoposter.log"),
        maxBytes=log_cfg.get("max_bytes", 10_485_760),
        backupCount=log_cfg.get("backup_count", 5),
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    sys.stdout.reconfigure(encoding="utf-8")
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    root.addHandler(console_handler)


def main():
    parser = argparse.ArgumentParser(description="YouTube Auto Poster")
    parser.add_argument("--once",           action="store_true", help="Run pipeline once and exit")
    parser.add_argument("--deep-dive",      action="store_true", help="Deep-dive video on a past horizontal video topic")
    parser.add_argument("--skip-upload",    action="store_true", help="Skip YouTube upload (test mode)")
    parser.add_argument("--update-stats",   action="store_true", help="Fetch latest stats for all logged videos")
    parser.add_argument("--analyze",        action="store_true", help="Run self-improvement analysis and update strategy")
    parser.add_argument("--test-analytics", action="store_true", help="Test YouTube Analytics API connection")
    parser.add_argument("--test-trends",    action="store_true", help="Test Google Trends fetch")
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config)
    logger = logging.getLogger(__name__)

    if args.test_analytics:
        logger.info("Testing YouTube Analytics API connection...")
        from src.uploader.youtube_uploader import YouTubeUploader
        from src.analytics.yt_analytics import build_analytics_service, fetch_video_analytics
        uploader = YouTubeUploader(config)
        analytics_svc = build_analytics_service(uploader.service._http.credentials)
        from src.analytics.video_tracker import get_all
        ids = [r["video_id"] for r in get_all()[:3] if r.get("video_id")]
        if ids:
            result = fetch_video_analytics(analytics_svc, ids)
            logger.info(f"Analytics API OK — data for {len(result)} videos:")
            for vid, data in result.items():
                logger.info(f"  {vid}: CTR={data['ctr']*100:.1f}% retention={data['avg_view_duration_sec']:.0f}s views={data['views']}")
        else:
            logger.info("No videos in log to test with.")

    elif args.test_trends:
        logger.info("Testing Google Trends fetch...")
        from src.fetcher.trends_fetcher import fetch_trending_jp, fetch_trending_realtime_jp
        daily = fetch_trending_jp(max_topics=10)
        logger.info(f"Daily trends ({len(daily)} topics):")
        for t in daily[:10]:
            logger.info(f"  [{t['trend_rank']}] {t['title']} (score={t['score']:.1f})")
        rt = fetch_trending_realtime_jp(max_topics=5)
        if rt:
            logger.info(f"Realtime trends ({len(rt)} topics):")
            for t in rt[:5]:
                logger.info(f"  [{t['trend_rank']}] {t['title']}")

    elif args.update_stats or args.analyze:
        from src.uploader.youtube_uploader import YouTubeUploader
        uploader = YouTubeUploader(config)
        service  = uploader.service

        logger.info("Fetching latest video stats (Data API)...")
        from src.analytics.video_tracker import update_stats, update_analytics
        update_stats(service)

        # Also fetch CTR + view duration from Analytics API
        try:
            from src.analytics.yt_analytics import build_analytics_service
            analytics_svc = build_analytics_service(service._http.credentials)
            update_analytics(analytics_svc)
            logger.info("Analytics API (CTR/retention) updated.")
        except Exception as e:
            logger.warning(f"Analytics API unavailable (CTR/retention skipped): {e}")

        # Always run self-improvement analysis after stats update
        logger.info("Running self-improvement analysis...")
        from src.analytics.evolver import run as evolve
        evolve()

    elif args.deep_dive:
        logger.info("Running deep-dive pipeline...")
        from src.pipeline import run_deep_dive_pipeline
        run_deep_dive_pipeline(config, skip_upload=args.skip_upload)

    elif args.once:
        logger.info("Running pipeline once...")
        from src.pipeline import run_pipeline
        run_pipeline(config, skip_upload=args.skip_upload)

    else:
        from src.scheduler import start_scheduler
        start_scheduler(config)


if __name__ == "__main__":
    main()
