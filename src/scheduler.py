import logging
import os

import portalocker
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config_loader import load_config
from src.pipeline import run_pipeline

logger = logging.getLogger(__name__)


def run_with_lock(config: dict):
    lock_file = config["scheduler"].get("lock_file", "output/.scheduler.lock")
    os.makedirs(os.path.dirname(lock_file), exist_ok=True)
    try:
        with portalocker.Lock(lock_file, timeout=1):
            run_pipeline(config)
    except portalocker.LockException:
        logger.warning("Another pipeline run is already in progress. Skipping this trigger.")
    except Exception as e:
        logger.exception(f"Pipeline run failed: {e}")


def start_scheduler(config: dict = None):
    if config is None:
        config = load_config()

    tz = config["scheduler"].get("timezone", "Asia/Tokyo")
    scheduler = BlockingScheduler(timezone=tz)

    for job in config["scheduler"].get("jobs", []):
        parts = job["cron"].split()
        trigger = CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
            timezone=tz,
        )
        scheduler.add_job(
            func=run_with_lock,
            trigger=trigger,
            args=[config],
            id=job["id"],
            max_instances=1,
            misfire_grace_time=300,
        )
        logger.info(f"Scheduled job '{job['id']}' — cron: {job['cron']} ({tz})")

    logger.info("Scheduler started. Press Ctrl+C to exit.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")
