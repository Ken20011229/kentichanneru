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


def snap_to_frame(seconds: float, fps: int) -> float:
    """秒数をフレーム境界に丸める（最低 1 フレーム）。

    fade_dur がフレームの整数倍でないと、クリップの切り出し位置と xfade の
    進行度が半フレームぶんずれる。実測では Shorts の 0.25s/30fps（＝7.5
    フレーム）で、遷移の中央の混合比が約 5% ずれていた（見た目には出ない
    が、境界結合と逐次結合の出力が一致しなくなり検証が効かなくなる）。
    """
    return max(1, round(seconds * fps)) / fps


def concat_clips_lossless(
    ffmpeg: str, clip_paths: list[str], out_path: str,
) -> str:
    """クリップを再エンコードせずに繋ぐ（ハードカット）。

    xfade と違って累積結果を作り直さないので、本数に対して線形。
    render_slide_clip の出力はコーデック・解像度・fps が全て揃っているため
    -c copy がそのまま成立する。1 本だけのときは連結不要なのでそのまま返す。
    """
    if len(clip_paths) == 1:
        return clip_paths[0]

    list_path = os.path.splitext(out_path)[0] + ".txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in clip_paths:
            # concat demuxer の list は ' をエスケープする必要がある
            f.write("file '{}'\n".format(os.path.abspath(p).replace("'", r"'\''")))

    _run(
        [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
         "-c", "copy", out_path],
        "concat clips",
    )
    return out_path


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


