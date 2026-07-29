import glob
import json
import logging
import math
import os
import random
import re
import shutil
import tempfile
import uuid
from datetime import datetime

from src.analytics.video_tracker import log_video
from src.channel_selector import get_active_channel
from src.config_loader import load_config
from src.deduplicator import Deduplicator
from src.fetcher.aggregator import fetch_and_select_item
from src.fetcher.article_extractor import fetch_article_body
from src.images.fallback_generator import generate_fallback_images
from src.images.pexels_client import PexelsClient
from src.images.unsplash_client import UnsplashClient
from src.script_gen.claude_scriptwriter import ClaudeScriptWriter, TokenBudgetExhausted
from src.translator.claude_translator import ClaudeTranslator
from src.tts.voicevox_client import VoicevoxClient
from src.uploader.youtube_uploader import YouTubeUploader
from src.video.composer import compose_audio, render_video
from src.video.shorts_composer import render_shorts
from src.video.shorts_slide_gen import generate_shorts_slides
from src.video.subtitle_gen import generate_ass, generate_shorts_ass
from src.video.thumbnail_gen import generate_shorts_thumbnail, generate_thumbnail
from src.video.thumbnail_style_selector import select_style
from src.bgm.bgm_moods import mood_for_channel, filter_by_mood, usable_tracks

logger = logging.getLogger(__name__)


def _pick_bgm(bgm_files: list, bgm_dir: str, mood: str = None) -> str | None:
    """Pick BGM: narrow to channel-appropriate mood first, then prefer
    top-ranked (by past CTR/retention) from strategy.json within that mood.

    以前は候補が無いときのフォールバックが `background.mp3` だったが、これは
    実測 -91 dB の無音ファイルで、返した時点で「BGMの無い動画」が確定する。
    鳴らせる曲が1つも無いなら None を返し、呼び出し側で BGM 無しとして
    正しく扱う（無音ファイルを混ぜても結果は同じで、ログ上だけ成功に見える）。
    """
    bgm_files = usable_tracks(bgm_files or [])
    if not bgm_files:
        logger.warning("No usable BGM tracks found — rendering without BGM")
        return None

    if mood:
        bgm_files = filter_by_mood(bgm_files, mood)

    strategy_file = "data/strategy.json"
    if os.path.exists(strategy_file):
        try:
            with open(strategy_file, "r", encoding="utf-8") as f:
                strategy = json.load(f)
            ranking = strategy.get("bgm_ranking", [])
            # ランキングは実測 n=1・43再生秒といった単発データから作られるため、
            # 母数が揃うまで重み付けを効かせない（1本の偶然で1曲に3倍の重みが
            # 付き、そのまま固定化される）。evolver が立てる学習ゲートに従う。
            if strategy.get("learning_ready"):
                weights = []
                names   = [os.path.basename(p) for p in bgm_files]
                for name in names:
                    rank = ranking.index(name) if name in ranking else len(ranking)
                    weights.append(3.0 if rank < 3 else 1.0)
                return random.choices(bgm_files, weights=weights, k=1)[0]
        except Exception:
            pass

    return random.choice(bgm_files)


def _realign_channel(config: dict, channel: dict, item: dict) -> dict:
    """グローバルフォールバックで拾った記事に、ブランディングを合わせ直す。

    チャンネル専用フィードが空だと `fetch_and_select_item` は全ソースから
    選び直すが、そのままだと「映画情報」のバッジ・赤系アクセント・映画評論の
    語り口で天気ニュースを読む動画になる（本番で発生）。記事のソース名から
    実際に合うチャンネルへ寄せる。
    """
    if not item.get("from_global_fallback"):
        return channel

    source   = (item.get("source") or "").lower()
    channels = config.get("channels", [])
    for ch in channels:
        for feed in ch.get("rss_feeds", []):
            if feed.get("name", "").lower() and feed["name"].lower() in source:
                if ch.get("id") != channel.get("id"):
                    logger.warning(
                        f"Channel realigned {channel.get('id')} -> {ch['id']} "
                        f"(item came from '{item.get('source')}' via global fallback)"
                    )
                return ch
    fallback = next((c for c in channels if c.get("id") == "news"), channel)
    if fallback.get("id") != channel.get("id"):
        logger.warning(
            f"Channel realigned {channel.get('id')} -> {fallback.get('id')} "
            f"(no channel matches source '{item.get('source')}')"
        )
    return fallback


