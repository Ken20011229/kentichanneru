"""slide_gen.py — Two-layer 1920×1080 slide system.

generate_slides() returns (plate_paths, content_paths):
  plate_paths   — static chrome frames (background + characters + badge + accent stripe)
  content_paths — animated content images (image/keyword graphic, or None for intro slides)

The FFmpeg composer overlays content_paths onto plate_paths with a zoompan animation,
while plate frames (including characters) remain completely static.
"""

import logging
import math
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from src.images.safe_open import safe_open_rgba

logger = logging.getLogger(__name__)

# ── Canvas dimensions ──────────────────────────────────────────────────────────
W, H = 1920, 1080

# ── Character size / safe area ─────────────────────────────────────────────────
# キャラの高さ比。0.72 だと ずんだもん が x=1486 から、つむぎ が x=0〜314 を
# 占有し、コンテンツ枠(220〜1700)・イントロタイトル・字幕が全部キャラの下に
# 潜って見切れていた(本番動画で実測)。0.56 に下げると占有幅は
# 右 1582〜1920 / 左 0〜244 になり、下の CONTENT/字幕領域と排他になる。
CHAR_RATIO   = 0.56
CHAR_SAFE_X1 = 260    # 左キャラの右端 + 余白
CHAR_SAFE_X2 = 1566   # 右キャラの左端 - 余白

# Content overlay area (between characters, passed to composer.py)
CONTENT_X1, CONTENT_Y1 = 280, 100
CONTENT_X2, CONTENT_Y2 = 1560, 790
CONTENT_W  = CONTENT_X2 - CONTENT_X1   # 1280
CONTENT_H  = CONTENT_Y2 - CONTENT_Y1   # 690

# ── Colors ─────────────────────────────────────────────────────────────────────
BG_COLOR = (252, 247, 238)

DECO_CIRCLES = [
    (0.07, 0.30, 140, (245, 200, 90,  55)),
    (0.92, 0.18, 110, (100, 210, 200, 50)),
    (0.88, 0.52,  90, (200, 175, 240, 45)),
    (0.14, 0.82, 100, (255, 155, 175, 40)),
    (0.55, 0.08,  70, (180, 230, 180, 35)),
]

_COLOR_DARK  = (40,  40,  50)
_COLOR_WHITE = (255, 255, 255)
_DEFAULT_ACCENT = (245, 166, 35)

_SECTION_LABELS = ["はじめに", "概要", "解説", "詳細", "まとめ"]


# ── Font helpers ───────────────────────────────────────────────────────────────

def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _tw(draw, text, font) -> int:
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0]


def _th(draw, text, font) -> int:
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[3] - bb[1]


def _wrap_px(draw, text, font, max_w) -> list[str]:
    lines, cur = [], ""
    for ch in text:
        test = cur + ch
        if _tw(draw, test, font) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur = test
    if cur:
        lines.append(cur)
    return lines


# ── Background builders ────────────────────────────────────────────────────────

def _photo_bg(path: str, dark: bool = False) -> Image.Image:
    try:
        img = safe_open_rgba(path)
        ir = img.width / img.height
        cr = W / H
        nw, nh = (int(H * ir), H) if ir > cr else (W, int(W / ir))
        img = img.resize((nw, nh), Image.LANCZOS)
        l, t = (nw - W) // 2, (nh - H) // 2
        img = img.crop((l, t, l + W, t + H))
        img = img.filter(ImageFilter.GaussianBlur(radius=12))
        ov = (8, 10, 20, 195) if dark else (252, 247, 238, 165)
        return Image.alpha_composite(img, Image.new("RGBA", (W, H), ov))
    except Exception as e:
        logger.warning(f"Photo background failed: {e}")
        return Image.new("RGBA", (W, H), (10, 12, 22, 255) if dark else (*BG_COLOR, 255))


def _draw_deco_circles() -> Image.Image:
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for rx, ry, r, color in DECO_CIRCLES:
        cx, cy = int(W * rx), int(H * ry)
        d.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=color)
    return layer.filter(ImageFilter.GaussianBlur(radius=28))


