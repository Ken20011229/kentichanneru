"""
Self-improvement engine.

Analyzes video_log.json → writes strategy.json + evolves prompt_hints.json.

strategy.json:
  channel_weights       — 実績ベースのチャンネル重み
  bgm_ranking           — 視聴維持率が高かったBGM順
  thumbnail_style_score — サムネイルスタイル別CTR
  avg_ctr / avg_retention — 全体平均

prompt_hints.json:
  top_title_patterns    — 高再生タイトルのパターン
  hook_examples         — 高維持率動画の冒頭文
  style_notes           — 改善メモ（次回のプロンプトに挿入）
"""
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_STRATEGY_FILE     = "data/strategy.json"
_PROMPT_HINTS_FILE = "data/prompt_hints.json"
_MIN_VIDEOS        = 3


def _avg(values):
    vals = [v for v in values if v is not None and v >= 0]
    return round(sum(vals) / len(vals), 4) if vals else 0.0


def run(log_file: str = "data/video_log.json"):
    if not Path(log_file).exists():
        logger.info("No video log — nothing to evolve.")
        return

    with open(log_file, "r", encoding="utf-8") as f:
        records = json.load(f)

    with_stats     = [r for r in records if r.get("views") is not None]
    with_analytics = [r for r in records if r.get("ctr") is not None]

    logger.info(f"Records: {len(records)} total, {len(with_stats)} with stats, {len(with_analytics)} with analytics")

    if len(with_stats) < _MIN_VIDEOS:
        logger.info(f"Need {_MIN_VIDEOS}+ videos with stats. Got {len(with_stats)}.")
        return

    # ⚠ サンプル数ゲート: 再生数が一桁の動画どうしの差（15再生 vs 9再生）から
    # 「鉄板パターン」を抽出して重みやプロンプトに反映すると、ノイズを学習して
    # 増幅するだけになる。母数が揃うまでは探索モード（全て等重み）を維持する。
    # prompt_hints だけでなく channel_weights / bgm_ranking にも同じゲートを
    # 掛ける（片方だけ塞いでも、乱数の偏りがそのまま固定化される）。
    _MIN_VIDEOS_FOR_LEARNING = 20
    _MIN_VIEWS_FOR_LEARNING  = 50
    max_views = max((r.get("views", 0) for r in with_stats), default=0)
    learning_ready = (
        len(with_stats) >= _MIN_VIDEOS_FOR_LEARNING and max_views >= _MIN_VIEWS_FOR_LEARNING
    )
    if not learning_ready:
        logger.info(
            f"Learning gated: {len(with_stats)} videos / max {max_views} views "
            f"(need {_MIN_VIDEOS_FOR_LEARNING} / {_MIN_VIEWS_FOR_LEARNING}) — "
            f"staying in exploration mode (equal weights, no prompt hints)"
        )

    # ── Channel weights (by views) ────────────────────────────────
    ch_views = defaultdict(list)
    ch_ctr   = defaultdict(list)
    ch_ret   = defaultdict(list)
    for r in with_stats:
        ch = r.get("channel_id", "unknown")
        ch_views[ch].append(r.get("views", 0))
    for r in with_analytics:
        ch = r.get("channel_id", "unknown")
        if r.get("ctr"):      ch_ctr[ch].append(r["ctr"])
        if r.get("avg_view_duration_sec"): ch_ret[ch].append(r["avg_view_duration_sec"])

    avg_views  = {ch: _avg(v) for ch, v in ch_views.items()}
    global_avg = _avg([v for vs in ch_views.values() for v in vs]) or 1.0
    # 母数が揃うまでは全チャンネル等重み。実データでは technology が「1本が
    # 0再生」というだけで weight 0.2（gaming 1.8 の1/9）まで落ちており、
    # n=1 の偶然がジャンル選定を半永久的に固定していた。
    if learning_ready:
        channel_weights = {
            ch: round(min(2.0, max(0.2, avg / global_avg)), 3)
            for ch, avg in avg_views.items()
        }
    else:
        channel_weights = {ch: 1.0 for ch in avg_views}

    # ── Thumbnail style weights (by CTR) ─────────────────────────
    from src.video.thumbnail_style_selector import STYLES
    style_ctr: dict = defaultdict(list)
    for r in with_analytics:
        st = r.get("thumbnail_style")
        if st and r.get("ctr") is not None:
            style_ctr[st].append(r["ctr"])

    thumbnail_style_weights: dict = {}

    # ── BGM ranking (by avg view duration = retention proxy) ──────
    bgm_ret  = defaultdict(list)
    bgm_view = defaultdict(list)
    for r in with_stats:
        bgm = r.get("bgm")
        if bgm:
            bgm_view[bgm].append(r.get("views", 0))
    for r in with_analytics:
        bgm = r.get("bgm")
        if bgm and r.get("avg_view_duration_sec"):
            bgm_ret[bgm].append(r["avg_view_duration_sec"])

    # Prefer retention data; fallback to views
    bgm_score = {}
    for bgm in set(list(bgm_ret.keys()) + list(bgm_view.keys())):
        if bgm in bgm_ret and bgm_ret[bgm]:
            bgm_score[bgm] = _avg(bgm_ret[bgm])
        else:
            bgm_score[bgm] = _avg(bgm_view.get(bgm, [0])) / 100  # normalize
    bgm_ranking = sorted(bgm_score.keys(), key=lambda b: bgm_score[b], reverse=True)

    # ── Global averages ───────────────────────────────────────────
    all_ctrs = [r["ctr"] for r in with_analytics if r.get("ctr")]
    all_rets = [r["avg_view_duration_sec"] for r in with_analytics if r.get("avg_view_duration_sec")]
    global_ctr = _avg(all_ctrs)
    global_ret = _avg(all_rets)

    # ── Thumbnail style weights (CTR-based learning) ──────────────
    for st in STYLES:
        ctrs = style_ctr.get(st, [])
        if len(ctrs) >= 2:
            avg_st_ctr = _avg(ctrs)
            weight = round(min(3.0, max(0.3, avg_st_ctr / max(global_ctr, 0.01))), 3)
        else:
            weight = 1.0
        thumbnail_style_weights[st] = weight

    # ── Insights ──────────────────────────────────────────────────
    insights = []
    for ch in sorted(channel_weights, key=lambda c: -channel_weights[c]):
        w   = channel_weights[ch]
        avg = round(avg_views.get(ch, 0))
        ctr = round(_avg(ch_ctr.get(ch, [])) * 100, 2)
        ret = round(_avg(ch_ret.get(ch, [])))
        insights.append(f"[{ch}] views={avg} weight={w} CTR={ctr}% retention={ret}s")

    top5 = sorted(with_stats, key=lambda r: r.get("views", 0), reverse=True)[:5]
    if top5:
        insights.append("Top 5 by views:")
        for r in top5:
            insights.append(f"  {r.get('views',0)}views CTR={round((r.get('ctr') or 0)*100,1)}% [{r.get('title','?')}]")

    if bgm_ranking:
        insights.append(f"Best BGM (retention): {bgm_ranking[0]}")

    # Thumbnail style insights
    styles_with_data = {s: w for s, w in thumbnail_style_weights.items() if style_ctr.get(s)}
    if styles_with_data:
        best_style  = max(styles_with_data, key=styles_with_data.get)
        worst_style = min(styles_with_data, key=styles_with_data.get)
        insights.append(f"Thumbnail style CTR weights: {thumbnail_style_weights}")
        insights.append(f"Best style: {best_style} (weight={thumbnail_style_weights[best_style]:.2f}), "
                        f"Worst: {worst_style} (weight={thumbnail_style_weights[worst_style]:.2f})")
        sample_info = {s: len(style_ctr.get(s, [])) for s in STYLES}
        insights.append(f"Style sample counts: {sample_info}")

    # ── Write strategy.json ───────────────────────────────────────
    strategy = {
        "updated_at":           datetime.now(timezone.utc).isoformat(),
        "videos_analyzed":      len(with_stats),
        "analytics_available":  len(with_analytics),
        # 消費側（_pick_bgm / channel_selector）が「この重みを信じてよいか」を
        # 判断するためのフラグ。False の間は探索モードとして等重みで扱う。
        "learning_ready":       learning_ready,
        "channel_weights":      channel_weights,
        "avg_views_per_channel": {ch: round(_avg(v)) for ch, v in ch_views.items()},
        "avg_ctr_per_channel":  {ch: round(_avg(v)*100, 2) for ch, v in ch_ctr.items()},
        "avg_retention_per_channel": {ch: round(_avg(v)) for ch, v in ch_ret.items()},
        "global_avg_ctr_pct":   round(global_ctr * 100, 2),
        "global_avg_retention_sec": round(global_ret),
        "bgm_ranking":          bgm_ranking,
        "bgm_score":            {b: round(s, 2) for b, s in bgm_score.items()},
        "thumbnail_style_weights":       thumbnail_style_weights,
        "thumbnail_style_sample_counts": {s: len(style_ctr.get(s, [])) for s in STYLES},
        "insights":             insights,
    }
    Path(_STRATEGY_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(_STRATEGY_FILE, "w", encoding="utf-8") as f:
        json.dump(strategy, f, ensure_ascii=False, indent=2)

    # ── Write prompt_hints.json (for script prompt evolution) ─────
    top10 = sorted(with_stats, key=lambda r: r.get("views", 0), reverse=True)[:10]
    # 再生数が閾値未満のタイトルを「高再生数の例」として渡さない
    top_titles = [
        r.get("title", "") for r in top10
        if r.get("title") and r.get("views", 0) >= _MIN_VIEWS_FOR_LEARNING
    ]

    # 維持率は「秒」ではなく「割合」で見る。動画尺が60秒→320秒に変わったのに
    # 絶対秒の閾値(45s/80s/150s)のまま判定していて、意味を失っていた。
    retention_sorted = sorted(
        with_analytics, key=lambda r: r.get("avg_view_percentage", 0), reverse=True
    )
    top_ret_titles = [r.get("title", "") for r in retention_sorted[:5] if r.get("title")]

    # ── Title pattern analysis ────────────────────────────────────
    # Extract bracket patterns from high-performing titles
    import re
    bracket_counts = defaultdict(int)
    for r in top5:
        m = re.search(r'【(.+?)】', r.get('title', ''))
        if m:
            bracket_counts[m.group(1)] += r.get('views', 1)
    top_brackets = sorted(bracket_counts, key=lambda k: -bracket_counts[k])[:5]

    # ── Build style notes ─────────────────────────────────────────
    style_notes = []
    if global_ctr > 0:
        if global_ctr < 0.03:
            style_notes.append("CTRが低い(< 3%)。サムネイルタイトルに具体的な数字・年号・金額を必ず入れる")
            style_notes.append("サムネイルの文字数を減らし、大きく読みやすくする（12文字以内推奨）")
        elif global_ctr < 0.05:
            style_notes.append("CTRが改善余地あり(3-5%)。タイトルの冒頭【】に感情を動かす動詞を使う")
        elif global_ctr > 0.07:
            style_notes.append("CTRが高い(> 7%)。現在のタイトルスタイルは非常に効果的 — 維持する")

    # 維持率は割合で判定する（動画尺が可変なので絶対秒では比較にならない）
    all_pcts = [r["avg_view_percentage"] for r in with_analytics if r.get("avg_view_percentage")]
    global_pct = _avg(all_pcts)
    if global_pct > 0:
        if global_pct < 30:
            style_notes.append(f"平均視聴率が非常に低い({global_pct:.0f}%)。最初の5秒で核心を言い切る。前置き禁止")
            style_notes.append("動画の冒頭15秒でサムネイルの答えを出す（「釣り」は離脱を招く）")
        elif global_pct < 45:
            style_notes.append(f"平均視聴率が低い({global_pct:.0f}%)。中盤に新情報・驚きを置いて引き留める")
        elif global_pct > 55:
            style_notes.append(f"平均視聴率が高い({global_pct:.0f}%)。現在の構成は効果的 — 維持する")

    if top_brackets and learning_ready:
        style_notes.append(f"高再生数タイトルの鉄板パターン: 【{'】【'.join(top_brackets[:3])}】")

    # Thumbnail style learning notes
    if styles_with_data:
        best_s = max(thumbnail_style_weights, key=thumbnail_style_weights.get)
        worst_s = min(thumbnail_style_weights, key=thumbnail_style_weights.get)
        bw, ww = thumbnail_style_weights[best_s], thumbnail_style_weights[worst_s]
        if bw > ww * 1.4:
            style_notes.append(
                f"サムネイルスタイル「{best_s}」のCTRが最も高い(weight={bw:.2f})。"
                f"「{worst_s}」(weight={ww:.2f})より優位 — 自動的に出現頻度を上げています"
            )
        # Styles with no data are undersampled — encourage exploration
        untested = [s for s in STYLES if not style_ctr.get(s)]
        if untested:
            style_notes.append(
                f"未テストのサムネイルスタイル: {', '.join(untested)} — "
                f"データ蓄積のため均等に試行中（重み=1.0）"
            )

    # Shorts-specific insights
    shorts_records = [r for r in with_stats if r.get('shorts_views') is not None]
    if shorts_records:
        avg_shorts_views = _avg([r['shorts_views'] for r in shorts_records])
        avg_main_views   = _avg([r.get('views', 0) for r in with_stats])
        if avg_shorts_views > avg_main_views * 5:
            style_notes.append("Shortsの再生数が横動画の5倍以上 — Shorts投稿を優先する戦略が有効")

    hints = {
        "updated_at":           datetime.now(timezone.utc).isoformat(),
        "learning_ready":       learning_ready,
        "top_titles_by_view":   top_titles if learning_ready else [],
        "top_titles_by_retention": top_ret_titles if learning_ready else [],
        "top_bracket_patterns": top_brackets if learning_ready else [],
        "style_notes":          style_notes,
        "global_ctr_pct":       round(global_ctr * 100, 2),
        "global_retention_sec": round(global_ret),
        "global_avg_view_percentage": round(global_pct, 1),
    }
    with open(_PROMPT_HINTS_FILE, "w", encoding="utf-8") as f:
        json.dump(hints, f, ensure_ascii=False, indent=2)

    logger.info("=== Strategy + PromptHints updated ===")
    for line in insights:
        logger.info(f"  {line}")
    for note in style_notes:
        logger.info(f"  [HINT] {note}")
