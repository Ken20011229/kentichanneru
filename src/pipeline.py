import glob
import json
import logging
import math
import os
import random
import shutil
import tempfile
import uuid
from datetime import datetime

from src.analytics.video_tracker import log_video
from src.channel_selector import get_active_channel
from src.config_loader import load_config
from src.deduplicator import Deduplicator
from src.fetcher.aggregator import fetch_and_select_item
from src.images.fallback_generator import generate_fallback_images
from src.images.pexels_client import PexelsClient
from src.images.unsplash_client import UnsplashClient
from src.script_gen.claude_scriptwriter import ClaudeScriptWriter
from src.translator.claude_translator import ClaudeTranslator
from src.tts.voicevox_client import VoicevoxClient
from src.uploader.youtube_uploader import YouTubeUploader
from src.video.composer import compose_audio, render_video
from src.video.shorts_composer import render_shorts
from src.video.shorts_slide_gen import generate_shorts_slides
from src.video.subtitle_gen import generate_ass, generate_shorts_ass
from src.video.thumbnail_gen import generate_shorts_thumbnail, generate_thumbnail
from src.video.thumbnail_style_selector import select_style
from src.bgm.bgm_moods import mood_for_channel, filter_by_mood

logger = logging.getLogger(__name__)


def _pick_bgm(bgm_files: list, bgm_dir: str, mood: str = None) -> str:
    """Pick BGM: narrow to channel-appropriate mood first, then prefer
    top-ranked (by past CTR/retention) from strategy.json within that mood."""
    if not bgm_files:
        return os.path.join(bgm_dir, "background.mp3")

    if mood:
        bgm_files = filter_by_mood(bgm_files, mood)

    strategy_file = "data/strategy.json"
    if os.path.exists(strategy_file):
        try:
            with open(strategy_file, "r", encoding="utf-8") as f:
                strategy = json.load(f)
            ranking = strategy.get("bgm_ranking", [])
            # Top 3 get 3x weight, others get 1x
            weights = []
            names   = [os.path.basename(p) for p in bgm_files]
            for name in names:
                rank = ranking.index(name) if name in ranking else len(ranking)
                weights.append(3.0 if rank < 3 else 1.0)
            return random.choices(bgm_files, weights=weights, k=1)[0]
        except Exception:
            pass

    return random.choice(bgm_files)


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

    # Stage 5: Generate/fetch 1 background image
    keywords = script_data.get("image_search_keywords", ["news"])
    image_dir = os.path.join(work_dir, "images")
    os.makedirs(image_dir, exist_ok=True)
    image_paths = []

    hf_cfg = config["images"].get("huggingface", {})
    if hf_cfg.get("enabled") and os.environ.get("HF_TOKEN"):
        from src.images.huggingface_client import HuggingFaceImageClient
        hf = HuggingFaceImageClient(os.environ["HF_TOKEN"])
        image_paths = hf.fetch_images_for_keywords(keywords, image_dir, total=1)

    if not image_paths:
        pexels_cfg = config["images"].get("pexels", {})
        if pexels_cfg.get("enabled") and os.environ.get("PEXELS_API_KEY"):
            pexels = PexelsClient(os.environ["PEXELS_API_KEY"])
            image_paths = pexels.fetch_images_for_keywords(keywords, image_dir, total=1)

    if not image_paths:
        unsplash_cfg = config["images"].get("unsplash", {})
        if unsplash_cfg.get("enabled") and os.environ.get("UNSPLASH_ACCESS_KEY"):
            unsplash = UnsplashClient(os.environ["UNSPLASH_ACCESS_KEY"])
            image_paths = unsplash.fetch_images_for_keywords(keywords, image_dir, total=1)

    if not image_paths:
        logger.warning("No images from APIs, generating fallback gradient for thumbnail")
        image_paths = generate_fallback_images(image_dir, 1)

    bg_image_path = image_paths[0] if image_paths else None

    # Stage 5.5a: Per-segment AI images for "image" visual_type segments
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

    # Stage 5.5b: Generate static character overlay (never transitions in video)
    from src.video.slide_gen import generate_slides, generate_character_overlay
    char_overlay_path = os.path.join(work_dir, "char_overlay.png")
    try:
        generate_character_overlay(config, char_overlay_path)
    except Exception as e:
        logger.warning(f"Character overlay generation failed (skipping): {e}")
        char_overlay_path = None

    # Stage 5.5c: Generate slides
    slide_dir = os.path.join(work_dir, "slides")
    os.makedirs(slide_dir, exist_ok=True)
    segs_with_meta = [
        {**seg, "segment_index": i}
        for i, seg in enumerate(script_data["script_segments"])
    ]
    slide_plate_paths, slide_content_paths = generate_slides(
        segs_with_meta,
        title=script_data["title"],
        config=config,
        output_dir=slide_dir,
        bg_image_path=bg_image_path,
        segment_images=segment_images,
    )
    per_slide_durations = [s["duration_sec"] for s in audio_segments]

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
        character_path=char_overlay_path,
        se_path=se_path,
        per_slide_durations=per_slide_durations,
    )

    # Stage 8: Generate thumbnail
    thumb_style = select_style()
    thumbnail_path = os.path.join(work_dir, "thumbnails", "thumb.jpg")
    generate_thumbnail(
        image_paths[0], script_data["thumbnail_title"], thumbnail_path, config,
        reaction_text=script_data.get("reaction_text", ""),
        style=thumb_style,
    )

    # Stage 8b: Render Shorts
    shorts_dir  = os.path.join(work_dir, "shorts")
    shorts_sub  = os.path.join(shorts_dir, "subs_shorts.ass")
    shorts_path = os.path.join(shorts_dir, f"{run_id}_shorts.mp4")
    os.makedirs(shorts_dir, exist_ok=True)

    if shorts_audio_segments:
        shorts_narration = os.path.join(shorts_dir, "shorts_narration.wav")
        compose_audio([s["audio_path"] for s in shorts_audio_segments], shorts_narration, ffmpeg_path)
        shorts_dur = min(sum(s["duration_sec"] for s in shorts_audio_segments) + 0.3, 58.0)
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
            for s_idx, s_seg in shorts_img_segs[:2]:
                s_prompt   = s_seg["image_prompt"]
                s_out_path = os.path.join(image_dir, f"shorts_seg_{s_idx:03d}.jpg")
                s_result = hf_s.generate_image(s_prompt, s_out_path, width=768, height=1344)
                if s_result:
                    shorts_segment_images[s_idx] = s_result
                    logger.info(f"Shorts segment {s_idx} portrait image generated")

        shorts_slide_dir = os.path.join(work_dir, "shorts_slides")
        shorts_plate_paths, shorts_content_paths = generate_shorts_slides(
            shorts_audio_segments,
            title=script_data["title"],
            config=config,
            output_dir=shorts_slide_dir,
            bg_image_path=bg_image_path,
            segment_images=shorts_segment_images,
        )
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
    try:
        generate_shorts_thumbnail(script_data["thumbnail_title"], shorts_thumb_path, config)
    except Exception as e:
        logger.warning(f"Shorts thumbnail generation failed: {e}")
        shorts_thumb_path = thumbnail_path

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
        "description": script_data["description"],
        "tags": script_data.get("tags", []),
        "category_id": channel.get("youtube_category_id"),
    }
    video_id = uploader.upload(video_path, thumbnail_path, meta)

    shorts_id = None
    if shorts_ok:
        shorts_desc = f"▶ 本編はこちら → https://youtu.be/{video_id}\n\n" + script_data["description"]
        shorts_meta = {
            **meta,
            "title": script_data["title"][:27] + " #Shorts",
            "description": shorts_desc,
            "tags": meta["tags"] + ["Shorts", "YouTubeShorts"],
        }
        try:
            shorts_id = uploader.upload(shorts_path, shorts_thumb_path, shorts_meta)
            logger.info(f"Shorts uploaded: https://youtu.be/{shorts_id}")
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
        for _item_attempt in range(6):
            try:
                script_data = writer.generate(item, channel=channel)
                break
            except Exception as e:
                cause = e.last_attempt.exception() if hasattr(e, "last_attempt") else e
                logger.warning(f"Script generation failed for '{item['title']}': {cause!r}")
                next_item = fetch_and_select_item(config, dedup, channel=channel)
                if not next_item:
                    raise RuntimeError("No more items available after script failures") from e
                dedup.mark_seen(next_item["id"])
                item = next_item
                if item.get("needs_translation"):
                    translator = ClaudeTranslator(config["groq"])
                    translated = translator.translate_batch([item["title"], item["summary"]])
                    item["title"] = translated[0]
                    item["summary"] = translated[1]
                logger.info(f"Retrying with next item: '{item['title']}'")
        if script_data is None:
            raise RuntimeError("All item attempts exhausted without generating a script")
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