def _make_bg(bg_image_path=None) -> Image.Image:
    canvas = _photo_bg(bg_image_path) if bg_image_path else Image.new("RGBA", (W, H), (*BG_COLOR, 255))
    canvas = Image.alpha_composite(canvas, _draw_deco_circles())
    rule = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rule)
    for y in range(0, H, 32):
        rd.line([(0, y), (W, y)], fill=(180, 165, 140, 14))
    return Image.alpha_composite(canvas, rule)


def _make_dark_bg(accent, bg_image_path=None) -> Image.Image:
    r, g, b = accent
    canvas = _photo_bg(bg_image_path, dark=True) if bg_image_path else Image.new("RGBA", (W, H), (10, 12, 22, 255))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for cx, cy, radius, alpha in [
        (int(W * 0.72), int(H * 0.30), 340, 20),
        (int(W * 0.18), int(H * 0.70), 240, 12),
        (int(W * 0.50), int(H * 0.85), 200,  8),
    ]:
        gd.ellipse([(cx - radius, cy - radius), (cx + radius, cy + radius)],
                   fill=(r, g, b, alpha))
    return Image.alpha_composite(canvas, glow.filter(ImageFilter.GaussianBlur(radius=90)))


# ── Character helpers ──────────────────────────────────────────────────────────

def _paste_char(canvas, path, h_ratio, side="right", flip=False, dim=False):
    """キャラを貼る。dim=True は「今は喋っていない側」の表現。

    表情差分アセットは実測で全てピクセル完全一致（zundamon.png/1/2 が同一）
    のため、差分を切り替えても画は1ミリも変わらない。代わりに待機側を暗く
    薄くして少し沈めることで、どちらが喋っているかを画面で示す。
    """
    fallback = W if side == "right" else 0
    if not path or not Path(path).exists():
        return canvas, fallback
    try:
        img = safe_open_rgba(path)
        if flip:
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        target_h = int(H * h_ratio)
        ratio    = target_h / img.height
        target_w = int(img.width * ratio)
        img      = img.resize((target_w, target_h), Image.LANCZOS)
        if dim:
            alpha = img.getchannel("A").point(lambda a: int(a * 0.62))
            img   = ImageEnhance.Brightness(img.convert("RGB")).enhance(0.72)
            img   = img.convert("RGBA")
            img.putalpha(alpha)
        bleed    = int(target_w * 0.06)
        if side == "right":
            char_x     = W - target_w + bleed
            inner_edge = char_x
        else:
            char_x     = -bleed
            inner_edge = target_w - bleed
        # 待機側はわずかに下げて奥行きを出す
        canvas.paste(img, (char_x, H - target_h - 6 + (8 if dim else 0)), img)
        return canvas, inner_edge
    except Exception as e:
        logger.warning(f"Character paste failed ({side}): {e}")
        return canvas, fallback


def _char_variant(base_path, suffix):
    if not base_path:
        return base_path
    p = Path(base_path)
    alt = p.parent / f"{p.stem}{suffix}{p.suffix}"
    return str(alt) if alt.exists() else base_path


def _resolve_char_paths(char_cfg, visual_type):
    right = char_cfg.get("image_path", "")
    left  = char_cfg.get("left_image_path", "")
    if visual_type == "keyword":
        right = _char_variant(right, "1")
        left  = _char_variant(left,  "1")
    elif visual_type == "point":
        right = _char_variant(right, "2")
    return right, left


def _paste_characters(canvas, right_path, left_path, char_ratio=CHAR_RATIO):
    """Paste both characters at fixed positions. Returns canvas (characters don't move)."""
    canvas, _ = _paste_char(canvas, left_path,  char_ratio, side="left",  flip=True)
    canvas, _ = _paste_char(canvas, right_path, char_ratio, side="right")
    return canvas


# ── Chrome element drawers ─────────────────────────────────────────────────────