def _chapter_label(seg: dict) -> str:
    """章題を作る。

    keyword をそのまま使うと「気象庁」「関東」のような2〜3文字の名詞が並び、
    「主要な場面」に出てもクリックする理由にならない。本文の1文目を短く
    切り詰めて、何の話が始まるのか分かる見出しにする。
    """
    text  = (seg.get("text") or "").strip()
    first = re.split(r"(?<=[。！？])", text)[0].strip() if text else ""
    first = first.rstrip("。！？")
    if len(first) >= 6:
        return _smart_trim(first, 24)
    return (seg.get("keyword") or "").strip()


def _chapters(script_segments: list[dict], audio_segments: list[dict],
              min_gap: float = 45.0) -> list[str]:
    """YouTube のチャプター行を作る。

    仕様上「00:00 で始まる」「3つ以上」「各チャプター10秒以上」が必要。
    min_gap が 12 秒だと平均24.6秒のセグメントがほぼ全部チャプターになり、
    5分の動画に13章が並んでいた（YouTube の推奨は5〜8章）。
    """
    lines, t, last_at = [], 0.0, -999.0
    for seg, audio in zip(script_segments, audio_segments):
        label = _chapter_label(seg)
        if not lines:
            label = label or "はじめに"
        if label and (t - last_at) >= min_gap:
            lines.append(f"{int(t // 60):02d}:{int(t % 60):02d} {label}")
            last_at = t
        t += audio.get("duration_sec", 0.0)
    return lines if len(lines) >= 3 else []


def _build_description(script_data: dict, item: dict, channel: dict,
                       audio_segments: list[dict]) -> str:
    """LLM の本文に、チャプター・出典・CTA・ハッシュタグを足して組み立てる。

    以前は LLM が返した 400〜500 文字をそのまま送っていた（上限は5000文字）。
    チャプターが無いと「主要な場面」が生成されず、離脱した視聴者が戻れない。
    出典URLは aggregator が既に持っているのに捨てられていた。
    """
    blocks = [(script_data.get("description") or "").strip()]

    chapters = _chapters(script_data.get("script_segments", []), audio_segments)
    if chapters:
        blocks.append("▼ チャプター\n" + "\n".join(chapters))

    src_name = item.get("source", "")
    src_url  = item.get("url", "")
    if src_url:
        blocks.append(f"▼ 出典\n{src_name}: {src_url}".strip())
    elif src_name:
        blocks.append(f"▼ 出典\n{src_name}")

    blocks.append(
        "※本動画は上記の報道・公開情報をもとに、AI音声（VOICEVOX）で再構成した解説です。"
        "内容は投稿時点の情報に基づきます。"
    )

    tags = [t for t in script_data.get("tags", []) if t][:3]
    if tags:
        blocks.append(" ".join(f"#{t.replace(' ', '')}" for t in tags))

    return "\n\n".join(b for b in blocks if b)[:4900]


_TRIM_BREAKS = "、。！？「」『』（）・ "


def _smart_trim(text: str, limit: int) -> str:
    """語や括弧の途中で切らずに limit 文字以内へ縮める。"""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for i in range(len(cut) - 1, max(len(cut) - 8, 0), -1):
        if cut[i] in _TRIM_BREAKS:
            cut = cut[:i]
            break
    # 開いたままの括弧を落とす
    for op, cl in (("「", "」"), ("『", "』"), ("（", "）")):
        if cut.count(op) > cut.count(cl):
            cut = cut[:cut.rfind(op)]
    return cut.rstrip("、。・ ") or text[:limit]


