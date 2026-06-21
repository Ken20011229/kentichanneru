import logging
import os
import shutil
import subprocess
from pathlib import Path

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
    """Generate a two-tone notification chime if the SE file is missing."""
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
    image_paths: list[str],
    audio_segments: list[dict],
    bgm_path: str,
    subtitle_path: str,
    output_path: str,
    config: dict,
    character_path: str = None,
    se_path: str = None,
    per_slide_durations: list[float] | None = None,
) -> str:
    """Render final video.

    When per_slide_durations is provided, each slide uses its corresponding audio
    duration and Ken Burns animation is replaced with a static scale filter.
    Character overlay is also suppressed (assumed baked into slides).
    """
    ffmpeg       = config["video"].get("ffmpeg_path", "ffmpeg")
    display_dur  = config["video"].get("display_duration_sec", 5)
    fps          = config["video"].get("fps", 30)
    resolution   = config["video"].get("resolution", "1920x1080")
    bgm_vol      = config["video"].get("bgm_volume", 0.15)
    fade_dur     = config["video"].get("xfade_duration", 0.5)
    se_vol       = config.get("se", {}).get("volume", 0.70)
    char_h       = config.get("character", {}).get("overlay_size", 480)

    total_dur    = sum(s["duration_sec"] for s in audio_segments)
    fade_start   = max(0, total_dur - 3)
    res_w, res_h = resolution.split("x")
    n            = len(image_paths)

    # Ken Burns scale: 12% larger than target to allow drift (only used without per_slide_durations)
    kb_w    = int(int(res_w) * 1.12)
    kb_h    = int(int(res_h) * 1.12)
    extra_x = (kb_w - int(res_w)) // 2
    extra_y = (kb_h - int(res_h)) // 2

    # Character is baked into slides when per_slide_durations is provided
    has_char = bool(character_path and Path(character_path).exists() and per_slide_durations is None)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Merge narration ───────────────────────────────────
    merged_audio = os.path.join(os.path.dirname(output_path), "narration.wav")
    compose_audio([s["audio_path"] for s in audio_segments], merged_audio, ffmpeg)

    # ── Step 2: Copy font ─────────────────────────────────────────
    work_dir   = os.path.dirname(output_path)
    font_src   = config["thumbnail"]["font_path"]
    local_font = os.path.join(work_dir, "fonts", os.path.basename(font_src))
    os.makedirs(os.path.dirname(local_font), exist_ok=True)
    if not os.path.exists(local_font):
        shutil.copy2(font_src, local_font)

    # ── Step 3: Ensure SE exists ──────────────────────────────────
    if se_path and not Path(se_path).exists():
        _ensure_se(ffmpeg, se_path)

    has_se = bool(se_path and Path(se_path).exists())

    # ── Step 4: Build FFmpeg inputs ───────────────────────────────
    inputs = []
    if per_slide_durations:
        for i, (img, dur) in enumerate(zip(image_paths, per_slide_durations)):
            clip_dur = dur + (fade_dur if i < n - 1 else 0)
            inputs += ["-loop", "1", "-t", str(clip_dur), "-i", img]
    else:
        for img in image_paths:
            inputs += ["-loop", "1", "-t", str(display_dur), "-i", img]

    audio_idx = n
    inputs += ["-i", merged_audio]
    bgm_idx = n + 1
    inputs += ["-i", bgm_path]

    next_idx = n + 2
    se_idx = None
    if has_se:
        se_idx = next_idx
        inputs += ["-i", se_path]
        next_idx += 1

    char_idx = None
    if has_char:
        char_idx = next_idx
        inputs += ["-loop", "1", "-t", str(total_dur + 1), "-i", character_path]

    # ── Step 5: Build filter_complex ─────────────────────────────
    # Transition playlist: varied professional effects
    _TRANSITIONS = [
        "fade", "wipeleft", "wiperight", "slideleft", "slideright",
        "circleopen", "circleclose", "dissolve", "fadeblack",
        "smoothleft", "smoothright", "smoothup", "smoothdown",
        "diagtl", "diagbr", "radial", "pixelize",
        "horzopen", "horzclose", "vertopen", "vertclose",
    ]

    parts = []

    # Ken Burns patterns: (scale, x_dir, y_dir)
    # x_dir: 'r'=right→left, 'l'=left→right, 'c'=center
    # y_dir: 'd'=down→up,    'u'=up→down,    'c'=center
    _KB_PATTERNS = [
        (1.06, 'r', 'c'),   # zoom+pan right
        (1.05, 'l', 'c'),   # zoom+pan left
        (1.07, 'c', 'd'),   # zoom+pan down→up
        (1.04, 'r', 'd'),   # zoom+diagonal
        (1.06, 'l', 'u'),   # zoom+diagonal reverse
        (1.05, 'c', 'u'),   # zoom+pan up→down
        (1.08, 'r', 'u'),   # strong zoom+diagonal
        (1.04, 'l', 'd'),   # gentle zoom+diagonal
    ]

    for i in range(n):
        if per_slide_durations:
            dur_i    = per_slide_durations[i]
            KB, xd, yd = _KB_PATTERNS[i % len(_KB_PATTERNS)]
            kb_i_w   = int(int(res_w) * KB / 2) * 2
            kb_i_h   = int(int(res_h) * KB / 2) * 2
            ox       = (kb_i_w - int(res_w)) // 2
            oy       = (kb_i_h - int(res_h)) // 2
            safe_dur = max(dur_i, 0.1)
            t_norm   = f"min(t,{safe_dur:.2f})/{safe_dur:.2f}"

            x_expr = (
                f"'{ox}*(1-{t_norm})'" if xd == 'r' else
                f"'{ox}*{t_norm}'"     if xd == 'l' else
                f"'{ox}'"
            )
            y_expr = (
                f"'{oy}*(1-{t_norm})'" if yd == 'd' else
                f"'{oy}*{t_norm}'"     if yd == 'u' else
                f"'{oy}'"
            )
            parts.append(
                f"[{i}:v]"
                f"scale={kb_i_w}:{kb_i_h}:force_original_aspect_ratio=increase,"
                f"crop={kb_i_w}:{kb_i_h},"
                f"fps={fps},"
                f"crop={res_w}:{res_h}:x={x_expr}:y={y_expr},"
                f"setsar=1"
                f"[v{i}]"
            )
        else:
            # Ken Burns: alternate drift direction
            crop_x = (
                f"min({extra_x}*t/{display_dur},{extra_x * 2})"
                if i % 2 == 0
                else f"max({extra_x}*(1-t/{display_dur}),0)"
            )
            parts.append(
                f"[{i}:v]"
                f"scale={kb_w}:{kb_h}:force_original_aspect_ratio=increase,"
                f"crop={kb_w}:{kb_h},"
                f"fps={fps},"
                f"crop={res_w}:{res_h}:x='{crop_x}':y='{extra_y}',"
                f"setsar=1"
                f"[v{i}]"
            )

    # Cross-fade chain between images (xfade with varied transitions)
    if n == 1:
        parts.append(f"[v0]trim=duration={total_dur:.3f},setpts=PTS-STARTPTS[trimmed]")
    else:
        prev = "v0"
        for i in range(n - 1):
            nxt = f"v{i + 1}"
            out = f"xf{i + 1}" if i < n - 2 else "slideshow"
            if per_slide_durations:
                offset = sum(per_slide_durations[:i + 1]) - fade_dur
            else:
                offset = (i + 1) * (display_dur - fade_dur)
            trans = _TRANSITIONS[i % len(_TRANSITIONS)]
            parts.append(
                f"[{prev}][{nxt}]xfade=transition={trans}:duration={fade_dur}:offset={offset:.3f}[{out}]"
            )
            prev = out
        parts.append(f"[slideshow]trim=duration={total_dur:.3f},setpts=PTS-STARTPTS[trimmed]")

    # Optional character overlay with bobbing animation
    video_out_label = "trimmed"
    if has_char:
        parts.append(f"[{char_idx}:v]scale=-1:{char_h}[char_scaled]")
        parts.append(
            f"[trimmed][char_scaled]"
            f"overlay=x=W-w-30:y='H-h-20-8*sin(t*2.513)':eval=frame"
            f"[video_out]"
        )
        video_out_label = "video_out"

    # BGM: loop → trim → volume → fade in/out
    parts.append(
        f"[{bgm_idx}:a]aloop=loop=-1:size=2e9,"
        f"atrim=duration={total_dur:.3f},"
        f"volume={bgm_vol},"
        f"afade=t=in:st=0:d=2,"
        f"afade=t=out:st={fade_start:.3f}:d=3[bgm_out]"
    )

    # SE + final audio mix (normalize=0 to keep individual volumes as-is)
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

    # ── Step 6: Pass 1 — images + audio + character ───────────────
    raw_video = output_path.replace(".mp4", "_raw.mp4")
    cmd_pass1 = (
        [ffmpeg, "-y"]
        + inputs
        + [
            "-filter_complex", filter_complex,
            "-map", f"[{video_out_label}]",
            "-map", "[audio_out]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            raw_video,
        ]
    )
    _run(cmd_pass1, "Video render pass1")

    # ── Step 7: Pass 2 — subtitle burn + bottom accent line ───────
    sub_dir  = os.path.dirname(subtitle_path)
    sub_font = os.path.join(sub_dir, "fonts", os.path.basename(font_src))
    os.makedirs(os.path.dirname(sub_font), exist_ok=True)
    if not os.path.exists(sub_font):
        shutil.copy2(font_src, sub_font)

    # Bottom accent line — color from active channel (default green)
    ch_accent = config.get("active_channel", {}).get("accent_color", [50, 200, 80])
    accent_hex = "{:02X}{:02X}{:02X}".format(*ch_accent[:3])
    vf_pass2 = f"drawbox=x=0:y=ih-6:w=iw:h=6:color=0x{accent_hex}:t=fill,ass=subs.ass:fontsdir=fonts"
    cmd_pass2 = [
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
