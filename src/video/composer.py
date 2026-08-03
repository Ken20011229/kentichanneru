import logging
import math
import os
import shutil
import subprocess
from pathlib import Path

from src.video.slide_gen import CONTENT_X1, CONTENT_Y1, CONTENT_W, CONTENT_H
from src.video.slide_render import (
    render_slide_clip, merge_clips_at_boundaries, concat_clips_lossless,
    snap_to_frame,
)

logger = logging.getLogger(__name__)

# YouTube の音量正規化基準 (-14 LUFS)。TP は真のピーク上限、LRA はダイナミック
# レンジ上限。composer / shorts_composer で同じ設定を使う。
_LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"


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
    character_layers: list[dict] | None = None,
    cuts_per_slide: list[int] | None = None,
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
    fade_dur  = snap_to_frame(config["video"].get("xfade_duration", 0.5), fps)
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

    # plate_paths はカット単位に平坦化されている。cuts_per_slide でセグメント
    # 境界を復元し、セグメント内はハードカット、境界だけ xfade でつなぐ。
    # xfade は結合のたびに「それまでの累積結果」を再エンコードするため、
    # カット単位でつなぐと総エンコード量が O(N^2) になる。64 カット・250 秒の
    # 実測では 2 時間 16 分ぶんのエンコードに相当し、60 分の枠を超えて
    # run 30455026545 がタイムアウトした。境界だけなら結合は 13 回で済む。
    if cuts_per_slide and sum(cuts_per_slide) == n:
        groups, at = [], 0
        for c in cuts_per_slide:
            groups.append(list(range(at, at + c)))
            at += c
    else:
        groups = [[i] for i in range(n)]

    clip_paths   = []
    group_durs   = []
    for g_idx, group in enumerate(groups):
        cut_clips = []
        for i in group:
            # xfade 用の予備 fade_dur は、次の xfade に食われるセグメント末尾の
            # カットにだけ持たせる。中間のカットはハードカットなので不要。
            clip_dur = slide_durs[i] + (fade_dur if i == group[-1] else 0.0)
            clip_out = os.path.join(clips_dir, f"clip_{i:03d}.mp4")
            render_slide_clip(
                ffmpeg, plate_paths[i], content_paths[i], clip_dur, fps,
                int(res_w), int(res_h), CONTENT_X1, CONTENT_Y1, CONTENT_W, CONTENT_H,
                clip_out,
                pan_right=(i % 2 == 0),
            )
            cut_clips.append(clip_out)

        clip_paths.append(concat_clips_lossless(
            ffmpeg, cut_clips, os.path.join(clips_dir, f"slide_{g_idx:03d}.mp4"),
        ))
        group_durs.append(sum(slide_durs[i] for i in group))

    slide_durs = group_durs
    n = len(clip_paths)
    logger.info(
        f"Rendered {sum(len(g) for g in groups)} cut(s) into {n} slide clip(s); "
        f"{max(n - 1, 0)} xfade merge(s) to follow"
    )

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
        slideshow_path, _ = merge_clips_at_boundaries(
            ffmpeg, clip_paths, slide_durs, fade_dur, _TRANSITIONS, clips_dir,
        )
    # 結合と最終合成はどちらも数分かかる。どちらが重いのか本番ログから切り分け
    # られるよう、境目にログを置く。
    logger.info("Slide clips merged; compositing characters, subtitles and audio")

    # ── Step 6: Final composite — character overlay + audio mix ───────────────
    # 入力は video 1本 + character(あれば)1本 + narration/bgm/se の高々5本の
    # みで、xfadeチェーンを含まないためスライド枚数nに依存せずメモリは一定。
    # 最終 ffmpeg は字幕ディレクトリを cwd にして実行するので、入力は必ず
    # 絶対パスで渡す（assets/bgm のような相対パスは cwd 変更で壊れる）。
    inputs = ["-i", os.path.abspath(slideshow_path)]
    parts  = [f"[0:v]trim=duration={total_dur:.3f},setpts=PTS-STARTPTS[pre_char]"]

    # キャラは表情差分ごとに1入力を用意し、`enable` で表示区間を切り替える。
    # クリップに焼き込まないのは、xfade に巻き込まれてキャラが背景と一緒に
    # 動く/消える不具合を過去に出しているため。入力本数は差分の種類数
    # (最大3)で頭打ちなのでスライド枚数に依存しない。
    layers = character_layers or (
        [{"path": character_path, "ranges": None}] if character_path else []
    )
    next_idx = 1
    cur      = "pre_char"
    for li, layer in enumerate(layers):
        path = layer.get("path")
        if not path or not Path(path).exists():
            continue
        inputs += ["-loop", "1", "-t", f"{total_dur:.3f}", "-i", os.path.abspath(path)]
        parts.append(f"[{next_idx}:v]setsar=1[char{li}]")
        ranges = layer.get("ranges")
        enable = ""
        if ranges:
            expr = "+".join(f"between(t\\,{s:.3f}\\,{e:.3f})" for s, e in ranges)
            enable = f":enable='{expr}'"
        parts.append(f"[{cur}][char{li}]overlay=0:0:shortest=1{enable}[chr{li}]")
        cur = f"chr{li}"
        next_idx += 1
    # 最終出力までのフィルタを1本にまとめる。以前は「合成 → mp4 → 字幕焼き」で
    # 2回エンコードしており、クリップ結合の再エンコードと合わせて最初のスライドは
    # 40世代以上を経ていた(実測ビットレート430kbps)。字幕とアクセント線を
    # ここに繋いで最終エンコードを1回にする。
    sub_dir    = os.path.dirname(subtitle_path)
    sub_font   = os.path.join(sub_dir, "fonts", os.path.basename(font_src))
    os.makedirs(os.path.dirname(sub_font), exist_ok=True)
    if not os.path.exists(sub_font):
        shutil.copy2(font_src, sub_font)

    ch_accent  = config.get("active_channel", {}).get("accent_color", [50, 200, 80])
    accent_hex = "{:02X}{:02X}{:02X}".format(*ch_accent[:3])
    # ⚠ 末尾の黒フェードは入れない。実測では t=266.13s で輝度166 → t=267.30s で
    # 4.16 まで落ちる一方、ナレーションは 267.3s まで鳴っており、
    # 「チャンネル登録と高評価をお願いします」の最後の一言が完全な暗転の上に
    # 乗っていた。YouTube の終了画面とも競合する。
    parts.append(
        f"[{cur}]"
        f"drawbox=x=0:y=ih-6:w=iw:h=6:color=0x{accent_hex}:t=fill,"
        f"ass={os.path.basename(subtitle_path)}:fontsdir=fonts[trimmed]"
    )

    audio_idx = next_idx
    inputs += ["-i", os.path.abspath(merged_audio)]
    next_idx += 1

    bgm_idx = None
    if bgm_path:
        bgm_idx = next_idx
        inputs += ["-i", os.path.abspath(bgm_path)]
        next_idx += 1

    se_idx = None
    if has_se:
        se_idx = next_idx
        inputs += ["-i", os.path.abspath(se_path)]
        next_idx += 1

    # ナレーションはミックス用とサイドチェイン(ダッキング)検出用の2系統に分ける。
    narration = f"[{audio_idx}:a]"
    if bgm_idx is not None:
        parts.append(f"[{audio_idx}:a]asplit=2[narr_mix][narr_sc]")
        narration = "[narr_mix]"

    mix_inputs = [narration]
    if bgm_idx is not None:
        # BGM素材のラウドネスは実測で -9.9 〜 -19.8 LUFS と約10dBばらついている
        # (Lostwood_Reverie -9.9 / Rain_Drop -19.8)。固定の volume= を掛けるだけ
        # だと、同じチャンネルなのに動画ごとにBGMの存在感が10dB変わる。まず
        # トラック単位でラウドネスを揃えてから相対音量を決める。
        # config の bgm_volume(相対値)を LUFS ターゲットに換算する: 0.09 → -28 LUFS。
        bgm_lufs = -28.0 + 20 * math.log10(max(float(bgm_vol), 0.001) / 0.09)
        parts.append(
            f"[{bgm_idx}:a]aloop=loop=-1:size=2e9,"
            f"atrim=duration={total_dur:.3f},"
            f"loudnorm=I={bgm_lufs:.1f}:TP=-8:LRA=11,"
            f"afade=t=in:st=0:d=2,"
            f"afade=t=out:st={fade_start:.3f}:d=3[bgm_norm]"
        )
        # サイドチェイン・ダッキング: ナレーションが鳴っている間だけ BGM を
        # 約6dB下げ、句間のポーズで戻す。固定ゲインのままだと声と音楽が同じ
        # 帯域で張り合い、ナレーションの明瞭度が落ちる。
        parts.append(
            f"[bgm_norm][narr_sc]"
            f"sidechaincompress=threshold=0.03:ratio=6:attack=20:release=350"
            f"[bgm_out]"
        )
        mix_inputs.append("[bgm_out]")

    if has_se:
        parts.append(f"[{se_idx}:a]volume={se_vol}[se_out]")
        mix_inputs.append("[se_out]")

    # Audio mix
    # YouTube は -14 LUFS 基準で音量を揃えるが、大きい音を下げるだけで小さい音は
    # 持ち上げない。実測した本番動画は -24.7 LUFS(ebur128)で、フィード上の他の
    # 動画より約10dB小さく再生されていた。ミックス後にラウドネス正規化をかける
    # (ナレーション/BGM/SEの比率は保ったまま全体を持ち上げる)。
    if len(mix_inputs) > 1:
        parts.append(
            "".join(mix_inputs)
            + f"amix=inputs={len(mix_inputs)}:duration=first:dropout_transition=2:"
              f"normalize=0,{_LOUDNORM}[audio_out]"
        )
    else:
        parts.append(f"{narration}{_LOUDNORM}[audio_out]")

    filter_complex = ";".join(parts)

    cmd = (
        [ffmpeg, "-y"]
        + inputs
        + [
            "-filter_complex", filter_complex,
            "-map", "[trimmed]",
            "-map", "[audio_out]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p",
            # シークのプレビューがカット位置から大きくずれないよう GOP を3秒に
            "-g", str(fps * 3), "-keyint_min", str(fps),
            "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
            "-movflags", "+faststart",
            os.path.abspath(output_path),
        ]
    )
    # ass フィルタのファイル指定は Windows のドライブレター(コロン)を
    # エスケープする必要があるため、字幕ディレクトリを cwd にして相対参照する。
    _run(cmd, "Video render", cwd=sub_dir)
    shutil.rmtree(clips_dir, ignore_errors=True)

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