def _draw_centered(draw, text, font, box, fill):
    """box=(x1,y1,x2,y2) の中に text を光学的に中央揃えで描く。

    `textbbox()[3]-[1]` はインクの高さで、描画原点からの左/上ベアリングを
    含まない。それを高さとして中央寄せに使うとベアリングの分だけ上下に
    ずれる(バッジ内の数字が下寄りに見えていた原因)。ベアリングを引く。
    """
    x1, y1, x2, y2 = box
    bb = draw.textbbox((0, 0), text, font=font)
    w, h = bb[2] - bb[0], bb[3] - bb[1]
    draw.text(
        (x1 + (x2 - x1 - w) // 2 - bb[0], y1 + (y2 - y1 - h) // 2 - bb[1]),
        text, font=font, fill=fill,
    )


def _draw_section_badge(draw, fp, accent, section_num, badge_label, x=36, y=30):
    SQ = 54
    font_sq    = _font(fp, 32)
    font_badge = _font(fp, 30)
    draw.rounded_rectangle([(x, y), (x + SQ, y + SQ)], radius=8, fill=(*accent, 255))
    _draw_centered(draw, str(section_num), font_sq, (x, y, x + SQ, y + SQ), _COLOR_WHITE)
    bw = _tw(draw, badge_label, font_badge)
    cx = x + SQ + 10
    cw = bw + 28
    draw.rounded_rectangle([(cx, y), (cx + cw, y + SQ)], radius=8, fill=(35, 35, 35, 235))
    _draw_centered(draw, badge_label, font_badge, (cx, y, cx + cw, y + SQ), _COLOR_WHITE)


def _draw_accent_stripe(draw, accent):
    draw.rectangle([(0, H - 6), (W, H)], fill=(*accent, 255))


def _draw_content_border(draw, accent):
    """Subtle accent border marking the content overlay area on the plate."""
    draw.rounded_rectangle(
        [(CONTENT_X1 - 3, CONTENT_Y1 - 3), (CONTENT_X2 + 3, CONTENT_Y2 + 3)],
        radius=18, outline=(*accent, 55), width=2,
    )


# ── Frame plate builders ───────────────────────────────────────────────────────

def generate_character_overlay(config: dict, output_path: str,
                               visual_type: str = None,
                               speaker: str = None) -> str:
    """Generate a static RGBA character overlay (both characters, transparent background).

    visual_type を渡すと `zundamon1.png` / `zundamon2.png` のような表情差分
    アセットを選択する。差分ファイルが無ければベース画像にフォールバックする。
    speaker ("right"/"left") を渡すと、喋っていない側を減光して沈める。
    """
    char_cfg   = config.get("character", {})
    right_path, left_path = _resolve_char_paths(char_cfg, visual_type)

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    canvas, _ = _paste_char(canvas, left_path,  CHAR_RATIO, side="left", flip=True,
                            dim=(speaker == "right"))
    canvas, _ = _paste_char(canvas, right_path, CHAR_RATIO, side="right",
                            dim=(speaker == "left"))

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, "PNG")
    logger.info(
        f"Character overlay generated: {output_path} "
        f"(variant={visual_type or 'base'}, speaker={speaker or 'both'})"
    )
    return output_path


def _variant_suffix(visual_type: str) -> str:
    """visual_type に対応する表情差分のファイル接尾辞。"""
    if visual_type == "keyword":
        return "1"
    if visual_type == "point":
        return "2"
    return ""


