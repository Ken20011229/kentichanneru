import logging
import re
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

# 1920px幅でフォント70pxのとき20文字は約1400px。中央寄せで x=260〜1660 になり
# 右キャラ(x=1582〜)の裏に末尾が回り込んで実際に切れていた。16文字なら1120pxで
# 両キャラの内側に収まる。読み速度も 6.9文字/秒 → 5.5文字/秒 に落ちる。
_MAX_LINE_CHARS = 16
_MAX_LINES_PER_CUE = 2

# 字幕の左右マージン。キャラの内側エッジ(左244px / 右1582px)より内側に置く。
_SUB_MARGIN_H = 340

# 行頭に来てはいけない文字（行頭禁則）と、行末に来てはいけない文字（行末禁則）
_NO_LINE_START = "、。，．）」』】〉》!?！？ー〜…・:;：；%％"
_NO_LINE_END   = "（「『【〈《"
# 文節の切れ目として優先的に採用する助詞・接続
_BREAK_AFTER = "、。！？"


def _ass_color(r: int, g: int, b: int, alpha: int = 0x00) -> str:
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _make_header(accent_rgb: tuple, channel_name: str = "NEWS") -> str:
    r, g, b = accent_rgb
    lr = min(255, r + 70)
    lg = min(255, g + 70)
    lb = min(255, b + 70)

    accent_text    = _ass_color(lr, lg, lb)
    banner_box     = "&H99201810"

    m = _SUB_MARGIN_H
    # 本文字幕は全編ひとつのスタイルに統一する。以前は visual_type によって
    # Detail(白・黒縁) / Point・Keyword(白・アクセント縁) が切り替わり、視聴者
    # からは字幕の見た目がランダムに変わっているようにしか見えなかった。
    # 明るい背景板の上でアクセント色の縁は分離せず(実測コントラスト1.31:1)、
    # 可読性の担保として機能していなかったので縁は黒に固定する。
    # Keyword だけは塗りがアクセント色のまま残っていて、該当セグメントの
    # 段落全文(実測で全キューの15%)が蛍光グリーンで表示されていた。
    # 段落単位で色を変えても視聴者には理由が分からないので、塗りも白に統一する。
    return f"""\
[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes
Timer: 100.0000

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Detail,Noto Sans CJK JP,72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,1,8,3,2,{m},{m},96,1
Style: Point,Noto Sans CJK JP,72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,1,8,3,2,{m},{m},96,1
Style: Keyword,Noto Sans CJK JP,72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,1,8,3,2,{m},{m},96,1
Style: Intro,Noto Sans CJK JP,80,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,1,9,4,2,{m},{m},96,1
Style: Banner_L,Noto Sans CJK JP,28,{accent_text},&H00000000,&H00000000,{banner_box},-1,0,3,4,0,7,16,0,8,1
Style: Banner_R,Noto Sans CJK JP,24,{accent_text},&H00000000,&H00000000,{banner_box},-1,0,3,4,0,9,0,16,8,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _to_ass_time(secs: float) -> str:
    h  = int(secs // 3600)
    m  = int((secs % 3600) // 60)
    s  = secs % 60
    cs = int((s % 1) * 100)
    return f"{h}:{m:02d}:{int(s):02d}.{cs:02d}"


_PARTICLE_BREAK = re.compile(r"[はがをにでとへもやのねよか][^ぁ-ん]")
_HIRAGANA_RE    = re.compile(r"[ぁ-ん]")
_KANJI_RE       = re.compile(r"[一-龥]")
_KATAKANA_RE    = re.compile(r"[ァ-ヶー]")
_ALNUM_RE       = re.compile(r"[A-Za-z0-9]")


# 段落の2キュー目以降に使う控えめなタグ（強いアニメの再発火を避ける）
_CONT_TAG = "{\\fad(140,120)}"

# モーラ換算の重み。VOICEVOX の audio_query で実測したモーラ長と突き合わせて
# 決めた概算値。漢字は音読みで2モーラになることが多く、小書き仮名は前の音に
# 吸収されて単独の長さを持たない。
_SMALL_KANA = "ゃゅょぁぃぅぇぉャュョァィゥェォっッ"


def _mora_weight(text: str) -> float:
    """発話の長さの目安（モーラ数の近似）を返す。"""
    w = 0.0
    for c in text:
        if c in _SMALL_KANA:
            continue
        if _KANJI_RE.match(c):
            w += 1.9
        elif c.isdigit():
            w += 2.0
        elif _HIRAGANA_RE.match(c) or _KATAKANA_RE.match(c):
            w += 1.0
        elif c.isalpha():
            w += 1.0
        # 句読点・記号は発話長を持たない（間はセグメント全体の尺に含まれる）
    return w


def _char_kind(c: str) -> str | None:
    """語中で切ってはいけない文字種を返す。同種が連続していれば1語の可能性が高い。

    漢字どうし・カタカナどうし・英数どうしの境界で改行すると「気/温」「吸/収」
    「チベ/ット」のように熟語や複合語が割れる（実レンダリングで実際に発生）。
    ひらがなは活用語尾なので、ここでは対象にしない（助詞判定が別途効く）。
    """
    if not c:
        return None
    if _KANJI_RE.match(c):
        return "kanji"
    if _KATAKANA_RE.match(c):
        return "kata"
    if _ALNUM_RE.match(c):
        return "alnum"
    return None


# 数字に続く助数詞。「5 / 日続いた」「40 / 度」「全国11 / 地点」のように
# 数字と単位が行をまたぐと、数値が読み取れなくなる。
_COUNTER_RE  = re.compile(r"[日度人件年月時分秒回台個名点円割倍歳週軒本枚匹頭番位階%％]")
# 漢数字の位取り。「1万2000人」の途中で折ると数値が読み取れなくなる。
_NUM_UNIT_RE = re.compile(r"[万億兆千百十]")


def _splits_word(text: str, i: int) -> bool:
    """位置 i で切ると語の途中で割れるか。"""
    if i <= 0 or i >= len(text):
        return False
    prev, nxt = text[i - 1], text[i]
    k = _char_kind(prev)
    if k is not None and k == _char_kind(nxt):
        return True
    # 数字は alnum、助数詞は kanji で別種になるため上の判定を素通りしていた
    if prev.isdigit() and _COUNTER_RE.match(nxt):
        return True
    # 漢数字の位取りも数値の一部（「全国で1万 / 2000人」と割れていた）
    if prev.isdigit() and _NUM_UNIT_RE.match(nxt):
        return True
    if _NUM_UNIT_RE.match(prev) and nxt.isdigit():
        return True
    # 接頭辞を行末に取り残さない（「次回お / 会いしましょう」）
    if prev in "おご":
        return True
    return False


def _splits_word_strict(text: str, i: int) -> bool:
    """文字数による強制切断用。ひらがなの連続も語中とみなす。

    `_char_kind` はひらがなに None を返すので、通常の判定ではひらがなの
    連続を守れない（「となりまし / た。」「それではまた次回お / 会いしましょう」
    のような分断が実際に出ていた）。最後の手段の切断でだけ、この条件も見る。
    """
    if _splits_word(text, i):
        return True
    if i <= 0 or i >= len(text):
        return False
    return bool(_HIRAGANA_RE.match(text[i - 1]) and _HIRAGANA_RE.match(text[i]))


def _wrap_one(text: str, max_chars: int) -> list[str]:
    """1つの文を max_chars 以内の行に折る。文節と禁則処理を考慮する。

    以前は「max_chars 位置から手前8文字だけ句読点を探し、無ければ無条件に
    切る」実装で、「以/上」「予/想」「くださ/い」のように単語や熟語が行を
    またいで分断されていた。
    """
    lines = []
    while len(text) > max_chars:
        break_at = 0
        # 1) 句読点の直後で切れるならそこ（末尾に句読点が残る形）
        for i in range(max_chars, max(max_chars - 12, 1), -1):
            if text[i - 1] in _BREAK_AFTER:
                break_at = i
                break
        # 2) だめなら助詞の直後（文節の切れ目）。極端に短い行を作らないよう
        #    行の半分より手前では切らない。
        if not break_at:
            for i in range(max_chars, max(max_chars // 2, 2) - 1, -1):
                if _PARTICLE_BREAK.match(text[i - 1:i + 1] or ""):
                    break_at = i
                    break
        # 3) 次の行が「ひらがな以外（漢字・カタカナ・英数）」で始まる位置。
        #    日本語は自立語がそこから始まることが多く、「となりまし/た。」の
        #    ような語中分断を避けられる。行が短くなりすぎないよう半分までに制限。
        #    ただし前後が同じ文字種なら熟語の内部なので切らない（この条件が
        #    無かったため「押し縮められて気 / 温が上昇しました」のように
        #    漢字熟語が割れていた）。
        if not break_at:
            for i in range(max_chars, max(max_chars // 2, 2) - 1, -1):
                if i < len(text) and not _HIRAGANA_RE.match(text[i]) \
                        and text[i] not in _NO_LINE_START \
                        and not _splits_word(text, i):
                    break_at = i
                    break
        # 4) それでもだめなら文字数で切る。その場合でも語中を割らない位置まで
        #    数文字だけ手前に戻す。
        if not break_at:
            break_at = max_chars
            for i in range(max_chars, max(max_chars - 5, 1), -1):
                if not _splits_word_strict(text, i):
                    break_at = i
                    break

        # 禁則: 次行の先頭が行頭禁則文字なら1文字こちらに引き込む
        while break_at < len(text) and text[break_at] in _NO_LINE_START:
            break_at += 1
        # 禁則: 行末が行末禁則文字なら1文字次行へ送る
        while break_at > 1 and text[break_at - 1] in _NO_LINE_END:
            break_at -= 1

        lines.append(text[:break_at])
        text = text[break_at:].lstrip()
    if text:
        lines.append(text)
    # 孤児行の解消: 最終行が1〜2文字だけ残ると「た。」のような行ができる。
    # 直前の行から数文字を送って、最低3文字にする。
    if len(lines) >= 2 and len(lines[-1]) <= 2:
        need = 3 - len(lines[-1])
        if len(lines[-2]) - need >= 4:
            lines[-1] = lines[-2][-need:] + lines[-1]
            lines[-2] = lines[-2][:-need]
    return lines


def _split_lines(text: str, max_chars: int = _MAX_LINE_CHARS) -> list[str]:
    """句点で文に割ってから、長い文だけを折り返す。"""
    if len(text) <= max_chars:
        return [text]
    sentences = [s for s in re.split(r"(?<=[。！？])", text) if s.strip()]
    lines: list[str] = []
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) <= max_chars:
            lines.append(sent)
        else:
            lines.extend(_wrap_one(sent, max_chars))
    return lines or [text]


def _split_into_cues(text: str, max_chars: int = _MAX_LINE_CHARS,
                     max_lines: int = _MAX_LINES_PER_CUE) -> list[list[str]]:
    """Break narration text into cue groups of at most max_lines lines,
    each line at most max_chars. A long segment becomes several cues that
    must be timed sequentially (proportional to their character count) so
    the on-screen text keeps pace with the voice."""
    all_lines = _split_lines(text, max_chars)
    return [all_lines[i:i + max_lines] for i in range(0, len(all_lines), max_lines)] or [[""]]


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{")


# ── Animation tag library ──────────────────────────────────────────────────────
# Each value is an ASS override tag block that controls entrance/exit animation.
# PlayRes: 1920×1080, default text anchor bottom-center (Y=992 at MarginV=88).

def _build_anim_tags(play_w: int, play_h: int, margin_v: int) -> dict[str, str]:
    """字幕アニメーションのタグ表を、解像度に合わせて生成する。

    `\\move` 系は座標の絶対値を書くため、解像度ごとに作り分ける必要がある。
    以前は 1920×1080 用の1つの辞書を Shorts(1080×1920) でも使い回しており、
    LLM が `slide_up` などを指定するたびに Shorts の字幕が
    x=960(右端寄り) / y=992(画面のど真ん中) に飛び、右端で切れて表示されて
    いた。**解像度別の表を必ず使うこと。**
    """
    cx = play_w // 2
    cy = play_h - margin_v
    return {
        # Simple fade — most legible
        "fade":        "{\\fad(220,160)}",
        # Scale zoom-in from 82% — smooth focus
        "scale_in":    "{\\fscx82\\fscy82\\fad(260,160)\\t(0,400,\\fscx100\\fscy100)}",
        # Scale up intro — current default for first segment
        "scale_intro": "{\\fscx88\\fscy88\\fad(320,180)\\t(0,440,\\fscx100\\fscy100)}",
        # Quick pop: shoots past 112% then settles — energetic
        "pop":         "{\\fscx60\\fscy60\\fad(80,160)\\t(0,200,\\fscx112\\fscy112)\\t(200,370,\\fscx100\\fscy100)}",
        # Elastic bounce: 106% overshoot then settle — bold emphasis
        "bounce":      "{\\fscx90\\fscy90\\fad(180,150)\\t(0,250,\\fscx106\\fscy106)\\t(250,420,\\fscx100\\fscy100)}",
        # Blur sharpen: cinematic reveal from out-of-focus
        "blur_in":     "{\\blur12\\fad(300,160)\\t(0,460,\\blur0)}",
        # Light blur glow dissolve — softer reveal
        "glow_in":     "{\\blur6\\fad(280,160)\\t(0,480,\\blur0)}",
        # Slight CW spin then straighten — dramatic flair
        "spin_in":     "{\\frz-8\\fscx88\\fscy88\\fad(200,140)\\t(0,380,\\frz0\\fscx100\\fscy100)}",
        # Slide up from below screen — punchy
        "slide_up":    f"{{\\move({cx},{cy + 260},{cx},{cy},0,320)\\fad(160,130)}}",
        # Drop from above screen
        "slide_down":  f"{{\\move({cx},-150,{cx},{cy},0,320)\\fad(160,130)}}",
        # Wipe in from right edge
        "slide_left":  f"{{\\move({play_w + 180},{cy},{cx},{cy},0,320)\\fad(160,130)}}",
        # Wipe in from left edge
        "slide_right": f"{{\\move(-300,{cy},{cx},{cy},0,320)\\fad(160,130)}}",
        # Float up slowly — calm / narrative
        "float_up":    f"{{\\move({cx},{cy + 70},{cx},{cy},0,540)\\fad(300,150)}}",
        # Typewriter-like: fast fade in with slight scale — snappy reveal
        "snap":        "{\\fscx95\\fscy95\\fad(60,160)\\t(0,120,\\fscx100\\fscy100)}",
    }


# 横型 1920×1080 用（MarginV=96）
_ANIM_TAGS: dict[str, str] = _build_anim_tags(1920, 1080, 96)

# 縦型 1080×1920 用。Shorts の下部は YouTube のタイトル/チャンネル名/説明の
# UI 帯（概ね y=1550〜1850）に覆われるので、その上に字幕を置く。
# 380 だと文字下端 y=1540 / 上端 y≈1320 で、キャラ(上端 y≈1187)の顎から胸の
# 上に字幕が乗る一方、コンテンツ枠の下端との間に約230pxの空白が空いていた。
# 560 にするとコンテンツ枠の直下・キャラの頭より上に収まる。
_SHORTS_MARGIN_V = 560
_SHORTS_ANIM_TAGS: dict[str, str] = _build_anim_tags(1080, 1920, _SHORTS_MARGIN_V)

# LLM が animation_style を返さなかったときに順番に当てるプール
_SHORTS_ANIM_POOL = [
    _SHORTS_ANIM_TAGS["scale_in"],
    _SHORTS_ANIM_TAGS["slide_up"],
    _SHORTS_ANIM_TAGS["pop"],
    _SHORTS_ANIM_TAGS["blur_in"],
    _SHORTS_ANIM_TAGS["bounce"],
    _SHORTS_ANIM_TAGS["spin_in"],
    _SHORTS_ANIM_TAGS["slide_left"],
    _SHORTS_ANIM_TAGS["snap"],
]

# Visual-type default animations when LLM does not specify animation_style
_DEFAULT_ANIM: dict[str, str] = {
    "intro":   "scale_intro",
    "keyword": "scale_in",
    "point":   "slide_up",
    "detail":  "fade",
}


def _format_events(seg_start: float, seg_dur: float, text: str,
                   visual_type: str, is_first: bool,
                   animation_style: str = None) -> list[str]:
    """Build one or more ASS Dialogue lines for a segment.

    Long narration is split into multiple cues (max _MAX_LINES_PER_CUE lines
    of _MAX_LINE_CHARS chars each). Each cue's on-screen duration is
    proportional to its *mora count* within the segment, so the text keeps
    pace with the voice instead of dumping the whole narration at once.
    """
    cue_groups = _split_into_cues(text)

    # Style selection
    if is_first:
        style = "Intro"
    elif visual_type == "keyword":
        style = "Keyword"
    elif visual_type == "point":
        style = "Point"
    else:
        style = "Detail"

    # Animation selection: LLM choice > visual_type default
    anim_key = animation_style if (animation_style and animation_style in _ANIM_TAGS) \
               else _DEFAULT_ANIM.get("intro" if is_first else visual_type, "fade")
    tags = _ANIM_TAGS[anim_key]

    events = []
    cursor  = seg_start
    seg_end = seg_start + seg_dur
    # 発話長は文字数ではなくモーラ数に比例する。文字数比で割ると、漢字の多い
    # キューほど早く進み、実測で最大1.36秒ぶん字幕が音声を追い越していた。
    total_weight = sum(_mora_weight(ln) for grp in cue_groups for ln in grp) or 1.0
    for i, grp in enumerate(cue_groups):
        grp_weight = sum(_mora_weight(ln) for ln in grp) or 1.0
        cue_end    = min(cursor + seg_dur * (grp_weight / total_weight), seg_end)
        body       = "\\N".join(_escape(ln) for ln in grp)
        # 強いアニメーションは段落の頭だけ。同じ話が続いている途中で毎回
        # 60%→112%→100% と跳ねると、1段落あたり5〜6回「変形中で読めない
        # 字幕」が発生する（実測: 全キューの約0.37秒 × 69キュー ≒ 26秒）。
        cue_tags = tags if i == 0 else _CONT_TAG
        events.append(
            f"Dialogue: 0,{_to_ass_time(cursor)},{_to_ass_time(cue_end)},{style},,0,0,0,,{cue_tags}{body}"
        )
        cursor = cue_end
    return events


def generate_ass(segments: list[dict], output_path: str, channel: dict = None) -> str:
    ch          = channel or {}
    accent      = tuple(ch.get("accent_color", [50, 200, 80]))
    chan_name   = ch.get("badge_label", "NEWS")
    header      = _make_header(accent, chan_name)

    lines       = [header.rstrip()]
    total_dur   = sum(s["duration_sec"] for s in segments)
    vid_end     = _to_ass_time(total_dur)
    today       = date.today().strftime("%Y.%m.%d")

    # イントロ台紙は中央にチャンネル名を大きく出すので、同じ文字列を左上バナー
    # にも同時に出すと画面内に同じ語が2つ見える。バナーはイントロが明けてから
    # 出す。
    banner_start = _to_ass_time(segments[0]["duration_sec"]) if segments else "0:00:00.00"
    lines.append(f"Dialogue: 0,{banner_start},{vid_end},Banner_L,,0,0,0,,{chan_name}")
    lines.append(f"Dialogue: 0,{banner_start},{vid_end},Banner_R,,0,0,0,,{today}")

    cursor = 0.0
    for i, seg in enumerate(segments):
        lines.extend(_format_events(
            cursor, seg["duration_sec"],
            seg["text"],
            seg.get("visual_type", "detail"),
            is_first=(i == 0),
            animation_style=seg.get("animation_style"),
        ))
        cursor += seg["duration_sec"]

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")

    logger.info(f"Subtitle file generated: {output_path} ({len(segments)} cues, {cursor:.1f}s total)")
    return output_path


def generate_shorts_ass(segments: list[dict], output_path: str,
                        channel: dict = None, max_dur: float = 58.0) -> str:
    ch     = channel or {}
    accent = tuple(ch.get("accent_color", [50, 200, 80]))
    r, g, b = accent
    accent_hex = _ass_color(min(255, r + 60), min(255, g + 60), min(255, b + 60))

    # ⚠ 以前のスタイルは BorderStyle=3(不透明ボックス)なのに Outline=0 で、
    # ASS の仕様上「ボックスの厚み = Outline」なので縁もボックスも一切描かれず、
    # 白文字が明るいキャラ/背景の上に素の塗りだけで乗っていた
    # (実測コントラスト比 1.85:1、WCAG の大文字最低基準 3.0:1 未満)。
    # 実機の Shorts では字幕がほぼ読めない状態だった。縁取り方式に変更する。
    #
    # MarginV も 140(＝下端 y=1780) では、YouTube Shorts のタイトル/チャンネル名/
    # 説明の帯(概ね y=1550〜1850)に完全に埋もれる。380 に上げて帯の上に出す。
    header = f"""\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes
Timer: 100.0000

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,Noto Sans CJK JP,80,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,1,9,4,2,70,70,{_SHORTS_MARGIN_V},1
Style: Hook,Noto Sans CJK JP,86,{accent_hex},&H00FFFFFF,&H00000000,&H00000000,-1,0,1,10,4,2,70,70,{_SHORTS_MARGIN_V},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    event_lines = [header.rstrip()]
    cursor = 0.0

    for i, seg in enumerate(segments):
        if cursor >= max_dur:
            break
        seg_end = min(cursor + seg["duration_sec"], max_dur)
        seg_dur = seg_end - cursor
        style   = "Hook" if i < 2 else "Main"

        # 縦型専用のタグ表を使う。横型の _ANIM_TAGS を参照してはいけない
        # (\move の座標が 1920×1080 前提で、Shorts では画面中央・右端切れになる)。
        anim_key = seg.get("animation_style")
        tags = _SHORTS_ANIM_TAGS[anim_key] if (anim_key and anim_key in _SHORTS_ANIM_TAGS) \
               else _SHORTS_ANIM_POOL[i % len(_SHORTS_ANIM_POOL)]

        # Split long narration into cues. In the 1080px-wide vertical frame only
        # ~10 full-width JP chars fit per line at these font sizes (margins 70+70
        # leave 940px; 10×80 = 800px), so wider lines overflowed the right edge.
        cue_groups   = _split_into_cues(seg["text"], max_chars=10, max_lines=2)
        # 本編と同じくモーラ数で配分する（文字数比だと漢字の多いキューで
        # 字幕が音声を追い越す）
        total_weight = sum(_mora_weight(ln) for grp in cue_groups for ln in grp) or 1.0
        sub_cursor   = cursor
        for ci, grp in enumerate(cue_groups):
            grp_weight = sum(_mora_weight(ln) for ln in grp) or 1.0
            cue_end    = min(sub_cursor + seg_dur * (grp_weight / total_weight), seg_end)
            body       = "\\N".join(_escape(ln) for ln in grp)
            cue_tags   = tags if ci == 0 else _CONT_TAG
            event_lines.append(
                f"Dialogue: 0,{_to_ass_time(sub_cursor)},{_to_ass_time(cue_end)},{style},,0,0,0,,{cue_tags}{body}"
            )
            sub_cursor = cue_end

        cursor += seg["duration_sec"]

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(event_lines) + "\n")
    logger.info(f"Shorts subtitle generated: {output_path}")
    return output_path