def _pick_deep_dive_topic(config: dict) -> tuple[dict, dict]:
    """Read video_log.json and pick a past horizontal video topic to deep-dive on.

    Returns (topic_dict, channel_dict).
    topic_dict has keys: original_title, channel_id, channel_name
    """
    import json as _json
    log_path = "data/video_log.json"
    if not os.path.exists(log_path):
        raise FileNotFoundError("data/video_log.json not found — no past videos to deep-dive on")

    with open(log_path, "r", encoding="utf-8") as f:
        entries = _json.load(f)

    # Only consider horizontal videos (have video_id) and skip entries with generic/poor titles
    candidates = [
        e for e in entries
        if e.get("video_id") and e.get("title") and len(e.get("title", "")) > 5
    ]
    if not candidates:
        raise RuntimeError("No suitable past videos found in video_log.json")

    # Pick by engagement: prefer highest views, fall back to most recent
    candidates.sort(key=lambda e: (e.get("views", 0), e.get("shorts_views", 0) or 0), reverse=True)
    # Weighted random from top-5 so it's not always the same topic
    top = candidates[:min(5, len(candidates))]
    weights = [max(1, e.get("views", 0) + (e.get("shorts_views") or 0)) for e in top]
    chosen = random.choices(top, weights=weights, k=1)[0]

    # Find matching channel config
    channels = config.get("channels", [])
    channel = next((ch for ch in channels if ch.get("id") == chosen.get("channel_id")), None)
    if not channel and channels:
        channel = channels[0]

    logger.info(
        f"Deep-dive topic selected: '{chosen['title']}' "
        f"(channel={chosen.get('channel_name')}, views={chosen.get('views', 0)})"
    )
    return chosen, channel or {}


def run_deep_dive_pipeline(config: dict = None, skip_upload: bool = False):
    """Run a deep-dive video pipeline on a past horizontal video topic."""
    if config is None:
        config = load_config()

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_dd_" + uuid.uuid4().hex[:6]
    work_dir = tempfile.mkdtemp(prefix=f"ytauto_{run_id}_")

    topic, channel = _pick_deep_dive_topic(config)
    config["active_channel"] = channel
    logger.info(f"=== Deep-dive pipeline started: {run_id} | topic: '{topic['title']}' ===")

    try:
        writer = ClaudeScriptWriter(config["groq"])
        script_data = writer.generate_deep_dive(topic["title"], channel=channel)
        logger.info(
            f"Deep-dive script generated: '{script_data['title']}' "
            f"({len(script_data['script_segments'])} segments)"
        )

        # From here the rest of the pipeline is identical to run_pipeline —
        # inject a synthetic item so we can reuse the shared stages below
        synthetic_item = {
            "id": f"deepdive_{run_id}",
            "title": topic["title"],
            "summary": "",
            "source": "deep_dive",
        }
        _run_shared_pipeline_stages(
            config, script_data, synthetic_item, channel,
            work_dir, run_id, skip_upload,
        )

    except Exception as e:
        logger.exception(f"Deep-dive pipeline {run_id} failed: {e}")
        raise
    finally:
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
            logger.debug(f"Cleaned up work dir: {work_dir}")


