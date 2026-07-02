"""
Thumbnail style selection with CTR-based learning weights.

Styles:
  SPLIT  — Episode badge top-left, golden title left, both chars right (baseline)
  IMPACT — Giant text spans full width, chars small in corner (click-bait energy)
  NEON   — Near-black bg, glowing accent text, char with edge light (VTuber aesthetic)
  CARD   — Bright accent bg, white card left, dark text, chars overlap right (clean/modern)
  BAND   — Dark bg + thick accent stripe left edge, title vertically centered (bold graphic)

Weights are updated by evolver.py after accumulating CTR data.
"""
import json
import logging
import random
from pathlib import Path

logger = logging.getLogger(__name__)

STYLES = ["SPLIT", "IMPACT", "NEON", "CARD", "BAND"]

_STRATEGY_FILE = "data/thumbnail_strategy.json"
_STYLE_DESCRIPTIONS = {
    "SPLIT":  "回バッジ左・ゴールド大タイトル・キャラ右（ベースライン）",
    "IMPACT": "巨大テキストが画面幅を支配・キャラ小さめ右下隅",
    "NEON":   "漆黒BG＋ネオン発光テキスト＋キャラエッジライト",
    "CARD":   "アクセント色BG＋白カード＋ダーク文字・キャラ右オーバーラップ",
    "BAND":   "左辺アクセントバンド＋縦中央寄せタイトル・グラフィックデザイン風",
}


def get_style_weights() -> dict[str, float]:
    """Load style weights from strategy file. Returns neutral weights if no data."""
    if Path(_STRATEGY_FILE).exists():
        try:
            with open(_STRATEGY_FILE, "r", encoding="utf-8") as f:
                strategy = json.load(f)
            saved = strategy.get("thumbnail_style_weights", {})
            weights = {s: float(saved.get(s, 1.0)) for s in STYLES}
            return weights
        except Exception as e:
            logger.warning(f"Could not load style weights: {e}")
    return {s: 1.0 for s in STYLES}


def select_style() -> str:
    """Weighted random style selection. Prefers styles with higher historical CTR."""
    weights = get_style_weights()
    w = [max(0.1, weights[s]) for s in STYLES]
    chosen = random.choices(STYLES, weights=w, k=1)[0]
    logger.info(
        f"Thumbnail style: {chosen} — {_STYLE_DESCRIPTIONS[chosen]} "
        f"(weights={', '.join(f'{s}={weights[s]:.2f}' for s in STYLES)})"
    )
    return chosen


def get_style_report() -> list[str]:
    """Return human-readable style performance report lines."""
    weights = get_style_weights()
    lines = ["[サムネイルスタイル重み]"]
    for s in STYLES:
        bar = "█" * int(weights[s] * 5)
        lines.append(f"  {s:8s} {weights[s]:.2f} {bar}  {_STYLE_DESCRIPTIONS[s]}")
    return lines
