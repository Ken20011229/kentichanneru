import logging
import os
import subprocess
from pathlib import Path

from src.video.shorts_slide_gen import CONTENT_X1, CONTENT_Y1, CONTENT_W, CONTENT_H

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

    import shutil
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

    # ── Build inputs ──────────────────────────────────────────────────────────
    # Group 1: plate images (indices 0..n-1)
    # Group 2: non-None content images (indices n..n+m-1)
    # Group 3: audio, bgm

    content_indices: dict[int, int] = {}
    inputs = []

    for i in range(n):
        clip_dur = slide_durs[i] + (fade_dur if i < n - 1 else 0)
        inputs += ["-loop", "1", "-t", f"{clip_dur:.3f}", "-i", plate_paths[i]]

    content_start = n
    j = 0
    for i in range(n):
        if i < len(content_paths) and content_paths[i] is not None:
            clip_dur = slide_durs[i] + (fade_dur if i < n - 1 else 0)
            inputs += ["-loop", "1", "-t", f"{clip_dur:.3f}", "-i", content_paths[i]]
            content_indices[i] = content_start + j
            j += 1
    m = j

    audio_idx = n + m
    inputs += ["-t", str(max_duration), "-i", audio_path]
    bgm_idx = n + m + 1
    inputs += ["-i", bgm_path]

    # ── filter_complex ────────────────────────────────────────────────────────
    parts = []

    # Static scale for each plate (no Ken Burns)
    for i in range(n):
        parts.append(
            f"[{i}:v]"
            f"scale={VW}:{VH}:force_original_aspect_ratio=increase,"
            f"crop={VW}:{VH},"
            f"fps={fps},setsar=1"
            f"[plate_{i}]"
        )

    # Zoompan animation for content images
    for i, ci in content_indices.items():
        clip_dur  = slide_durs[i] + (fade_dur if i < n - 1 else 0)
        D         = max(1, int(clip_dur * fps))
        zoom_rate = 0.04 / D
        parts.append(
            f"[{ci}:v]"
            f"scale={CONTENT_W}:{CONTENT_H}:force_original_aspect_ratio=increase,"
            f"crop={CONTENT_W}:{CONTENT_H},"
            f"fps={fps},"
            f"zoompan="
            f"z='min(zoom+{zoom_rate:.6f},1.04)':"
            f"x='(iw-iw/zoom)/2':"
            f"y='(ih-ih/zoom)/2':"
            f"d={D}:s={CONTENT_W}x{CONTENT_H}:fps={fps},"
            f"setsar=1"
            f"[anim_{i}]"
        )

    # Compose: overlay content onto plate
    for i in range(n):
        if i in content_indices:
            parts.append(
                f"[plate_{i}][anim_{i}]"
                f"overlay=x={CONTENT_X1}:y={CONTENT_Y1}:shortest=1"
                f"[comp_{i}]"
            )
        else:
            parts.append(f"[plate_{i}]null[comp_{i}]")

    # xfade chain
    if n == 1:
        parts.append(f"[comp_0]trim=duration={max_duration:.3f},setpts=PTS-STARTPTS[trimmed_v]")
    else:
        cumulative = 0.0
        prev = "comp_0"
        for i in range(n - 1):
            nxt        = f"comp_{i + 1}"
            out        = f"xf_{i + 1}" if i < n - 2 else "slideshow"
            cumulative += slide_durs[i]
            offset     = max(cumulative - fade_dur, 0.0)
            trans      = _SHORTS_TRANSITIONS[i % len(_SHORTS_TRANSITIONS)]
            parts.append(
                f"[{prev}][{nxt}]xfade=transition={trans}:duration={fade_dur:.3f}:offset={offset:.3f}[{out}]"
            )
            prev = out
        parts.append(f"[slideshow]trim=duration={max_duration:.3f},setpts=PTS-STARTPTS[trimmed_v]")

    # BGM
    parts.append(
        f"[{bgm_idx}:a]aloop=loop=-1:size=2e9,"
        f"atrim=duration={max_duration:.3f},"
        f"volume={bgm_vol},"
        f"afade=t=in:st=0:d=1,"
        f"afade=t=out:st={max_duration - 2:.3f}:d=2[bgm_out]"
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
            "-map", "[trimmed_v]",
            "-map", "[audio_out]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            raw_shorts,
        ]
    )
    _run(cmd_pass1, "Shorts pass1")

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
