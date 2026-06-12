import logging
import math
import os
import shutil
import tempfile
import uuid
from datetime import datetime

from src.config_loader import load_config
from src.deduplicator import Deduplicator
from src.fetcher.aggregator import fetch_and_select_item
from src.images.pexels_client import PexelsClient
from src.images.unsplash_client import UnsplashClient
from src.script_gen.claude_scriptwriter import ClaudeScriptWriter
from src.translator.claude_translator import ClaudeTranslator
from src.tts.voicevox_client import VoicevoxClient
from src.uploader.youtube_uploader import YouTubeUploader
from src.video.composer import render_video
from src.video.subtitle_gen import generate_ass
from src.video.thumbnail_gen import generate_thumbnail

logger = logging.getLogger(__name__)


def run_pipeline(config: dict = None, skip_upload: bool = False):
    if config is None:
        config = load_config()

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    # Use ASCII-only temp dir to avoid FFmpeg issues with non-ASCII paths
    work_dir = tempfile.mkdtemp(prefix=f"ytauto_{run_id}_")
    logger.info(f"=== Pipeline run started: {run_id} ===")

    try:
        # Stage 1: Fetch and select content
        dedup = Deduplicator(config["deduplication"])
        item = fetch_and_select_item(config, dedup)
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
        script_data = writer.generate(item)
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

        # Stage 5: Fetch images (Pexels with Unsplash fallback)
        keywords = script_data.get("image_search_keywords", ["news"])
        image_dir = os.path.join(work_dir, "images")
        os.makedirs(image_dir, exist_ok=True)
        total_images = config["images"].get("images_per_video", 8)

        image_paths = []
        pexels_cfg = config["images"].get("pexels", {})
        if pexels_cfg.get("enabled") and os.environ.get("PEXELS_API_KEY"):
            pexels = PexelsClient(os.environ["PEXELS_API_KEY"])
            image_paths = pexels.fetch_images_for_keywords(keywords, image_dir, total=total_images)

        if len(image_paths) < total_images:
            unsplash_cfg = config["images"].get("unsplash", {})
            if unsplash_cfg.get("enabled") and os.environ.get("UNSPLASH_ACCESS_KEY"):
                unsplash = UnsplashClient(os.environ["UNSPLASH_ACCESS_KEY"])
                remaining = total_images - len(image_paths)
                image_paths += unsplash.fetch_images_for_keywords(keywords, image_dir, total=remaining)

        if len(image_paths) < 3:
            raise RuntimeError(f"Not enough images fetched ({len(image_paths)}). Check API keys.")

        # Cycle images to cover the full audio duration
        display_dur = config["video"].get("display_duration_sec", 5)
        total_audio_dur = sum(s["duration_sec"] for s in audio_segments)
        needed = max(len(audio_segments), math.ceil(total_audio_dur / display_dur))
        while len(image_paths) < needed:
            image_paths = (image_paths * 2)[:needed]

        # Stage 6: Generate subtitles
        subtitle_path = os.path.join(work_dir, "subtitles", "subs.ass")
        generate_ass(audio_segments, subtitle_path)

        # Stage 7: Render video
        video_path = os.path.join(work_dir, "video", f"{run_id}.mp4")
        bgm_path = os.path.join("assets", "bgm", "background.mp3")
        render_video(image_paths, audio_segments, bgm_path, subtitle_path, video_path, config)

        # Stage 8: Generate thumbnail
        thumbnail_path = os.path.join(work_dir, "thumbnails", "thumb.jpg")
        generate_thumbnail(image_paths[0], script_data["thumbnail_title"], thumbnail_path, config)

        # Stage 9: Upload to YouTube
        if skip_upload:
            logger.info(f"=== Pipeline run complete (upload skipped) ===")
            logger.info(f"  Video:     {video_path}")
            logger.info(f"  Thumbnail: {thumbnail_path}")
            logger.info(f"  Title:     {script_data['title']}")
            return None

        uploader = YouTubeUploader(config)
        video_id = uploader.upload(
            video_path,
            thumbnail_path,
            {
                "title": script_data["title"],
                "description": script_data["description"],
                "tags": script_data.get("tags", []),
            },
        )

        logger.info(f"=== Pipeline run complete: https://youtu.be/{video_id} ===")
        return video_id

    except Exception as e:
        logger.exception(f"Pipeline run {run_id} failed: {e}")
        raise
    finally:
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
            logger.debug(f"Cleaned up work dir: {work_dir}")