def merge_clips_at_boundaries(
    ffmpeg: str,
    clip_paths: list[str],
    display_durs: list[float],
    fade_dur: float,
    transitions: list[str],
    work_dir: str,
) -> tuple[str, float]:
    """境界の fade_dur 秒だけを xfade し、残りは 1 回ずつ切り出して繋ぐ。

    merge_clips_sequential は結合のたびに「それまでの累積結果」をまるごと
    再エンコードするため、総エンコード量がクリップ数に対して O(n^2) になる
    (実測: 8 クリップ・230 秒の動画で 12 分)。実際に 2 本のクリップが混ざる
    のは境界の fade_dur 秒だけで、それ以外の区間はどちらか一方がそのまま
    流れているだけなので、境界だけを xfade して本体はそれぞれ 1 回だけ
    切り出せば足りる。総エンコード量は動画の尺そのものに比例し、クリップ数
    には依存しなくなる。

    副次的に世代劣化も減る。逐次結合では先頭のクリップだけが n-1 回
    再エンコードされており、これが実測ビットレート低下の一因だった。

    xfade(offset = 累積表示時間 - fade_dur)を展開すると出力はこう分解できる:

        出力 = B0 + T0 + B1 + T1 + ... + T(n-2) + B(n-1)

        B0     = clip0[0, d0 - fade)
        Bi     = clipi[fade, di)                  (0 < i < n-1)
        B(n-1) = clip(n-1)[fade, 末尾]
        Ti     = xfade(clipi の遷移元 fade 秒, clip(i+1) の先頭 fade 秒)

    遷移元は i=0 のときだけ clip0[d0-fade, d0)、i>=1 では clipi[di, di+fade)
    になる(clip0 は先頭が前の遷移に食われないぶんだけ位相がずれるため)。

    ⚠ xfade は非結合的で「これまでの結合結果を入力1、次のクリップを入力2」
    として左から順に処理する以外に正しい方法が無い、という制約はこの分解でも
    保たれている。各境界は独立に 1 度だけ現れ、順序も入れ替えていない。
    """
    os.makedirs(work_dir, exist_ok=True)
    n = len(clip_paths)
    if n == 1:
        return clip_paths[0], display_durs[0]

    total_display = sum(display_durs)

    # 表示時間が fade の2倍に満たないクリップがあると本体部分が取り出せない。
    # 実運用ではまず起きないが、起きたら従来方式に落として動画を守る。
    if any(d <= fade_dur * 2 for d in display_durs):
        logger.warning(
            f"表示時間が fade_dur({fade_dur}s) の2倍に満たないクリップがあるため、"
            f"境界結合をやめて逐次 xfade に落とす"
        )
        return merge_clips_sequential(
            ffmpeg, clip_paths, display_durs, fade_dur, transitions, work_dir,
        )

    # 本体と遷移をまったく同じ設定で符号化する。この後の -c copy 連結は
    # コーデックパラメータが揃っていることが前提なので、ここを揃えるのは
    # 速度ではなく正しさのため。
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
           "-pix_fmt", "yuv420p"]

    pieces: list[str] = []
    for i in range(n):
        # ── 本体 ────────────────────────────────────────────────────────────
        body = os.path.join(work_dir, f"body_{i:03d}.mp4")
        # 先頭 fade は直前の遷移に食われている(clip0 だけは前が無いので 0 から)。
        start = 0.0 if i == 0 else fade_dur
        cmd   = [ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", clip_paths[i]]
        if i < n - 1:
            # 末尾 fade は直後の遷移が受け持つので本体からは外す。最後のクリップ
            # だけは後ろに遷移が無いため、末尾まで丸ごと使う。
            cmd += ["-t", f"{display_durs[i] - fade_dur:.3f}"]
        cmd += ["-an"] + enc + [body]
        _run(cmd, f"Cut body {i}")
        pieces.append(body)

        if i == n - 1:
            break

        # ── 遷移(ここだけが 2 本のクリップを混ぜる) ─────────────────────────
        trans_start = (display_durs[0] - fade_dur) if i == 0 else display_durs[i]
        trans       = transitions[i % len(transitions)]
        tpath       = os.path.join(work_dir, f"trans_{i:03d}.mp4")
        _run(
            [ffmpeg, "-y",
             "-ss", f"{trans_start:.3f}", "-i", clip_paths[i],
             "-ss", "0", "-t", f"{fade_dur:.3f}", "-i", clip_paths[i + 1],
             "-filter_complex",
             f"[0:v]setpts=PTS-STARTPTS[a];[1:v]setpts=PTS-STARTPTS[b];"
             f"[a][b]xfade=transition={trans}:duration={fade_dur:.3f}:offset=0[v]",
             "-map", "[v]", "-an"] + enc + [tpath],
            f"Render transition {i}",
        )
        pieces.append(tpath)

    out_path = concat_clips_lossless(
        ffmpeg, pieces, os.path.join(work_dir, "merged.mp4"),
    )

    # 分解を間違えると尺が縮む(過去に木構造結合で 320 秒が 159 秒になった前例が
    # ある)。壊れた動画を投稿するくらいなら遅くても正しいほうを採るので、
    # 尺がずれていたら従来方式でやり直す。
    got = _probe_duration(ffmpeg, out_path)
    if got and abs(got - total_display) > 1.0:
        logger.warning(
            f"境界結合の尺が想定と合わない(実測 {got:.2f}s / 想定 {total_display:.2f}s) "
            f"— 逐次 xfade でやり直す"
        )
        return merge_clips_sequential(
            ffmpeg, clip_paths, display_durs, fade_dur, transitions, work_dir,
        )

    return out_path, total_display


def _probe_duration(ffmpeg: str, path: str) -> float:
    """ffprobe で尺を取る。取れなければ 0.0(呼び出し側で検証をスキップ)。

    ffprobe は ffmpeg と同じディレクトリに置かれるので、設定された ffmpeg の
    パスから導く(config が絶対パスでも PATH 頼みにならないようにする)。
    """
    base, name = os.path.split(ffmpeg)
    stem, ext  = os.path.splitext(name)
    ffprobe    = os.path.join(base, "ffprobe" + ext) if stem == "ffmpeg" else "ffprobe"
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return float(result.stdout.strip())
    except (OSError, ValueError):
        logger.warning(f"ffprobe で尺を取得できなかった: {path}")
        return 0.0


def merge_clips_sequential(
    ffmpeg: str,
    clip_paths: list[str],
    display_durs: list[float],
    fade_dur: float,
    transitions: list[str],
    work_dir: str,
) -> tuple[str, float]:
    """クリップを逐次(ローリング)xfade結合し、最終動画パスと総尺を返す。

    通常は merge_clips_at_boundaries を使う(こちらは総エンコード量が O(n^2)
    になるため実測で 5 倍遅い)。境界結合が使えない構成に当たったときと、
    結合結果の尺が想定と合わなかったときのフォールバックとして残している。

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