def build_character_timeline(config: dict, segments: list[dict],
                             durations: list[float], output_dir: str,
                             speaker_sides: list[str] | None = None) -> list[dict]:
    """話者ごとのキャラオーバーレイと、その表示区間を作って返す。

    返り値: [{"path": png, "ranges": [(start_sec, end_sec), ...]}, ...]

    ⚠ 区間の切り替えは `visual_type` ではなく **話者** で行う。
    VOICEVOX は偶数セグメント=右(ずんだもん)/奇数=左(つむぎ)で声を切り替えて
    いるのに、以前は visual_type で表情差分を選んでいたため、
    「つむぎが喋っているのにずんだもんの口が開く」状態だった（実測で確認）。
    さらに visual_type=="keyword" では左右とも差分1になり、両方の口が同時に
    開いていた。

    キャラはスライドのクロスフェードに巻き込まれてはいけない(過去に背景と
    一緒に動く不具合を出している)ため、クリップには焼き込まず最終合成で
    `overlay ... enable='between(t,...)'` として乗せる。入力本数は話者数(最大2)
    で頭打ちになるのでスライド枚数に依存しない。
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    ranges_by_speaker: dict[str, list[tuple[float, float]]] = {}
    cursor = 0.0
    for i, seg in enumerate(segments):
        dur = durations[i] if i < len(durations) else 0.0
        if speaker_sides and i < len(speaker_sides) and speaker_sides[i]:
            side = speaker_sides[i]
        else:
            # TTS 側が話者情報を返さない(edge_tts など)場合は交代しない
            side = "both"
        ranges_by_speaker.setdefault(side, []).append((cursor, cursor + dur))
        cursor += dur

    layers = []
    for side, ranges in ranges_by_speaker.items():
        out_path = os.path.join(output_dir, f"char_{side}.png")
        try:
            generate_character_overlay(
                config, out_path,
                speaker=None if side == "both" else side,
            )
        except Exception as e:
            logger.warning(f"Character overlay '{side}' failed: {e}")
            continue
        layers.append({"path": out_path, "ranges": ranges})

    logger.info(
        f"Character timeline: {len(layers)} speaker layer(s), "
        f"{sum(len(l['ranges']) for l in layers)} slot(s)"
    )
    return layers


def _plate_intro(fp, title, accent, badge_label, bg_image_path=None) -> Image.Image:
    """INTRO plate: dark slide with title baked in. Characters are separate overlay."""
    canvas = _make_dark_bg(accent, bg_image_path)
    draw   = ImageDraw.Draw(canvas)

    font_badge = _font(fp, 30)
    bw = _tw(draw, badge_label, font_badge)
    draw.text(((W - bw) // 2, 52), badge_label, font=font_badge, fill=(*accent, 220))
    draw.rectangle([(W // 2 - 160, 98), (W // 2 + 160, 101)], fill=(*accent, 140))

    # タイトルは必ずキャラの内側に収める。以前は x=264〜1664 に描いていたため
    # 右キャラ(旧 x=1486〜)の裏に末尾数文字が隠れ、本番動画で「…で最長」の
    # ように読めない状態で出ていた。
    text_x  = max(CONTENT_X1 + 44, CHAR_SAFE_X1 + 20)
    inner_w = CHAR_SAFE_X2 - text_x - 20
    MAX_TITLE_LINES = 3

    font_title, lines = None, None
    for size in (96, 84, 76, 64, 54, 44, 38, 32):
        ft         = _font(fp, size)
        full_lines = _wrap_px(draw, title, ft, inner_w)
        if len(full_lines) <= MAX_TITLE_LINES:
            font_title, lines = ft, full_lines
            break

    if font_title is None:
        # Doesn't fit in 2 lines even at the smallest size — truncate with ellipsis.
        font_title = _font(fp, 32)
        lines = _wrap_px(draw, title, font_title, inner_w)[:MAX_TITLE_LINES]
        last = lines[-1]
        while last and _tw(draw, last + "…", font_title) > inner_w:
            last = last[:-1]
        lines[-1] = last + "…"

    lh    = max((_th(draw, ln, font_title) for ln in lines), default=44) + 22
    total = lh * len(lines)
    ty    = 130 + max(0, (int(H * 0.48) - total) // 2)
    for ln in lines:
        draw.text((text_x, ty), ln, font=font_title, fill=(255, 255, 255, 255))
        ty += lh

    draw.rectangle([(0, 0), (W, 5)],      fill=(*accent, 255))
    draw.rectangle([(0, H - 5), (W, H)],  fill=(*accent, 255))
    return canvas.convert("RGB")


def _plate_standard(fp, accent, section_num, badge_label,
                    bg_image_path=None) -> Image.Image:
    """Standard plate: background + badge + content border + accent stripe. Characters are separate overlay."""
    canvas = _make_bg(bg_image_path)
    draw   = ImageDraw.Draw(canvas)
    _draw_section_badge(draw, fp, accent, section_num, badge_label)
    _draw_content_border(draw, accent)
    _draw_accent_stripe(draw, accent)
    return canvas.convert("RGB")


# ── Content image builders ─────────────────────────────────────────────────────

def _content_from_image(image_path: str) -> Image.Image:
    """Load and fit an image into the content area dimensions."""
    img = safe_open_rgba(image_path).convert("RGB")
    ir  = img.width / img.height
    cr  = CONTENT_W / CONTENT_H
    if ir > cr:
        nw, nh = int(CONTENT_H * ir), CONTENT_H
    else:
        nw, nh = CONTENT_W, int(CONTENT_W / ir)
    img = img.resize((nw, nh), Image.LANCZOS)
    l, t = (nw - CONTENT_W) // 2, (nh - CONTENT_H) // 2
    return img.crop((l, t, l + CONTENT_W, t + CONTENT_H))


def _content_photo_caption(image_path: str, keyword: str, accent: tuple,
                           fp: str) -> Image.Image:
    """写真をコンテンツ枠に収め、キーワードを下部のテロップ帯として重ねる。

    以前は keyword があると写真より優先して `_content_keyword`（暗い単色板に
    単語1つ）を描いていた。バリデータが keyword をほぼ必ず埋めるため、実質
    全セグメントの主カットが「フレームの42.6%を占める黒い板に漢字3文字」に
    なっていた（実測: 板の94〜96%が単色の暗部）。写真を主役にして、keyword は
    その上のテロップにする。
    """
    img = _content_from_image(image_path).convert("RGBA")
    if not keyword:
        return img.convert("RGB")

    draw   = ImageDraw.Draw(img)
    band_h = int(CONTENT_H * 0.22)
    band_y = CONTENT_H - band_h
    band   = Image.new("RGBA", (CONTENT_W, band_h), (10, 12, 20, 200))
    img.paste(band, (0, band_y), band)
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, band_y), (CONTENT_W, band_y + 6)], fill=(*accent, 255))

    for size in (112, 96, 84, 72, 62, 52, 44):
        fk = _font(fp, size)
        if _tw(draw, keyword, fk) <= CONTENT_W - 80:
            break
    bb = draw.textbbox((0, 0), keyword, font=fk)
    kx = (CONTENT_W - (bb[2] - bb[0])) // 2 - bb[0]
    ky = band_y + (band_h - (bb[3] - bb[1])) // 2 - bb[1]
    draw.text((kx + 3, ky + 3), keyword, font=fk, fill=(0, 0, 0, 190))
    draw.text((kx, ky), keyword, font=fk, fill=_COLOR_WHITE)
    return img.convert("RGB")


def _content_keyword(fp, keyword, accent, section_num, sec_label) -> Image.Image:
    """Keyword graphic: dark background with large accent keyword + section info.

    写真が1枚も無いときの最後の手段。写真があるなら
    `_content_photo_caption` を使うこと。
    """
    r, g, b = accent
    img = Image.new("RGBA", (CONTENT_W, CONTENT_H), (18, 20, 32, 255))

    # Accent glow blob
    glow = Image.new("RGBA", (CONTENT_W, CONTENT_H), (0, 0, 0, 0))
    gd   = ImageDraw.Draw(glow)
    gd.ellipse([(-60, -60), (CONTENT_W // 2 + 100, CONTENT_H + 60)], fill=(r, g, b, 22))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(90)))

    draw = ImageDraw.Draw(img)

    # NOTE: セクション番号とラベルはフレーム側(_plate_standard の
    # _draw_section_badge)が既に左上に描いている。ここにも描くと画面上に
    # 「2 概要」が縦に2つ並ぶ二重表示になっていたため、パネル内では描かず
    # キーワードに全面積を使う。

    # Large keyword centered in the full panel
    if keyword:
        start_y  = 56
        avail_h  = CONTENT_H - start_y - 64
        avail_w  = CONTENT_W - 72

        for size in (150, 132, 112, 96, 80, 66, 54, 48):
            fk   = _font(fp, size)
            ls   = _wrap_px(draw, keyword, fk, avail_w)
            lh   = max((_th(draw, ln, fk) for ln in ls), default=size) + 22
            if lh * len(ls) <= avail_h and len(ls) <= 3:
                font_kw, kw_lines = fk, ls
                break
        else:
            font_kw = _font(fp, 48)
            kw_lines = _wrap_px(draw, keyword, font_kw, avail_w)

        kw_lh    = max((_th(draw, ln, font_kw) for ln in kw_lines), default=60) + 22
        total_kh = kw_lh * len(kw_lines)
        ty       = start_y + max(0, (avail_h - total_kh) // 2)

        for ln in kw_lines:
            lx = 36 + max(0, (avail_w - _tw(draw, ln, font_kw)) // 2)
            g_layer = Image.new("RGBA", (CONTENT_W, CONTENT_H), (0, 0, 0, 0))
            ImageDraw.Draw(g_layer).text((lx, ty), ln, font=font_kw, fill=(*accent, 150))
            img  = Image.alpha_composite(img, g_layer.filter(ImageFilter.GaussianBlur(18)))
            draw = ImageDraw.Draw(img)
            draw.text((lx, ty), ln, font=font_kw, fill=_COLOR_WHITE,
                      stroke_width=2, stroke_fill=(0, 0, 0, 80))
            ty += kw_lh

    draw.rectangle([(0, CONTENT_H - 7), (CONTENT_W, CONTENT_H)], fill=(*accent, 255))
    return img.convert("RGB")


def _content_section(fp, accent, section_num, sec_label) -> Image.Image:
    """Minimal section graphic when no keyword or image is available."""
    r, g, b = accent
    img  = Image.new("RGBA", (CONTENT_W, CONTENT_H), (18, 20, 32, 255))
    draw = ImageDraw.Draw(img)

    # Ghost section number
    for size in (320, 260, 220):
        ft = _font(fp, size)
        if _tw(draw, str(section_num), ft) < CONTENT_W - 60:
            font_ghost = ft
            break
    else:
        font_ghost = _font(fp, 220)

    ns   = str(section_num)
    nw_n = _tw(draw, ns, font_ghost)
    nh_n = _th(draw, ns, font_ghost)
    ghost = Image.new("RGBA", (CONTENT_W, CONTENT_H), (0, 0, 0, 0))
    ImageDraw.Draw(ghost).text(
        ((CONTENT_W - nw_n) // 2, (CONTENT_H - nh_n) // 2),
        ns, font=font_ghost,
        fill=(min(255, r + 40), min(255, g + 40), min(255, b + 40), 55),
    )
    img  = Image.alpha_composite(img, ghost.filter(ImageFilter.GaussianBlur(6)))
    draw = ImageDraw.Draw(img)

    # Section label
    font_label = _font(fp, 64)
    lw = _tw(draw, sec_label, font_label)
    lh = _th(draw, sec_label, font_label)
    draw.text(((CONTENT_W - lw) // 2, (CONTENT_H - lh) // 2),
              sec_label, font=font_label, fill=(*accent, 255))

    draw.rectangle([(0, CONTENT_H - 7), (CONTENT_W, CONTENT_H)], fill=(*accent, 255))
    return img.convert("RGB")


# ── Public entry point ─────────────────────────────────────────────────────────

def generate_slides(
    segments: list[dict],
    title: str,
    config: dict,
    output_dir: str,
    bg_image_path: str = None,
    segment_images: dict[int, str] | None = None,
    durations: list[float] | None = None,
    photo_pool: list[str] | None = None,
    max_cut_sec: float = 4.5,
    max_cuts_per_segment: int = 8,
) -> tuple[list[str], list[str | None], list[float] | None]:
    """Generate frame plates and content images per segment.

    1セグメント = 1カットではなく、ナレーションが長いセグメントは複数カットに
    割る。以前は1セグメントの音声尺(平均24秒、イントロは27秒)がそのまま1枚の
    静止画の表示時間になっていて、本番動画の冒頭27秒はピクセル単位で完全に
    静止していた。

    max_cut_sec は解説系の標準的なカット長(3〜5秒)に合わせている。9.0 だと
    平均24.6秒のセグメントが3カットにしか割れず、1カット8秒超のままだった。

    Returns:
        (plate_paths, content_paths, slide_durations, cuts_per_slide)
        plate_paths     — static chrome frames (1920×1080), characters are a separate overlay
        content_paths   — content images (CONTENT_W×CONTENT_H) to animate, or None for intro
        slide_durations — カットごとの表示秒(durations 未指定なら None)
        cuts_per_slide  — セグメントごとのカット数。plate_paths はカット単位で
                          平坦化されているため、これが無いと composer 側で
                          「どこがセグメントの切れ目か」を復元できない。
                          セグメント内のカットは無劣化 concat、セグメント境界だけ
                          xfade する分岐に使う（xfade は結合のたびに累積結果を
                          再エンコードするので、カット単位でつなぐと総エンコード量が
                          O(N^2) になり 64 カットで 60 分の枠に収まらなくなる）。
    """
    os.makedirs(output_dir, exist_ok=True)
    plates_dir  = os.path.join(output_dir, "plates")
    content_dir = os.path.join(output_dir, "content")
    os.makedirs(plates_dir,  exist_ok=True)
    os.makedirs(content_dir, exist_ok=True)

    fp          = config.get("thumbnail", {}).get("font_path", "")
    ch          = config.get("active_channel", {})
    accent      = tuple(ch.get("accent_color", list(_DEFAULT_ACCENT)))
    badge_label = ch.get("badge_label", "テック速報")
    total       = max(len(segments), 1)

    def _section(idx):
        s = min(int(idx * len(_SECTION_LABELS) / total), len(_SECTION_LABELS) - 1)
        return s + 1, _SECTION_LABELS[s]

    plate_paths     = []
    content_paths   = []
    slide_durations = []
    cuts_per_slide  = []

    pool     = [p for p in (photo_pool or []) if p and Path(p).exists()]
    pool_pos = 0
    # ⚠ 使用済み集合は動画全体で共有する。以前はセグメントごとに作り直して
    # いたため、「このセグメントで未使用」しか見ておらず、隣り合うセグメントで
    # 同じ写真が連続して出ていた。
    used_photos: set = set()

    def _section_bg(sec_num: int) -> str | None:
        """台紙の背景写真。セクション(5区分)ごとに差し替える。

        以前は photo_pool[0] を全セグメントに渡していたため、5〜7分すべてが
        同じ写真をぼかした下地だった（実測: 4分27秒の全フレームで同一）。
        """
        if not pool:
            return bg_image_path
        return pool[(max(sec_num, 1) - 1) % len(pool)]

    def _next_photo() -> str | None:
        """写真プールから、まだ使っていない1枚を取り出す。

        プールを一巡したら使用履歴をリセットして再利用する（枚数が足りない
        ときに情報量ゼロのセクション板へ落とすより、再登場のほうがマシ）。
        """
        nonlocal pool_pos
        if not pool:
            return None
        if len(used_photos) >= len(pool):
            used_photos.clear()
        for _ in range(len(pool)):
            p = pool[pool_pos % len(pool)]
            pool_pos += 1
            if p not in used_photos:
                used_photos.add(p)
                return p
        return None

    for seg in segments:
        idx         = seg.get("segment_index", 0)
        keyword     = seg.get("keyword", "")
        visual_type = seg.get("visual_type", "detail")
        if idx == 0:
            visual_type = "intro"

        sec_num, sec_label = _section(idx)
        slide_label = badge_label if visual_type == "intro" else sec_label
        seg_img     = (segment_images or {}).get(idx)
        seg_dur     = durations[idx] if (durations and idx < len(durations)) else None

        std_plate_path = os.path.join(plates_dir, f"plate_{idx:03d}.jpg")

        try:
            # ── そのセグメントで見せるビジュアルを組み立てる ──────────────
            # cuts: [(plate_path, content_path|None, kind), ...]
            cuts: list[tuple[str, str | None, str]] = []

            if visual_type == "intro":
                intro_plate_path = os.path.join(plates_dir, f"plate_{idx:03d}_title.jpg")
                _plate_intro(fp, title, accent, slide_label, bg_image_path) \
                    .save(intro_plate_path, "JPEG", quality=93)
                cuts.append((intro_plate_path, None, "intro"))

            std_plate = None
            if visual_type != "intro" or (seg_dur and seg_dur > max_cut_sec):
                std_plate = _plate_standard(fp, accent, sec_num, slide_label,
                                            bg_image_path=_section_bg(sec_num))
                std_plate.save(std_plate_path, "JPEG", quality=93)

            if visual_type != "intro":
                # ⚠ 優先順位は 写真 > キーワード板 > セクション板。
                # 以前は keyword が写真より優先だったが、バリデータが本文から
                # keyword を必ず埋め直すため `elif pool` に到達せず、実質すべての
                # 主カットが単色の黒板になっていた。keyword は写真を捨てる理由
                # ではなく、写真の上のテロップとして出す。
                c_path = os.path.join(content_dir, f"content_{idx:03d}.jpg")
                # seg_img は HF 生成画像でプール外。used_photos に混ぜると
                # 「プールを一巡したか」の判定がずれるので入れない。
                photo = seg_img if (seg_img and Path(seg_img).exists()) else _next_photo()
                if photo:
                    _content_photo_caption(photo, keyword, accent, fp) \
                        .save(c_path, "JPEG", quality=93)
                    cuts.append((std_plate_path, c_path, "photo+caption"))
                elif keyword:
                    _content_keyword(fp, keyword, accent, sec_num, sec_label) \
                        .save(c_path, "JPEG", quality=93)
                    cuts.append((std_plate_path, c_path, "keyword(no photo)"))
                else:
                    _content_section(fp, accent, sec_num, sec_label) \
                        .save(c_path, "JPEG", quality=93)
                    cuts.append((std_plate_path, c_path, "section(EMPTY)"))
                    logger.warning(
                        f"Slide {idx}: no photo and no keyword — falling back to an "
                        f"information-free section panel"
                    )

            # ── 長いセグメントは追加カットに割って画を更新する ──────────────
            if seg_dur and pool:
                want = min(int(math.ceil(seg_dur / max_cut_sec)), max_cuts_per_segment)
                while len(cuts) < want:
                    photo = _next_photo()
                    if not photo:
                        break
                    extra_path = os.path.join(
                        content_dir, f"content_{idx:03d}_{len(cuts):02d}.jpg"
                    )
                    _content_from_image(photo).save(extra_path, "JPEG", quality=93)
                    if std_plate is None:
                        std_plate = _plate_standard(fp, accent, sec_num, sec_label,
                                                    bg_image_path=_section_bg(sec_num))
                        std_plate.save(std_plate_path, "JPEG", quality=93)
                    cuts.append((std_plate_path, extra_path, "photo(extra)"))

        except Exception as e:
            logger.error(f"Slide {idx} generation failed ({visual_type}): {e}")
            fb_content = os.path.join(content_dir, f"content_{idx:03d}.jpg")
            _make_bg().convert("RGB").save(std_plate_path, "JPEG", quality=90)
            _content_section(fp, accent, sec_num, sec_label).save(fb_content, "JPEG", quality=90)
            cuts = [(std_plate_path, fb_content, "error")]

        for plate_p, content_p, _kind in cuts:
            plate_paths.append(plate_p)
            content_paths.append(content_p)
        cuts_per_slide.append(len(cuts))
        if seg_dur is not None:
            slide_durations.extend([seg_dur / len(cuts)] * len(cuts))

        logger.info(
            f"Slide {idx} [{visual_type}]: {len(cuts)} cut(s) "
            f"({', '.join(k for _, _, k in cuts)})"
            + (f" over {seg_dur:.1f}s" if seg_dur else "")
        )

    return plate_paths, content_paths, (slide_durations or None), cuts_per_slide
