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
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run the pipeline once and exit (no scheduler)",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Run the pipeline but skip YouTube upload (for testing)",
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to config YAML file (default: config/config.yaml)",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config)
    logger = logging.getLogger(__name__)

    if args.once:
        logger.info("Running pipeline once...")
        from src.pipeline import run_pipeline
        run_pipeline(config, skip_upload=args.skip_upload)
    else:
        from src.scheduler import start_scheduler
        start_scheduler(config)


if __name__ == "__main__":
    main()