def _run_shared_pipeline_stages(
    config: dict,
    script_data: dict,
    item: dict,
    channel: dict,
    work_dir: str,
    run_id: str,
    skip_upload: bool,
) -> str | None:
    """Execute TTS → images → slides → subtitle → render → upload (stages 4-9).

    Shared between run_pipeline and run_deep_dive_pipeline.
    Returns the uploaded video_id, or None when skip_upload=True.
    """
    # Stage 4: TTS synthesis
    tts_provider = os.environ.get("TTS_PROVIDER", config.get("tts", {}).get("provider", "voicevox"))
    ffmpeg_path = os.environ.get("FFMPEG_PATH", config["video"].get("ffmpeg_path", "ffmpeg"))
    config["video"]["ffmpeg_path"] = ffmpeg_path
    if tts_provider == "edge_tts":
        from src.tts.edge_tts_client import EdgeTTSClient
        tts = EdgeTTSClient(config.get("edge_tts", {}), ffmpeg=ffmpeg_path)
    else:
        tts = VoicevoxClient(config["voicevox"])
    audio_dir = os.path.join(work_dir, "audio")
    audio_segments = tts.synthesize_segments(script_data["script_segments"], audio_dir)

    # Stage 4b: TTS for Shorts dedicated script
    shorts_audio_segments = []
    shorts_script = script_data.get("shorts_script_segments", [])
    if shorts_script:
        shorts_audio_dir = os.path.join(work_dir, "audio_shorts")
        try:
            shorts_audio_segments = tts.synthesize_segments(shorts_script, shorts_audio_dir)
            logger.info(f"Shorts TTS synthesized: {len(shorts_audio_segments)} segments, "
                        f"{sum(s['duration_sec'] for s in shorts_audio_segments):.1f}s total")
        except Exception as e:
            logger.warning(f"Shorts TTS failed (will use truncated main audio): {e}")

    # Stage 5: Fetch a *pool* of photos — background + per-segment visuals.
    # 以前は total=1 で1枚しか取らず、5〜7分の全編がその1枚のぼかし背景のまま
    # だった。中央のコンテンツ枠にも写真が1枚も入らず、全スライドが
    # 「巨大な黒箱に2〜3文字」になっていた。config の images_per_video を使う。
    keywords = script_data.get("image_search_keywords", ["news"])
    image_dir = os.path.join(work_dir, "images")
    os.makedirs(image_dir, exist_ok=True)
    n_photos    = int(config["images"].get("images_per_video", 8))
    photo_pool: list[str] = []
    shorts_photo_pool: list[str] = []

    pexels_cfg = config["images"].get("pexels", {})
    if pexels_cfg.get("enabled") and os.environ.get("PEXELS_API_KEY"):
        pexels = PexelsClient(os.environ["PEXELS_API_KEY"])
        photo_pool = pexels.fetch_images_for_keywords(keywords, image_dir, total=n_photos)
        try:
            shorts_photo_pool = pexels.fetch_images_for_keywords(
                keywords, image_dir, total=4, orientation="portrait"
            )
        except Exception as e:
            logger.warning(f"Pexels portrait fetch failed: {e}")

    if not photo_pool:
        unsplash_cfg = config["images"].get("unsplash", {})
        if unsplash_cfg.get("enabled") and os.environ.get("UNSPLASH_ACCESS_KEY"):
            unsplash = UnsplashClient(os.environ["UNSPLASH_ACCESS_KEY"])
            photo_pool = unsplash.fetch_images_for_keywords(keywords, image_dir, total=n_photos)

    if not photo_pool:
        logger.warning("No images from APIs, generating fallback gradient for thumbnail")
        photo_pool = generate_fallback_images(image_dir, 1)

    image_paths   = photo_pool
    bg_image_path = photo_pool[0] if photo_pool else None
    logger.info(f"Photo pool: {len(photo_pool)} landscape / {len(shorts_photo_pool)} portrait")

    # Stage 5.5a: Per-segment AI images for "image" visual_type segments
    # 背景・コンテンツ用の写真は Stage 5 の Pexels プールが担うようになったので、
    # HuggingFace はここ（セグメント固有の生成画像）専用になった。
    hf_cfg = config["images"].get("huggingface", {})
    segment_images: dict[int, str] = {}
    if hf_cfg.get("enabled") and os.environ.get("HF_TOKEN"):
        from src.images.huggingface_client import HuggingFaceImageClient
        hf_gen = HuggingFaceImageClient(os.environ["HF_TOKEN"])
        image_segs = [
            (i, seg) for i, seg in enumerate(script_data["script_segments"])
            if seg.get("visual_type") == "image" and seg.get("image_prompt", "").strip()
        ]
        for idx, seg in image_segs[:8]:
            prompt   = seg["image_prompt"]
            out_path = os.path.join(image_dir, f"seg_{idx:03d}.jpg")
            result   = hf_gen.generate_image(prompt, out_path)
            if result:
                segment_images[idx] = result
                logger.info(f"Segment {idx} image generated: {prompt[:60]}")

    # Stage 5.5b: Character overlays — one per facial variant, switched over time.
    # 差分アセット(zundamon1/2.png 等)は用意されていたのに使われておらず、
    # キャラが全編まったく動かない静止画になっていた。
    from src.video.slide_gen import build_character_timeline, generate_slides
    char_dir = os.path.join(work_dir, "chars")
    seg_durations = [s["duration_sec"] for s in audio_segments]
    try:
        character_layers = build_character_timeline(
            config, script_data["script_segments"], seg_durations, char_dir,
            speaker_sides=[s.get("speaker_side") for s in audio_segments],
        )
    except Exception as e:
        logger.warning(f"Character overlay generation failed (skipping): {e}")
        character_layers = []

    # Stage 5.5c: Generate slides
    slide_dir = os.path.join(work_dir, "slides")
    os.makedirs(slide_dir, exist_ok=True)
    segs_with_meta = [
        {**seg, "segment_index": i}
        for i, seg in enumerate(script_data["script_segments"])
    ]
    slide_plate_paths, slide_content_paths, per_slide_durations = generate_slides(
        segs_with_meta,
        title=script_data["title"],
        config=config,
        output_dir=slide_dir,
        bg_image_path=bg_image_path,
        segment_images=segment_images,
        durations=seg_durations,
        photo_pool=photo_pool,
    )
    if not per_slide_durations:
        per_slide_durations = seg_durations

    # Stage 6: Generate subtitles
    subtitle_path = os.path.join(work_dir, "subtitles", "subs.ass")
    generate_ass(audio_segments, subtitle_path, channel=channel)

    # Stage 7: Render video
    video_path = os.path.join(work_dir, "video", f"{run_id}.mp4")
    bgm_dir   = os.path.join("assets", "bgm")
    bgm_files = glob.glob(os.path.join(bgm_dir, "*.mp3"))
    bgm_mood  = mood_for_channel(channel["id"])
    bgm_path  = _pick_bgm(bgm_files, bgm_dir, mood=bgm_mood)
    logger.info(f"BGM: {os.path.basename(bgm_path)} (mood={bgm_mood})")
    se_path = config.get("se", {}).get("intro", os.path.join("assets", "se", "intro.mp3"))
    render_video(
        slide_plate_paths, slide_content_paths,
        audio_segments, bgm_path, subtitle_path, video_path, config,
        se_path=se_path,
        per_slide_durations=per_slide_durations,
        character_layers=character_layers,
    )

    # Stage 8: Generate thumbnail
    thumb_style = select_style()
    thumbnail_path = os.path.join(work_dir, "thumbnails", "thumb.jpg")
    logger.info(f"Stage 8: generating thumbnail (style={thumb_style})...")
    generate_thumbnail(
        image_paths[0], script_data["thumbnail_title"], thumbnail_path, config,
        reaction_text=script_data.get("reaction_text", ""),
        style=thumb_style,
    )
    logger.info("Stage 8: thumbnail done")

    # Stage 8b: Render Shorts
    shorts_dir  = os.path.join(work_dir, "shorts")
    shorts_sub  = os.path.join(shorts_dir, "subs_shorts.ass")
    shorts_path = os.path.join(shorts_dir, f"{run_id}_shorts.mp4")
    os.makedirs(shorts_dir, exist_ok=True)

    if shorts_audio_segments:
        logger.info("Stage 8b: composing shorts narration audio...")
        shorts_narration = os.path.join(shorts_dir, "shorts_narration.wav")
        compose_audio([s["audio_path"] for s in shorts_audio_segments], shorts_narration, ffmpeg_path)
        # 58秒で機械的にぶった切ると文の途中で終わり、ループしたときに
        # 気持ち悪い。セグメント境界で終わらせる。
        acc, kept = 0.0, 0
        for s in shorts_audio_segments:
            if acc + s["duration_sec"] > 58.0:
                break
            acc += s["duration_sec"]
            kept += 1
        if kept == 0:
            acc, kept = 58.0, len(shorts_audio_segments)
        else:
            shorts_audio_segments = shorts_audio_segments[:kept]
        shorts_dur = min(acc + 0.3, 58.5)
        generate_shorts_ass(shorts_audio_segments, shorts_sub, channel=channel, max_dur=shorts_dur)
        shorts_audio_path = shorts_narration

        shorts_segment_images: dict[int, str] = {}
        hf_cfg_s = config["images"].get("huggingface", {})
        if hf_cfg_s.get("enabled") and os.environ.get("HF_TOKEN"):
            from src.images.huggingface_client import HuggingFaceImageClient
            hf_s = HuggingFaceImageClient(os.environ["HF_TOKEN"])
            shorts_img_segs = [
                (i, seg) for i, seg in enumerate(shorts_audio_segments)
                if seg.get("visual_type") == "image" and seg.get("image_prompt", "").strip()
            ]
            logger.info(f"Stage 8b: generating {len(shorts_img_segs[:2])} shorts segment images via HF...")
            for s_idx, s_seg in shorts_img_segs[:2]:
                s_prompt   = s_seg["image_prompt"]
                s_out_path = os.path.join(image_dir, f"shorts_seg_{s_idx:03d}.jpg")
                s_result = hf_s.generate_image(s_prompt, s_out_path, width=768, height=1344)
                if s_result:
                    shorts_segment_images[s_idx] = s_result
                    logger.info(f"Shorts segment {s_idx} portrait image generated")
            logger.info("Stage 8b: shorts segment image generation done")

        logger.info("Stage 8b: generating shorts slides...")
        shorts_slide_dir = os.path.join(work_dir, "shorts_slides")
        shorts_plate_paths, shorts_content_paths = generate_shorts_slides(
            shorts_audio_segments,
            title=script_data["title"],
            config=config,
            output_dir=shorts_slide_dir,
            # 縦画面の下地に横長写真を敷くと大きく切り落とされるので、
            # portrait で取った分があればそちらを使う
            bg_image_path=(shorts_photo_pool or photo_pool or [None])[0],
            segment_images=shorts_segment_images,
            photo_pool=shorts_photo_pool or photo_pool,
        )
        logger.info("Stage 8b: shorts slides done")
        per_shorts_durations = [s["duration_sec"] for s in shorts_audio_segments]
    else:
        shorts_dur = 58.0
        generate_shorts_ass(audio_segments, shorts_sub, channel=channel, max_dur=shorts_dur)
        shorts_audio_path = os.path.join(work_dir, "video", "narration.wav")
        cycle_n = max(4, math.ceil(shorts_dur / 3) + 1)
        shorts_plate_paths   = (slide_plate_paths * 20)[:cycle_n]
        shorts_content_paths = (slide_content_paths * 20)[:cycle_n]
        per_shorts_durations = None

    shorts_thumb_path = os.path.join(shorts_dir, "shorts_thumb.jpg")
    logger.info("Stage 8b: generating shorts thumbnail...")
    try:
        generate_shorts_thumbnail(script_data["thumbnail_title"], shorts_thumb_path, config)
    except Exception as e:
        logger.warning(f"Shorts thumbnail generation failed: {e}")
        shorts_thumb_path = thumbnail_path

    logger.info(f"Stage 8b: rendering shorts video ({len(shorts_plate_paths)} slides)...")
    try:
        render_shorts(
            shorts_plate_paths, shorts_content_paths,
            shorts_audio_path, bgm_path, shorts_sub, shorts_path, config,
            max_duration=shorts_dur,
            per_slide_durations=per_shorts_durations,
        )
        shorts_ok = True
    except Exception as e:
        logger.warning(f"Shorts render failed (skipping): {e}")
        shorts_ok = False

    # Stage 9: Upload to YouTube
    if skip_upload:
        preview_dir = os.path.join("output", "preview", run_id)
        os.makedirs(preview_dir, exist_ok=True)
        preview_video = os.path.join(preview_dir, "video.mp4")
        preview_thumb = os.path.join(preview_dir, "thumbnail.jpg")
        shutil.copy2(video_path, preview_video)
        shutil.copy2(thumbnail_path, preview_thumb)
        if shorts_ok:
            shutil.copy2(shorts_path, os.path.join(preview_dir, "shorts.mp4"))
            if os.path.exists(shorts_thumb_path):
                shutil.copy2(shorts_thumb_path, os.path.join(preview_dir, "shorts_thumbnail.jpg"))
        logger.info(f"=== Pipeline run complete (upload skipped) ===")
        logger.info(f"  Title:     {script_data['title']}")
        logger.info(f"  Preview:   {preview_dir}")
        logger.info(f"  Video:     {preview_video}")
        logger.info(f"  Thumbnail: {preview_thumb}")
        return None

    uploader = YouTubeUploader(config)
    meta = {
        "title": script_data["title"],
        "description": _build_description(script_data, item, channel, audio_segments),
        "tags": script_data.get("tags", []),
        "category_id": channel.get("youtube_category_id"),
    }
    video_id = uploader.upload(video_path, thumbnail_path, meta)

    shorts_id = None
    if shorts_ok:
        hook = (script_data.get("shorts_hook") or "").strip()
        shorts_desc = "\n\n".join(filter(None, [
            f"▶ 本編（詳しい解説）→ https://youtu.be/{video_id}",
            hook,
            " ".join(f"#{t.replace(' ', '')}" for t in meta["tags"][:3]) + " #Shorts",
        ]))
        # 以前は title[:27] で機械的に切っていたため
        # 「【衝撃】Amazonセール『Nintendo Swit #Shorts」のように
        # 単語や鉤括弧の途中で切れていた。
        shorts_title = script_data.get("shorts_title") or script_data["title"]
        shorts_meta = {
            **meta,
            "title": _smart_trim(shorts_title, 27) + " #Shorts",
            "description": shorts_desc,
            "tags": meta["tags"] + ["Shorts", "YouTubeShorts"],
            # 本編と同じ題材なので、Shorts では登録者に通知しない。
            # 1回の実行で本編+Shortsの2通、1日2回で4通の通知が飛ぶと
            # 登録解除の理由になる。
            "notify_subscribers": False,
        }
        try:
            shorts_id = uploader.upload(shorts_path, shorts_thumb_path, shorts_meta)
            logger.info(f"Shorts uploaded: https://youtu.be/{shorts_id}")
            # 本編→Shorts の導線を追加する。説明欄は本編アップロード時点では
            # shorts_id が未確定なので、確定後に追記する（相互送客が片方向
            # だと、Shortsで拾った視聴者しか本編に流れない）。
            uploader.append_description(
                video_id, f"▶ ショート版（60秒で要点だけ）→ https://youtu.be/{shorts_id}"
            )
        except Exception as e:
            logger.warning(f"Shorts upload failed (main video still uploaded): {e}")

    log_video(video_id, {
        "shorts_id":       shorts_id,
        "channel_id":      channel.get("id"),
        "channel_name":    channel.get("name"),
        "title":           script_data["title"],
        "bgm":             os.path.basename(bgm_path),
        "thumbnail_style": thumb_style,
    })

    logger.info(f"=== Pipeline run complete: https://youtu.be/{video_id} ===")
    return video_id


