import logging
import os
import shutil
import subprocess
from pathlib import Path

from src.video.slide_gen import CONTENT_X1, CONTENT_Y1, CONTENT_W, CONTENT_H

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

    # ── Step 4: Build FFmpeg inputs ───────────────────────────────────────────
    # Group 1: plate images (n inputs, indices 0..n-1)
    # Group 2: non-None content images (m inputs, indices n..n+m-1)
    # Group 3: audio, bgm, SE

    content_indices = {}   # slide_i → ffmpeg_input_index for its content image
    inputs = []

    for i in range(n):
        clip_dur = slide_durs[i] + (fade_dur if i < n - 1 else 0)
        inputs += ["-loop", "1", "-t", f"{clip_dur:.3f}", "-i", plate_paths[i]]

    content_ffmpeg_start = n
    j = 0
    for i in range(n):
        if content_paths[i] is not None:
            clip_dur = slide_durs[i] + (fade_dur if i < n - 1 else 0)
            inputs += ["-loop", "1", "-t", f"{clip_dur:.3f}", "-i", content_paths[i]]
            content_indices[i] = content_ffmpeg_start + j
            j += 1
    m = j

    audio_idx = n + m
    inputs += ["-i", merged_audio]
    bgm_idx = n + m + 1
    inputs += ["-i", bgm_path]

    se_idx = None
    if has_se:
        se_idx = n + m + 2
        inputs += ["-i", se_path]

    # Character overlay: permanent static PNG (overlaid last, never transitions)
    char_idx = None
    if character_path:
        char_idx = n + m + 2 + (1 if has_se else 0)
        inputs += ["-loop", "1", "-t", f"{total_dur:.3f}", "-i", character_path]

    # ── Step 5: Build filter_complex ─────────────────────────────────────────
    # Only fade-family transitions for background — content/text animate independently
    _TRANSITIONS = [
        "fade", "dissolve", "fadeblack", "pixelize",
        "fade", "dissolve", "fadeblack",
        "circleopen", "circleclose", "radial",
        "fade", "dissolve",
    ]

    parts = []

    # Static scale for each plate
    for i in range(n):
        parts.append(
            f"[{i}:v]"
            f"scale={res_w}:{res_h}:force_original_aspect_ratio=increase,"
            f"crop={res_w}:{res_h},"
            f"fps={fps},setsar=1"
            f"[plate_{i}]"
        )

    # Zoompan animation for each content image
    for i, ci in content_indices.items():
        clip_dur = slide_durs[i] + (fade_dur if i < n - 1 else 0)
        D        = max(1, int(clip_dur * fps))
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

    # Compose: overlay content onto plate (or use plate directly for intro)
    for i in range(n):
        if i in content_indices:
            parts.append(
                f"[plate_{i}][anim_{i}]"
                f"overlay=x={CONTENT_X1}:y={CONTENT_Y1}:shortest=1"
                f"[comp_{i}]"
            )
        else:
            parts.append(f"[plate_{i}]null[comp_{i}]")

    # xfade chain between composed frames
    if n == 1:
        parts.append(f"[comp_0]trim=duration={total_dur:.3f},setpts=PTS-STARTPTS[pre_char]")
    else:
        prev = "comp_0"
        cumulative = 0.0
        for i in range(n - 1):
            nxt    = f"comp_{i + 1}"
            out    = f"xf_{i + 1}" if i < n - 2 else "slideshow"
            cumulative += slide_durs[i]
            offset = max(cumulative - fade_dur, 0.0)
            trans  = _TRANSITIONS[i % len(_TRANSITIONS)]
            parts.append(
                f"[{prev}][{nxt}]xfade=transition={trans}:duration={fade_dur:.3f}:offset={offset:.3f}[{out}]"
            )
            prev = out
        parts.append(f"[slideshow]trim=duration={total_dur:.3f},setpts=PTS-STARTPTS[pre_char]")

    # Character overlay: composited on top of slideshow, never transitions
    if char_idx is not None:
        parts.append(f"[{char_idx}:v]setsar=1[char_layer]")
        parts.append(f"[pre_char][char_layer]overlay=0:0:shortest=1[trimmed]")
    else:
        parts.append(f"[pre_char]null[trimmed]")

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

    # ── Step 6: Pass 1 — compose video + audio ────────────────────────────────
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
