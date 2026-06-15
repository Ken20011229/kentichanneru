import logging
import os
import subprocess
from pathlib import Path

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
    image_paths: list[str],
    audio_path: str,
    bgm_path: str,
    subtitle_path: str,
    output_path: str,
    config: dict,
    max_duration: float = 58.0,
    per_slide_durations: list[float] | None = None,
) -> str:
    """Render a vertical 9:16 YouTube Shorts video (≤58s).

    When per_slide_durations is provided each slide plays for its exact audio
    duration and xfade offsets are calculated from cumulative timings.
    """
    ffmpeg   = config["video"].get("ffmpeg_path", "ffmpeg")
    fps      = config["video"].get("fps", 30)
    bgm_vol  = config["video"].get("bgm_volume", 0.05)

    W, H = 1080, 1920
    font_src   = config["thumbnail"]["font_path"]
    work_dir   = os.path.dirname(output_path)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    import shutil
    local_font = os.path.join(work_dir, "fonts", os.path.basename(font_src))
    os.makedirs(os.path.dirname(local_font), exist_ok=True)
    if not os.path.exists(local_font):
        shutil.copy2(font_src, local_font)

    n = max(len(image_paths), 1)
    fade_dur    = 0.3
    display_dur = 3  # fallback when per_slide_durations not given

    # ── Build inputs ──────────────────────────────────────────────
    inputs = []
    if per_slide_durations and len(per_slide_durations) == n:
        for i, img in enumerate(image_paths):
            dur = per_slide_durations[i] + fade_dur
            inputs += ["-loop", "1", "-t", f"{dur:.3f}", "-i", img]
    else:
        for img in image_paths:
            inputs += ["-loop", "1", "-t", str(display_dur), "-i", img]
    audio_idx = n
    inputs += ["-t", str(max_duration), "-i", audio_path]
    bgm_idx = n + 1
    inputs += ["-i", bgm_path]

    # ── filter_complex: scale to 9:16 with crop ──────────────────
    parts = []
    for i in range(n):
        parts.append(
            f"[{i}:v]"
            f"scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},"
            f"fps={fps},setsar=1"
            f"[v{i}]"
        )

    if n == 1:
        parts.append(f"[v0]trim=duration={max_duration:.3f},setpts=PTS-STARTPTS[slideshow]")
    elif per_slide_durations and len(per_slide_durations) == n:
        # Per-slide timing: xfade offsets follow cumulative audio durations
        cumulative = 0.0
        prev = "v0"
        for i in range(n - 1):
            nxt = f"v{i+1}"
            out = f"xf{i+1}" if i < n - 2 else "slideshow"
            cumulative += per_slide_durations[i]
            offset = max(cumulative - fade_dur, 0.0)
            parts.append(
                f"[{prev}][{nxt}]xfade=transition=fade:duration={fade_dur:.3f}:offset={offset:.3f}[{out}]"
            )
            prev = out
        parts.append(f"[slideshow]trim=duration={max_duration:.3f},setpts=PTS-STARTPTS[trimmed_v]")
    else:
        prev = "v0"
        for i in range(n - 1):
            nxt = f"v{i+1}"
            out = f"xf{i+1}" if i < n - 2 else "slideshow"
            offset = (i + 1) * (display_dur - fade_dur)
            parts.append(
                f"[{prev}][{nxt}]xfade=transition=fade:duration={fade_dur:.3f}:offset={offset:.3f}[{out}]"
            )
            prev = out
        parts.append(f"[slideshow]trim=duration={max_duration:.3f},setpts=PTS-STARTPTS[trimmed_v]")

    if n > 1:
        video_label = "trimmed_v"
    else:
        video_label = "slideshow"

    # BGM mix
    parts.append(
        f"[{bgm_idx}:a]aloop=loop=-1:size=2e9,"
        f"atrim=duration={max_duration:.3f},"
        f"volume={bgm_vol},"
        f"afade=t=in:st=0:d=1,"
        f"afade=t=out:st={max_duration-2:.3f}:d=2[bgm_out]"
    )
    parts.append(
        f"[{audio_idx}:a][bgm_out]"
        f"amix=inputs=2:duration=first:dropout_transition=1:normalize=0[audio_out]"
    )

    filter_complex = ";".join(parts)

    raw_shorts = output_path.replace(".mp4", "_raw.mp4")
    cmd_pass1 = (
        [ffmpeg, "-y"]
        + inputs
        + [
            "-filter_complex", filter_complex,
            "-map", f"[{video_label}]",
            "-map", "[audio_out]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            raw_shorts,
        ]
    )
    _run(cmd_pass1, "Shorts pass1")

    # Pass2: subtitles with larger font for vertical
    sub_dir  = os.path.dirname(subtitle_path)
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
