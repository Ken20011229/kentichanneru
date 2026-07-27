import logging
import math
import os
import shutil
import subprocess
from pathlib import Path

from src.video.composer import _LOUDNORM
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
            pan_right=(i % 2 == 0),
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
    # 字幕ディレクトリを cwd にして1回で焼き込むため、入力は絶対パスで渡す
    inputs = [
        "-i", os.path.abspath(slideshow_path),
        "-t", str(max_duration), "-i", os.path.abspath(audio_path),
    ]
    if bgm_path:
        inputs += ["-i", os.path.abspath(bgm_path)]
    parts = [
        f"[0:v]trim=duration={max_duration:.3f},setpts=PTS-STARTPTS,"
        f"{{VF}}[trimmed_v]"
    ]

    narration  = "[1:a]"
    mix_inputs = [narration]
    if bgm_path:
        parts.append("[1:a]asplit=2[narr_mix][narr_sc]")
        narration = "[narr_mix]"
        mix_inputs = [narration]
        # ⚠ 末尾の afade=t=out を入れてはいけない。Shorts はループ再生され、
        # ループ点で音が消えるとそこが離脱ポイントになる（CTAを外して
        # 「最後の1文が最初につながる」構成にした意図が消える）。
        # BGMは本編と同じくトラック単位で正規化してから相対音量を決める。
        bgm_lufs = -28.0 + 20 * math.log10(max(float(bgm_vol), 0.001) / 0.09)
        parts.append(
            f"[2:a]aloop=loop=-1:size=2e9,"
            f"atrim=duration={max_duration:.3f},"
            f"loudnorm=I={bgm_lufs:.1f}:TP=-8:LRA=11,"
            f"afade=t=in:st=0:d=0.8[bgm_norm]"
        )
        parts.append(
            f"[bgm_norm][narr_sc]"
            f"sidechaincompress=threshold=0.03:ratio=6:attack=20:release=300[bgm_out]"
        )
        mix_inputs.append("[bgm_out]")

    # 本編と同じく -14 LUFS に正規化する(実測 -24.7 LUFS で、Shorts のフィードでは
    # 音量差がそのままスワイプ理由になる)
    if len(mix_inputs) > 1:
        parts.append(
            "".join(mix_inputs)
            + f"amix=inputs={len(mix_inputs)}:duration=first:dropout_transition=1:"
              f"normalize=0,{_LOUDNORM}[audio_out]"
        )
    else:
        parts.append(f"{narration}{_LOUDNORM}[audio_out]")

    # 最下部のラインは Shorts のUI帯に完全に隠れるため描かない。アクセントは
    # plate 側（上端とコンテンツ下辺）で表現する。
    # 合成と字幕焼き込みを1回のエンコードにまとめる（以前は2回エンコードして
    # いて世代劣化していた）。
    #
    # ⚠ 末尾のフェードアウトも入れない。ループ再生の継ぎ目が黒画面になる。
    # 代わりに先頭だけ 0.15 秒フェードインさせて、ループの折り返しを滑らかにする。
    sub_dir = os.path.dirname(subtitle_path)
    vf = (
        f"ass={os.path.basename(subtitle_path)}:fontsdir=fonts,"
        f"fade=t=in:st=0:d=0.15"
    )
    filter_complex = ";".join(parts).replace("{VF}", vf)

    cmd = (
        [ffmpeg, "-y"]
        + inputs
        + [
            "-filter_complex", filter_complex,
            "-map", "[trimmed_v]",
            "-map", "[audio_out]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-g", str(fps * 3), "-keyint_min", str(fps),
            "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
            "-movflags", "+faststart",
            os.path.abspath(output_path),
        ]
    )
    _run(cmd, "Shorts render", cwd=sub_dir)
    shutil.rmtree(clips_dir, ignore_errors=True)

    logger.info(f"Shorts rendered: {output_path}")
    return output_path
