import logging
import os
import shutil
import subprocess
from pathlib import Path

from src.video.shorts_slide_gen import CONTENT_X1, CONTENT_Y1, CONTENT_W, CONTENT_H
from src.video.slide_render import render_slide_clip, merge_clips_sequential

logger = logging.getLogger(__name__)


def _fwd(path: str) -> str:
    return path.replace("\\", "/")


def _run(cmd: list[str], label: str, cwd: str = None):
    logger.debug(f"{label}: {' '.join(cmd)}")
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=cwd,
    )
    if result.returncode != 0:
        logger.error(f"{label} stderr:\n{result.stderr[-2000:]}")
        raise RuntimeError(f"FFmpeg {label} failed (exit {result.returncode})")


def render_shorts(
    plate_paths: list[str],
    content_paths: list[str | None],
    audio_path: str,
    bgm_path: str,
    subtitle_path: str,
    output_path: str,
    config: dict,
    max_duration: float = 58.0,
    per_slide_durations: list[float] | None = None,
) -> str:
    """Render vertical 9:16 Shorts video with two-layer compositing.

    plate_paths   — static chrome frames (character at fixed position)
    content_paths — animated content images with zoompan (None = skip overlay)
    """
    ffmpeg  = config["video"].get("ffmpeg_path", "ffmpeg")
    fps     = config["video"].get("fps", 30)
    bgm_vol = config["video"].get("bgm_volume", 0.05)

    VW, VH = 1080, 1920
    font_src = config["thumbnail"]["font_path"]
    work_dir = os.path.dirname(output_path)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    local_font = os.path.join(work_dir, "fonts", os.path.basename(font_src))
    os.makedirs(os.path.dirname(local_font), exist_ok=True)
    if not os.path.exists(local_font):
        shutil.copy2(font_src, local_font)

    n        = max(len(plate_paths), 1)
    fade_dur = 0.25
    display_dur = 3.0

    slide_durs = per_slide_durations if (per_slide_durations and len(per_slide_durations) == n) else [display_dur] * n

    _SHORTS_TRANSITIONS = [
        "fade", "wipeleft", "wiperight", "dissolve",
        "fadeblack", "smoothleft", "smoothright",
        "vertopen", "vertclose", "slideleft",
    ]

    # ── Render each slide as an individual clip (input count <= 2) ────────────
    # 全スライドで統一して「表示時間 + 次のxfade用の予備fade_dur」の長さを
    # 持たせる(composer.pyと同じ不変量)。
    clips_dir = os.path.join(work_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)

    clip_paths = []
    for i in range(n):
        clip_dur = slide_durs[i] + fade_dur
        content_path = content_paths[i] if i < len(content_paths) else None
        clip_out = os.path.join(clips_dir, f"clip_{i:03d}.mp4")
        render_slide_clip(
            ffmpeg, plate_paths[i], content_path, clip_dur, fps,
            VW, VH, CONTENT_X1, CONTENT_Y1, CONTENT_W, CONTENT_H,
            clip_out,
        )
        clip_paths.append(clip_out)

    # ── Merge clips via sequential xfade (input count always 2) ───────────────
    if n == 1:
        slideshow_path = clip_paths[0]
    else:
        slideshow_path, _ = merge_clips_sequential(
            ffmpeg, clip_paths, slide_durs, fade_dur, _SHORTS_TRANSITIONS, clips_dir,
        )

    # ── Final composite — narration + bgm mix ──────────────────────────────────
    # character overlayは既にplateに焼き込み済みのため、videoの合成は
    # trimのみ。入力は video 1本 + narration + bgm の3本のみでn非依存。
    inputs = [
        "-i", slideshow_path,
        "-t", str(max_duration), "-i", audio_path,
        "-i", bgm_path,
    ]
    parts = [f"[0:v]trim=duration={max_duration:.3f},setpts=PTS-STARTPTS[trimmed_v]"]

    parts.append(
        f"[2:a]aloop=loop=-1:size=2e9,"
        f"atrim=duration={max_duration:.3f},"
        f"volume={bgm_vol},"
        f"afade=t=in:st=0:d=1,"
        f"afade=t=out:st={max_duration - 2:.3f}:d=2[bgm_out]"
    )
    parts.append(
        f"[1:a][bgm_out]"
        f"amix=inputs=2:duration=first:dropout_transition=1:normalize=0[audio_out]"
    )

    filter_complex = ";".join(parts)

    raw_shorts = output_path.replace(".mp4", "_raw.mp4")
    cmd_pass1 = (
        [ffmpeg, "-y"]
        + inputs
        + [
            "-filter_complex", filter_complex,
            "-map", "[trimmed_v]",
            "-map", "[audio_out]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            raw_shorts,
        ]
    )
    _run(cmd_pass1, "Shorts pass1")
    shutil.rmtree(clips_dir, ignore_errors=True)

    # Pass2: subtitles
    sub_dir   = os.path.dirname(subtitle_path)
    ch_accent = config.get("active_channel", {}).get("accent_color", [50, 200, 80])
    accent_hex = "{:02X}{:02X}{:02X}".format(*ch_accent[:3])
    vf = f"drawbox=x=0:y=ih-8:w=iw:h=8:color=0x{accent_hex}:t=fill,ass=subs_shorts.ass:fontsdir=fonts"
    cmd_pass2 = [
        ffmpeg, "-y", "-i", raw_shorts,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "copy",
        "-movflags", "+faststart",
        output_path,
    ]
    _run(cmd_pass2, "Shorts pass2", cwd=sub_dir)

    try:
        os.unlink(raw_shorts)
    except Exception:
        pass

    logger.info(f"Shorts rendered: {output_path}")
    return output_path
