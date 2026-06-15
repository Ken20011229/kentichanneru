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

logger = logging.getLogger(__name__)


def _pick_bgm(bgm_files: list, bgm_dir: str) -> str:
    """Pick BGM: prefer top-ranked from strategy.json, with weighted random fallback."""
    if not bgm_files:
        return os.path.join(bgm_dir, "background.mp3")

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


def run_pipeline(config: dict = None, skip_upload: bool = False):
    if config is None:
        config = load_config()

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    # Use ASCII-only temp dir to avoid FFmpeg issues with non-ASCII paths
    work_dir = tempfile.mkdtemp(prefix=f"ytauto_{run_id}_")

    # Select the active channel for this run (round-robin rotation)
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
        script_data = writer.generate(item, channel=channel)
        logger.info(f"Script generated: '{script_data['title']}' ({len(script_data['script_segments'])} segments)")

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

        # Stage 5: Fetch 1 image (for thumbnail background only)
        keywords = script_data.get("image_search_keywords", ["news"])
        image_dir = os.path.join(work_dir, "images")
        os.makedirs(image_dir, exist_ok=True)

        image_paths = []
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

        # Stage 5.5: Generate slides (one per script segment — replaces Pexels for video body)
        from src.video.slide_gen import generate_slides
        slide_dir = os.path.join(work_dir, "slides")
        os.makedirs(slide_dir, exist_ok=True)
        segs_with_meta = [
            {**seg, "segment_index": i}
            for i, seg in enumerate(script_data["script_segments"])
        ]
        slide_paths = generate_slides(
            segs_with_meta,
            title=script_data["title"],
            config=config,
            output_dir=slide_dir,
        )
        per_slide_durations = [s["duration_sec"] for s in audio_segments]

        # Stage 6: Generate subtitles
        subtitle_path = os.path.join(work_dir, "subtitles", "subs.ass")
        generate_ass(audio_segments, subtitle_path, channel=channel)

        # Stage 7: Render video
        video_path = os.path.join(work_dir, "video", f"{run_id}.mp4")
        bgm_dir   = os.path.join("assets", "bgm")
        bgm_files = glob.glob(os.path.join(bgm_dir, "*.mp3"))
        bgm_path  = _pick_bgm(bgm_files, bgm_dir)
        logger.info(f"BGM: {os.path.basename(bgm_path)}")
        char_path  = config.get("character", {}).get("image_path", "")
        se_path    = config.get("se", {}).get("intro", os.path.join("assets", "se", "intro.mp3"))
        render_video(
            slide_paths, audio_segments, bgm_path, subtitle_path, video_path, config,
            character_path=None,  # character is baked into each slide
            se_path=se_path,
            per_slide_durations=per_slide_durations,
        )

        # Stage 8: Generate thumbnail
        thumbnail_path = os.path.join(work_dir, "thumbnails", "thumb.jpg")
        generate_thumbnail(image_paths[0], script_data["thumbnail_title"], thumbnail_path, config)

        # Stage 8b: Render Shorts (≤58s vertical version with dedicated slides)
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

            # Generate dedicated vertical slides (1080×1920) for Shorts
            shorts_slide_dir = os.path.join(work_dir, "shorts_slides")
            shorts_slide_paths = generate_shorts_slides(
                shorts_audio_segments,
                title=script_data["title"],
                config=config,
                output_dir=shorts_slide_dir,
            )
            per_shorts_durations = [s["duration_sec"] for s in shorts_audio_segments]
        else:
            shorts_dur = 58.0
            generate_shorts_ass(audio_segments, shorts_sub, channel=channel, max_dur=shorts_dur)
            shorts_audio_path = os.path.join(work_dir, "video", "narration.wav")
            # Fall back to horizontal slides cycled
            shorts_slide_paths = (slide_paths * 20)[:max(4, math.ceil(shorts_dur / 3) + 1)]
            per_shorts_durations = None

        # Generate dedicated vertical Shorts thumbnail
        shorts_thumb_path = os.path.join(shorts_dir, "shorts_thumb.jpg")
        try:
            generate_shorts_thumbnail(
                script_data["thumbnail_title"],
                shorts_thumb_path,
                config,
            )
        except Exception as e:
            logger.warning(f"Shorts thumbnail generation failed: {e}")
            shorts_thumb_path = thumbnail_path  # fall back to main thumbnail

        try:
            render_shorts(
                shorts_slide_paths, shorts_audio_path, bgm_path, shorts_sub, shorts_path, config,
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

        # Upload Shorts with #Shorts tag in title
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

        # Log video for analytics/self-improvement
        log_video(video_id, {
            "shorts_id":    shorts_id,
            "channel_id":   channel.get("id"),
            "channel_name": channel.get("name"),
            "title":        script_data["title"],
            "bgm":          os.path.basename(bgm_path),
        })

        logger.info(f"=== Pipeline run complete: https://youtu.be/{video_id} ===")
        return video_id

    except Exception as e:
        logger.exception(f"Pipeline run {run_id} failed: {e}")
        raise
    finally:
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
            logger.debug(f"Cleaned up work dir: {work_dir}")
