import json
import logging
import random
from pathlib import Path

logger = logging.getLogger(__name__)


def get_active_channel(config: dict) -> dict:
    """Return a channel using weighted random selection based on performance strategy."""
    channels = config.get("channels", [])
    if not channels:
        logger.warning("No channels defined in config — using empty channel")
        return {}

    # Load performance weights from strategy.json
    strategy_file = "data/strategy.json"
    channel_weights = {}
    if Path(strategy_file).exists():
        try:
            with open(strategy_file, "r", encoding="utf-8") as f:
                strategy = json.load(f)
            channel_weights = strategy.get("channel_weights", {})
            if channel_weights:
                logger.info(f"Loaded channel weights from strategy: {channel_weights}")
        except Exception as e:
            logger.warning(f"Could not read strategy ({e}), using equal weights")

    # Build weighted list: better-performing channels appear more often
    weights = [channel_weights.get(ch["id"], 1.0) for ch in channels]

    # Weighted random selection
    total = sum(weights)
    r = random.uniform(0, total)
    cumulative = 0.0
    selected = channels[-1]
    for ch, w in zip(channels, weights):
        cumulative += w
        if r <= cumulative:
            selected = ch
            break

    logger.info(f"Selected channel: {selected.get('name')} (weight={channel_weights.get(selected['id'], 1.0):.2f})")
    return selected
