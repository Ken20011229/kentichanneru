import logging
import os
import subprocess

logger = logging.getLogger(__name__)


def _run(cmd: list[str], label: str, cwd: str = None):
    logger.debug(f"{label}: {' '.join(cmd)}")
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=cwd,
    )
    if result.returncode != 0:
        logger.error(f"{label} stderr:\n{result.stderr[-2000:]}")
        raise RuntimeError(f"FFmpeg {label} failed (exit {result.returncode})")


def render_slide_clip(
    ffmpeg: str,
    plate_path: str,
    content_path: str | None,
    clip_dur: float,
    fps: int,
    res_w: int,
    res_h: int,
    content_x1: int,
    content_y1: int,
    content_w: int,
    content_h: int,
    out_path: str,
    pan_right: bool | None = True,
) -> str:
    """plate(+content zoompan overlay)を1本のmp4クリップにレンダリングする。

    ffmpeg入力は常に最大2本(plate + content)に限定されるため、スライド枚数に
    かかわらずメモリ使用量が一定になる。clip_durは表示時間+次のxfadeで使う
    予備fade_dur分を含んだ長さを渡すこと(呼び出し側で統一する)。
    """
    inputs = ["-loop", "1", "-t", f"{clip_dur:.3f}", "-i", plate_path]
    parts = [
        f"[0:v]"
        f"scale={res_w}:{res_h}:force_original_aspect_ratio=increase,"
        f"crop={res_w}:{res_h},"
        f"fps={fps},setsar=1[plate]"
    ]

    if content_path is not None:
        inputs += ["-loop", "1", "-t", f"{clip_dur:.3f}", "-i", content_path]
        D = max(1, int(clip_dur * fps))
        # Ken Burns: 総ズーム量 14%。加えて左右にゆっくりパンさせ、方向は
        # スライドごとに交互にする（中央固定ズームだけだと動きが単調）。
        #
        # zoompan は x/y を整数に切り捨てるため等倍で処理すると1px単位で
        # カクつく(実測: 2秒でグリフ幅 +0.65% しか動かないのに左端が
        # 509→510→508 と往復していた)。SS 倍に拡大してから zoompan し、
        # 最後に等倍へ戻すことで切り捨て誤差を 1/SS にする。
        SS        = 2
        zoom_max  = 1.14
        zoom_rate = (zoom_max - 1.0) / D
        pan_dir   = 1 if pan_right else -1
        # 中央を基準に、ズームで生まれた余白の 60% までを横に振る
        x_expr = (
            f"(iw-iw/zoom)/2+{pan_dir}*(iw-iw/zoom)/2*0.6*(on/{D})"
            if pan_right is not None else "(iw-iw/zoom)/2"
        )
        parts.append(
            f"[1:v]"
            f"scale={content_w * SS}:{content_h * SS}:force_original_aspect_ratio=increase,"
            f"crop={content_w * SS}:{content_h * SS},"
            f"fps={fps},"
            f"zoompan="
            f"z='min(zoom+{zoom_rate:.6f},{zoom_max})':"
            f"x='{x_expr}':"
            f"y='(ih-ih/zoom)/2':"
            f"d={D}:s={content_w * SS}x{content_h * SS}:fps={fps},"
            f"scale={content_w}:{content_h}:flags=bicubic,"
            f"setsar=1[anim]"
        )
        parts.append(f"[plate][anim]overlay=x={content_x1}:y={content_y1}:shortest=1[out]")
    else:
        # content が無いカット(タイトル台紙)も完全静止させない。以前はここが
        # `null` だったため、本番動画の冒頭27秒がピクセル単位で不動になり、
        # 離脱が集中する区間を丸ごと捨てていた。
        D  = max(1, int(clip_dur * fps))
        SS = 2
        zoom_rate = 0.06 / D
        parts.append(
            f"[plate]"
            f"scale={res_w * SS}:{res_h * SS}:flags=bicubic,"
            f"zoompan="
            f"z='min(zoom+{zoom_rate:.6f},1.06)':"
            f"x='(iw-iw/zoom)/2':"
            f"y='(ih-ih/zoom)/2':"
            f"d={D}:s={res_w * SS}x{res_h * SS}:fps={fps},"
            f"scale={res_w}:{res_h}:flags=bicubic,"
            f"setsar=1[out]"
        )

    cmd = [ffmpeg, "-y"] + inputs + [
        "-filter_complex", ";".join(parts),
        "-map", "[out]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p",
        out_path,
    ]
    _run(cmd, f"Render slide clip {os.path.basename(out_path)}")
    return out_path


def merge_clips_sequential(
    ffmpeg: str,
    clip_paths: list[str],
    display_durs: list[float],
    fade_dur: float,
    transitions: list[str],
    work_dir: str,
) -> tuple[str, float]:
    """クリップを逐次(ローリング)xfade結合し、最終動画パスと総尺を返す。

    各clip_paths[i]は display_durs[i] + fade_dur の長さを持つ前提(末尾
    fade_dur分は次のxfadeとのブレンド用の予備)。

    xfadeは「offsetまで入力1をそのまま通し、以降を入力2に置き換える」非対称
    な操作で、入力1側の末尾(予備)だけが消費され、入力2側は表示内容の先頭
    からそのまま使われる。そのため2本ずつのペアをどう括ってもよい
    結合律(associativity)は成り立たず、常に「これまでの結合結果」を入力1、
    「次のクリップ」を入力2として左から順に結合する必要がある(木構造で
    任意の順序に結合すると、結合のたびにfade_dur分の表示内容が失われ、
    深さに応じてロスが蓄積し動画が本来より短くなる)。

    各ffmpeg呼び出しの入力は常に2本のみ(これまでの結合結果+次のクリップ)
    なので、スライド枚数nに依存せずメモリ使用量はほぼ一定になる。
    """
    os.makedirs(work_dir, exist_ok=True)

    current_path    = clip_paths[0]
    current_display = display_durs[0]
    current_is_tmp  = False

    for i in range(1, len(clip_paths)):
        next_path = clip_paths[i]
        offset = max(current_display - fade_dur, 0.0)
        trans  = transitions[(i - 1) % len(transitions)]

        out_path = os.path.join(work_dir, f"merge_{i:03d}.mp4")
        fc = f"[0:v][1:v]xfade=transition={trans}:duration={fade_dur:.3f}:offset={offset:.3f}[v]"
        cmd = [
            ffmpeg, "-y", "-i", current_path, "-i", next_path,
            "-filter_complex", fc, "-map", "[v]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
            out_path,
        ]
        _run(cmd, f"Merge clips [0:{i + 1})")

        if current_is_tmp:
            try:
                os.unlink(current_path)
            except OSError:
                pass

        current_path    = out_path
        current_display += display_durs[i]
        current_is_tmp  = True

    return current_path, current_display
