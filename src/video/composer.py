import logging
import os
import shutil
import subprocess
from pathlib import Path

from src.video.slide_gen import CONTENT_X1, CONTENT_Y1, CONTENT_W, CONTENT_H
from src.video.slide_render import render_slide_clip, merge_clips_sequential

logger = logging.getLogger(__name__)


def _fwd(path: str) -> str:
    return path.replace("\\", "/")


def compose_audio(audio_segment_paths: list[str], output_path: str, ffmpeg: str = "ffmpeg") -> str:
    list_file = output_path.replace(".wav", "_list.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in audio_segment_paths:
            f.write(f"file '{_fwd(os.path.abspath(p))}'\n")
    _run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", output_path],
         "Audio concatenation")
    os.unlink(list_file)
    return output_path


def _ensure_se(ffmpeg: str, se_path: str) -> bool:
    Path(se_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-y",
        "-f", "lavfi", "-i", "sine=frequency=523:duration=0.18",
        "-f", "lavfi", "-i", "sine=frequency=659:duration=0.22",
        "-filter_complex",
        "[0:a][1:a]concat=n=2:v=0:a=1,volume=0.65,afade=t=out:st=0.34:d=0.06[out]",
        "-map", "[out]", se_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        logger.warning(f"SE generation failed (skipping): {result.stderr[-300:]}")
        return False
    return True


def render_video(
    plate_paths: list[str],
    content_paths: list[str | None],
    audio_segments: list[dict],
    bgm_path: str,
    subtitle_path: str,
    output_path: str,
    config: dict,
    character_path: str = None,
    se_path: str = None,
    per_slide_durations: list[float] | None = None,
) -> str:
    """Render final video using a three-layer approach.

    plate_paths      — background + badge frames (no characters)
    content_paths    — per-segment content images animated with zoompan (None = skip)
    character_path   — static RGBA PNG overlaid permanently on top (never transitions)

    Characters are composited last so they never move or fade between slides.
    """
    ffmpeg    = config["video"].get("ffmpeg_path", "ffmpeg")
    fps       = config["video"].get("fps", 30)
    resolution = config["video"].get("resolution", "1920x1080")
    bgm_vol   = config["video"].get("bgm_volume", 0.15)
    fade_dur  = config["video"].get("xfade_duration", 0.5)
    se_vol    = config.get("se", {}).get("volume", 0.70)
    display_dur = config["video"].get("display_duration_sec", 5)

    res_w, res_h = resolution.split("x")
    n           = len(plate_paths)
    total_dur   = sum(s["duration_sec"] for s in audio_segments)
    fade_start  = max(0, total_dur - 3)

    # Per-slide durations (or uniform fallback)
    slide_durs = per_slide_durations if per_slide_durations else [display_dur] * n

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Merge narration ───────────────────────────────────────────────
    merged_audio = os.path.join(os.path.dirname(output_path), "narration.wav")
    compose_audio([s["audio_path"] for s in audio_segments], merged_audio, ffmpeg)

    # ── Step 2: Copy font ─────────────────────────────────────────────────────
    work_dir   = os.path.dirname(output_path)
    font_src   = config["thumbnail"]["font_path"]
    local_font = os.path.join(work_dir, "fonts", os.path.basename(font_src))
    os.makedirs(os.path.dirname(local_font), exist_ok=True)
    if not os.path.exists(local_font):
        shutil.copy2(font_src, local_font)

    # ── Step 3: Ensure SE exists ──────────────────────────────────────────────
    if se_path and not Path(se_path).exists():
        _ensure_se(ffmpeg, se_path)
    has_se = bool(se_path and Path(se_path).exists())

    # ── Step 4: Render each slide as an individual clip (input count <= 2) ────
    # 全スライドで統一して「表示時間 + 次のxfade用の予備fade_dur」の長さを
    # 持たせる(最後のスライドも例外にしない)。
    clips_dir = os.path.join(work_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)

    clip_paths = []
    for i in range(n):
        clip_dur = slide_durs[i] + fade_dur
        clip_out = os.path.join(clips_dir, f"clip_{i:03d}.mp4")
        render_slide_clip(
            ffmpeg, plate_paths[i], content_paths[i], clip_dur, fps,
            int(res_w), int(res_h), CONTENT_X1, CONTENT_Y1, CONTENT_W, CONTENT_H,
            clip_out,
        )
        clip_paths.append(clip_out)

    # ── Step 5: Merge clips via sequential xfade (input count always 2) ───────
    # Only fade-family transitions for background — content/text animate independently
    _TRANSITIONS = [
        "fade", "dissolve", "fadeblack", "pixelize",
        "fade", "dissolve", "fadeblack",
        "circleopen", "circleclose", "radial",
        "fade", "dissolve",
    ]

    if n == 1:
        slideshow_path = clip_paths[0]
    else:
        slideshow_path, _ = merge_clips_sequential(
            ffmpeg, clip_paths, slide_durs, fade_dur, _TRANSITIONS, clips_dir,
        )

    # ── Step 6: Final composite — character overlay + audio mix ───────────────
    # 入力は video 1本 + character(あれば)1本 + narration/bgm/se の高々5本の
    # みで、xfadeチェーンを含まないためスライド枚数nに依存せずメモリは一定。
    inputs = ["-i", slideshow_path]
    parts  = [f"[0:v]trim=duration={total_dur:.3f},setpts=PTS-STARTPTS[pre_char]"]

    char_idx = None
    if character_path:
        char_idx = 1
        inputs += ["-loop", "1", "-t", f"{total_dur:.3f}", "-i", character_path]
        parts.append(f"[{char_idx}:v]setsar=1[char_layer]")
        parts.append(f"[pre_char][char_layer]overlay=0:0:shortest=1[trimmed]")
    else:
        parts.append(f"[pre_char]null[trimmed]")

    audio_idx = (char_idx + 1) if char_idx is not None else 1
    inputs += ["-i", merged_audio]
    bgm_idx = audio_idx + 1
    inputs += ["-i", bgm_path]

    se_idx = None
    if has_se:
        se_idx = bgm_idx + 1
        inputs += ["-i", se_path]

    # BGM
    parts.append(
        f"[{bgm_idx}:a]aloop=loop=-1:size=2e9,"
        f"atrim=duration={total_dur:.3f},"
        f"volume={bgm_vol},"
        f"afade=t=in:st=0:d=2,"
        f"afade=t=out:st={fade_start:.3f}:d=3[bgm_out]"
    )

    # Audio mix
    if has_se:
        parts.append(f"[{se_idx}:a]volume={se_vol}[se_out]")
        parts.append(
            f"[{audio_idx}:a][bgm_out][se_out]"
            f"amix=inputs=3:duration=first:dropout_transition=2:normalize=0[audio_out]"
        )
    else:
        parts.append(
            f"[{audio_idx}:a][bgm_out]"
            f"amix=inputs=2:duration=first:dropout_transition=2:normalize=0[audio_out]"
        )

    filter_complex = ";".join(parts)

    raw_video = output_path.replace(".mp4", "_raw.mp4")
    cmd_pass1 = (
        [ffmpeg, "-y"]
        + inputs
        + [
            "-filter_complex", filter_complex,
            "-map", "[trimmed]",
            "-map", "[audio_out]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            raw_video,
        ]
    )
    _run(cmd_pass1, "Video render pass1")
    shutil.rmtree(clips_dir, ignore_errors=True)

    # ── Step 7: Pass 2 — subtitle burn + bottom accent line ───────────────────
    sub_dir  = os.path.dirname(subtitle_path)
    sub_font = os.path.join(sub_dir, "fonts", os.path.basename(font_src))
    os.makedirs(os.path.dirname(sub_font), exist_ok=True)
    if not os.path.exists(sub_font):
        shutil.copy2(font_src, sub_font)

    ch_accent  = config.get("active_channel", {}).get("accent_color", [50, 200, 80])
    accent_hex = "{:02X}{:02X}{:02X}".format(*ch_accent[:3])
    vf_pass2   = f"drawbox=x=0:y=ih-6:w=iw:h=6:color=0x{accent_hex}:t=fill,ass=subs.ass:fontsdir=fonts"
    cmd_pass2  = [
        ffmpeg, "-y", "-i", raw_video,
        "-vf", vf_pass2,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "copy",
        "-movflags", "+faststart",
        output_path,
    ]
    _run(cmd_pass2, "Subtitle burn pass2", cwd=sub_dir)
    logger.info(f"Video rendered: {output_path}")
    return output_path


def _run(cmd: list[str], label: str, cwd: str = None):
    logger.debug(f"{label}: {' '.join(cmd)}")
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=cwd,
    )
    if result.returncode != 0:
        logger.error(f"{label} stderr:\n{result.stderr[-3000:]}")
        raise RuntimeError(f"FFmpeg {label} failed (exit {result.returncode})")
