import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def _fwd(path: str) -> str:
    """Convert Windows backslashes to forward slashes for FFmpeg filter args."""
    return path.replace("\\", "/")


def compose_audio(audio_segment_paths: list[str], output_path: str, ffmpeg: str = "ffmpeg") -> str:
    """Concatenate WAV segments into a single WAV using FFmpeg concat demuxer."""
    list_file = output_path.replace(".wav", "_list.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in audio_segment_paths:
            f.write(f"file '{_fwd(os.path.abspath(p))}'\n")

    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", output_path]
    _run(cmd, "Audio concatenation")
    os.unlink(list_file)
    return output_path


def render_video(
    image_paths: list[str],
    audio_segments: list[dict],
    bgm_path: str,
    subtitle_path: str,
    output_path: str,
    config: dict,
) -> str:
    ffmpeg = config["video"].get("ffmpeg_path", "ffmpeg")
    display_dur = config["video"].get("display_duration_sec", 5)
    fps = config["video"].get("fps", 30)
    resolution = config["video"].get("resolution", "1920x1080")
    bgm_vol = config["video"].get("bgm_volume", 0.15)
    font_dir = _fwd(os.path.abspath(os.path.dirname(config["thumbnail"]["font_path"])))

    total_audio_dur = sum(s["duration_sec"] for s in audio_segments)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # Step 1: Merge narration audio
    audio_paths = [s["audio_path"] for s in audio_segments]
    merged_audio = os.path.join(os.path.dirname(output_path), "narration.wav")
    compose_audio(audio_paths, merged_audio, ffmpeg)

    # Step 2: Copy font to work_dir so FFmpeg can find it by relative path
    work_dir = os.path.dirname(output_path)
    font_src = config["thumbnail"]["font_path"]
    local_font_dir = os.path.join(work_dir, "fonts")
    os.makedirs(local_font_dir, exist_ok=True)
    local_font = os.path.join(local_font_dir, os.path.basename(font_src))
    if not os.path.exists(local_font):
        shutil.copy2(font_src, local_font)

    # Step 3: Pass 1 — images + audio + BGM → raw MP4 (no subtitles)
    raw_video = output_path.replace(".mp4", "_raw.mp4")

    inputs = []
    for img in image_paths:
        inputs += ["-loop", "1", "-t", str(display_dur), "-i", img]
    audio_idx = len(image_paths)
    bgm_idx = audio_idx + 1
    inputs += ["-i", merged_audio, "-i", bgm_path]

    n = len(image_paths)
    res_w, res_h = resolution.split("x")

    scale_parts = "".join(
        f"[{i}:v]scale={res_w}:{res_h}:force_original_aspect_ratio=increase,"
        f"crop={res_w}:{res_h},setsar=1,fps={fps}[v{i}];"
        for i in range(n)
    )
    concat_in = "".join(f"[v{i}]" for i in range(n))
    concat_part = f"{concat_in}concat=n={n}:v=1:a=0[slideshow];"
    video_trim = (
        f"[slideshow]trim=duration={total_audio_dur:.3f},setpts=PTS-STARTPTS[video_out]"
    )
    fade_out_start = max(0, total_audio_dur - 3)
    bgm_part = (
        f"[{bgm_idx}:a]aloop=loop=-1:size=2e9,"
        f"atrim=duration={total_audio_dur:.3f},"
        f"volume={bgm_vol},"
        f"afade=t=in:st=0:d=2,"
        f"afade=t=out:st={fade_out_start:.3f}:d=3[bgm_out]"
    )
    amix_part = f"[{audio_idx}:a][bgm_out]amix=inputs=2:duration=first:dropout_transition=2[audio_out]"
    filter_complex = scale_parts + concat_part + video_trim + ";" + bgm_part + ";" + amix_part

    cmd_pass1 = (
        [ffmpeg, "-y"]
        + inputs
        + [
            "-filter_complex", filter_complex,
            "-map", "[video_out]",
            "-map", "[audio_out]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            raw_video,
        ]
    )
    _run(cmd_pass1, "Video render pass1")

    # Step 4: Pass 2 — burn subtitles using relative path (CWD = subtitle dir)
    sub_dir = os.path.dirname(subtitle_path)
    sub_filename = os.path.basename(subtitle_path)
    font_dir_name = "fonts"  # relative to sub_dir after copy
    local_font_dir_for_sub = os.path.join(sub_dir, font_dir_name)
    os.makedirs(local_font_dir_for_sub, exist_ok=True)
    local_font_for_sub = os.path.join(local_font_dir_for_sub, os.path.basename(font_src))
    if not os.path.exists(local_font_for_sub):
        shutil.copy2(font_src, local_font_for_sub)

    cmd_pass2 = [
        ffmpeg, "-y", "-i", raw_video,
        "-vf", f"ass={sub_filename}:fontsdir={font_dir_name}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "copy",
        "-movflags", "+faststart",
        output_path,
    ]
    _run(cmd_pass2, "Subtitle burn pass2", cwd=sub_dir)
    logger.info(f"Video rendered: {output_path}")
    return output_path


def _run(cmd: list[str], label: str, cwd: str = None):
    logger.debug(f"{label} command: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd)
    if result.returncode != 0:
        logger.error(f"{label} stderr:\n{result.stderr[-3000:]}")
        raise RuntimeError(f"FFmpeg {label} failed (exit {result.returncode})")