def run_pipeline(config: dict = None, skip_upload: bool = False):
    if config is None:
        config = load_config()

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    work_dir = tempfile.mkdtemp(prefix=f"ytauto_{run_id}_")

    channel = get_active_channel(config)
    config["active_channel"] = channel
    logger.info(f"=== Pipeline run started: {run_id} | channel: {channel.get('name', 'default')} ===")

    try:
        # Stage 1: Fetch and select content
        dedup = Deduplicator(config["deduplication"])
        item = fetch_and_select_item(config, dedup, channel=channel)
        if not item:
            logger.info("No new content available. Skipping run.")
            return None

        dedup.mark_seen(item["id"])

        # Stage 1b: グローバルフォールバックで拾った場合、記事に合うチャンネルへ
        # ブランディングを寄せ直す
        channel = _realign_channel(config, channel, item)
        config["active_channel"] = channel

        # Stage 2: Translate if needed
        if item.get("needs_translation"):
            translator = ClaudeTranslator(config["groq"])
            translated = translator.translate_batch([item["title"], item["summary"]])
            item["title"] = translated[0]
            item["summary"] = translated[1]
            logger.info(f"Translated title: {item['title']}")

        # Stage 3: Generate script and metadata
        writer = ClaudeScriptWriter(config["groq"])
        script_data = None
        # 記事を替えても短さの傾向は変わらない（モデルの実力の問題で、記事の
        # 良し悪しではない）。6 記事を舐めても通らず、そのあいだに日次トークンを
        # 焼き切って以降の回まで 429 で落とすほうが損なので、リペアで拾えなかった
        # ぶんだけ数記事試す。予算切れの時点でループは即座に打ち切る。
        for _item_attempt in range(3):
            try:
                # 台本の素材は要約(百数十字)ではなく記事本文から取る
                if not item.get("body"):
                    item["body"] = fetch_article_body(item.get("url", ""))
                script_data = writer.generate(item, channel=channel)
                break
            except Exception as e:
                cause = e.last_attempt.exception() if hasattr(e, "last_attempt") else e
                logger.warning(f"Script generation failed for '{item['title']}': {cause!r}")
                if isinstance(cause, TokenBudgetExhausted):
                    # 記事を替えても叩くトークンが無い。次の回のために枠を残して降りる。
                    logger.error(f"{cause} — 残りの記事は試さず、次の回に予算を残して中断する")
                    break
                next_item = fetch_and_select_item(config, dedup, channel=channel)
                if not next_item:
                    raise RuntimeError("No more items available after script failures") from e
                dedup.mark_seen(next_item["id"])
                item = next_item
                channel = _realign_channel(config, channel, item)
                config["active_channel"] = channel
                if item.get("needs_translation"):
                    translator = ClaudeTranslator(config["groq"])
                    translated = translator.translate_batch([item["title"], item["summary"]])
                    item["title"] = translated[0]
                    item["summary"] = translated[1]
                logger.info(f"Retrying with next item: '{item['title']}'")
        if script_data is None:
            logger.warning(
                f"All item attempts failed via generate(); "
                f"falling back to deep-dive expansion for '{item['title']}'"
            )
            try:
                script_data = writer.generate_deep_dive(item["title"], channel=channel)
            except Exception as e:
                raise RuntimeError("All item attempts exhausted without generating a script") from e
        logger.info(f"Script generated: '{script_data['title']}' ({len(script_data['script_segments'])} segments)")

        return _run_shared_pipeline_stages(
            config, script_data, item, channel, work_dir, run_id, skip_upload,
        )

    except Exception as e:
        logger.exception(f"Pipeline run {run_id} failed: {e}")
        raise
    finally:
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
            logger.debug(f"Cleaned up work dir: {work_dir}")
