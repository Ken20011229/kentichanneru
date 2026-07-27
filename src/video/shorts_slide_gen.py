"""shorts_slide_gen.py — Two-layer 1080×1920 vertical slides for YouTube Shorts.

generate_shorts_slides() returns (plate_paths, content_paths):
  plate_paths   — static chrome frames (character fixed at bottom, badge, stripe)
  content_paths — animated content images in the upper content zone, or None for intro

Content zone: x=0, y=CONTENT_Y1, w=1080, h=CONTENT_H (above the character)
"""
import logging
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from src.images.safe_open import safe_open_rgba

logger = logging.getLogger(__name__)

W, H = 1080, 1920
BG_COLOR     = (252, 247, 238)
_DARK_BG     = (18, 18, 28)
_COLOR_WHITE = (255, 255, 255)
_DEFAULT_ACCENT = (245, 166, 35)

# Character at bottom
# 0.63 だとキャラが縦1209px・横717px(画面幅の66%)を占め、情報が入る領域は
# 画面のわずか30%しか残っていなかった。縦動画で価値が高いのは中央帯なので
# キャラを小さくしてコンテンツ領域を広げる。
# 0.42 でもキャラ上端が y=1110 まで来るため、字幕(下端 y=1360)と重なる。
# 0.38 にするとキャラ上端が y=1187 になり、コンテンツ枠と字幕帯の下に収まる。
_CHAR_RATIO = 0.38
_CHAR_H     = int(H * _CHAR_RATIO)   # 729px
_CHAR_TOP   = H - _CHAR_H - 4        # 1187px

# Content area (above character)
CONTENT_X1 = 0
CONTENT_Y1 = 120
CONTENT_X2 = W
CONTENT_Y2 = _CHAR_TOP - 16          # 1094px
CONTENT_W  = W                        # 1080
CONTENT_H  = CONTENT_Y2 - CONTENT_Y1  # 974px

# Shorts はアプリ上部の検索/戻るオーバーレイと重なるため、バッジを下げる
_BADGE_TOP = 118

DECO_CIRCLES = [
    (0.12, 0.08, 130, (245, 200, 90,  55)),
    (0.88, 0.06, 110, (100, 210, 200, 50)),
    (0.92, 0.30,  90, (200, 175, 240, 45)),
    (0.08, 0.55, 120, (255, 155, 175, 40)),
    (0.50, 0.03,  70, (180, 230, 180, 35)),
]

_SECTION_LABELS = ["はじめに", "概要", "解説", "詳細", "まとめ"]


# ── Utilities ──────────────────────────────────────────────────────────────────

def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _tw(draw, text, font):
    return draw.textbbox((0, 0), text, font=font)[2]


def _th(draw, text, font):
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[3] - bb[1]


def _wrap_px(draw, text, font, max_w):
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

def _photo_bg(path, blur=14, overlay_alpha=160):
    try:
        img = safe_open_rgba(path)
        ir  = img.width / img.height
        cr  = W / H
        nw, nh = (int(H * ir), H) if ir > cr else (W, int(W / ir))
        img  = img.resize((nw, nh), Image.LANCZOS)
        img  = img.crop(((nw - W) // 2, (nh - H) // 2,
                         (nw - W) // 2 + W, (nh - H) // 2 + H))
        img  = img.filter(ImageFilter.GaussianBlur(radius=blur))
        return Image.alpha_composite(img, Image.new("RGBA", (W, H), (*BG_COLOR, overlay_alpha)))
    except Exception as e:
        logger.warning(f"Photo bg failed: {e}")
        return Image.new("RGBA", (W, H), (*BG_COLOR, 255))


def _make_base_bg(bg_image_path=None):
    deco = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d    = ImageDraw.Draw(deco)
    for rx, ry, r, color in DECO_CIRCLES:
        cx, cy = int(W * rx), int(H * ry)
        d.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=color)
    deco   = deco.filter(ImageFilter.GaussianBlur(radius=28))
    canvas = _photo_bg(bg_image_path) if bg_image_path else Image.new("RGBA", (W, H), (*BG_COLOR, 255))
    canvas = Image.alpha_composite(canvas, deco)
    rule   = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    rd     = ImageDraw.Draw(rule)
    for y in range(0, H, 32):
        rd.line([(0, y), (W, y)], fill=(180, 165, 140, 14))
    return Image.alpha_composite(canvas, rule)


# ── Character helpers ──────────────────────────────────────────────────────────

def _paste_char(canvas, path, side="right"):
    """Paste character at fixed bottom position — no animation."""
    if not path or not Path(path).exists():
        return canvas
    try:
        img = safe_open_rgba(path)
        if side == "left":
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        ratio    = _CHAR_H / img.height
        target_w = int(img.width * ratio)
        img      = img.resize((target_w, _CHAR_H), Image.LANCZOS)
        bleed    = int(target_w * 0.08)
        char_x   = W - target_w + bleed if side == "right" else -bleed
        canvas.paste(img, (char_x, H - _CHAR_H - 4), img)
    except Exception as e:
        logger.warning(f"Char paste failed ({side}): {e}")
    return canvas


# ── Chrome drawers ─────────────────────────────────────────────────────────────

def _draw_centered(draw, text, font, box, fill):
    """box=(x1,y1,x2,y2) 内に光学的中央で描く（ベアリング補正つき）。"""
    x1, y1, x2, y2 = box
    bb = draw.textbbox((0, 0), text, font=font)
    w, h = bb[2] - bb[0], bb[3] - bb[1]
    draw.text(
        (x1 + (x2 - x1 - w) // 2 - bb[0], y1 + (y2 - y1 - h) // 2 - bb[1]),
        text, font=font, fill=fill,
    )


def _draw_badge(draw, fp, accent, sec_num, label, x=30, y=_BADGE_TOP):
    SQ       = 52
    font_sq  = _font(fp, 30)
    font_lbl = _font(fp, 28)
    draw.rounded_rectangle([(x, y), (x + SQ, y + SQ)], radius=8, fill=(*accent, 255))
    _draw_centered(draw, str(sec_num), font_sq, (x, y, x + SQ, y + SQ), _COLOR_WHITE)
    bw  = _tw(draw, label, font_lbl)
    cx  = x + SQ + 10
    cw  = bw + 24
    draw.rounded_rectangle([(cx, y), (cx + cw, y + SQ)], radius=8, fill=(35, 35, 35, 235))
    _draw_centered(draw, label, font_lbl, (cx, y, cx + cw, y + SQ), _COLOR_WHITE)


def _draw_accent_stripe(draw, accent):
    # 画面最下部(y=H-8)は Shorts のタイトル/チャンネル名の帯に100%隠れて
    # 一度も見えていなかった。ブランドのアクセントは上端と、コンテンツ領域の
    # 下辺という「実際に見える位置」に置く。
    draw.rectangle([(0, 0), (W, 6)], fill=(*accent, 255))
    draw.rectangle([(0, CONTENT_Y2 + 4), (W, CONTENT_Y2 + 10)], fill=(*accent, 255))


def _draw_content_border(draw, accent):
    """Subtle border around the content area on the plate."""
    draw.rounded_rectangle(
        [(CONTENT_X1 + 4, CONTENT_Y1 - 3), (CONTENT_X2 - 4, CONTENT_Y2 + 3)],
        radius=16, outline=(*accent, 55), width=2,
    )


# ── Frame plate builders ───────────────────────────────────────────────────────

def _plate_intro(fp, accent, keyword, text, badge_label, char_path, side,
                 bg_image_path=None) -> Image.Image:
    """INTRO plate: accent gradient + large keyword as title. Self-contained, no content overlay."""
    # bg_image_path は引数にあるだけで一度も参照されておらず、Shorts の冒頭は
    # 常に無地のダークネイビーだった（画面の38%が完全な空白）。写真を暗く
    # 敷いて、最初の1秒から「何の話か」が伝わるようにする。
    if bg_image_path:
        canvas = _photo_bg(bg_image_path, blur=10, overlay_alpha=120)
        canvas = Image.alpha_composite(
            canvas, Image.new("RGBA", (W, H), (*_DARK_BG, 150))
        )
    else:
        canvas = Image.new("RGBA", (W, H), (*_DARK_BG, 255))

    # Subtle accent glow
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd   = ImageDraw.Draw(glow)
    r, g, b = accent
    gd.ellipse([(-100, -100), (W + 100, H // 2)], fill=(r, g, b, 28))
    canvas = Image.alpha_composite(canvas, glow.filter(ImageFilter.GaussianBlur(80)))

    canvas = _paste_char(canvas, char_path, side=side)
    draw   = ImageDraw.Draw(canvas)

    cur_y = CONTENT_Y1 + 60
    if keyword:
        for sz in (150, 130, 110, 92, 78, 64, 52):
            fk = _font(fp, sz)
            if _tw(draw, keyword, fk) <= W - 80:
                break
        bb   = draw.textbbox((0, 0), keyword, font=fk)
        kw_w = bb[2] - bb[0]
        kx   = (W - kw_w) // 2 - bb[0]
        draw.text((kx + 4, cur_y + 4), keyword, font=fk, fill=(0, 0, 0, 140))
        draw.text((kx, cur_y), keyword, font=fk, fill=_COLOR_WHITE)
        # 下線は「インクの高さ(bb[3]-bb[1])」ではなく、描画原点からグリフ下端
        # までの距離 bb[3] を使う。以前は前者を使っていたため、下線がグリフを
        # 貫通して打ち消し線のように見えていた。
        uy = cur_y + bb[3] + 14
        draw.rectangle([(kx + bb[0], uy), (kx + bb[0] + kw_w, uy + 8)], fill=(*accent, 255))

    # ⚠ ここにナレーション本文を描いてはいけない。ASS字幕が同じ文をもう一度
    # 表示するため、画面上に同じ文章が上下に二重で出る(本番Shortsで実際に
    # 発生していた)。ナレーション全文は字幕side の責務。

    draw.rectangle([(0, 0), (W, 5)],     fill=(*accent, 255))
    return canvas.convert("RGB")


def _plate_standard(fp, accent, sec_num, sec_label,
                    char_path, side, bg_image_path=None) -> Image.Image:
    """Standard plate: background + character fixed + badge + content border + stripe."""
    canvas = _make_base_bg(bg_image_path)
    canvas = _paste_char(canvas, char_path, side=side)
    draw   = ImageDraw.Draw(canvas)
    _draw_badge(draw, fp, accent, sec_num, sec_label)
    _draw_content_border(draw, accent)
    _draw_accent_stripe(draw, accent)
    return canvas.convert("RGB")


# ── Content image builders ─────────────────────────────────────────────────────

def _content_from_image(image_path: str) -> Image.Image:
    """Load and fit an image into the Shorts content area dimensions."""
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

    縦画面は横幅が狭く、単色板に単語だけだと情報量がゼロに近い。写真を主役に
    して keyword はテロップにする（本編の `slide_gen._content_photo_caption`
    と同じ方針）。
    """
    img = _content_from_image(image_path).convert("RGBA")
    if not keyword:
        return img.convert("RGB")

    draw   = ImageDraw.Draw(img)
    band_h = int(CONTENT_H * 0.18)
    band_y = CONTENT_H - band_h
    img.paste(Image.new("RGBA", (CONTENT_W, band_h), (10, 12, 20, 200)),
              (0, band_y))
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, band_y), (CONTENT_W, band_y + 6)], fill=(*accent, 255))

    for size in (104, 92, 80, 68, 58, 48, 40):
        fk = _font(fp, size)
        if _tw(draw, keyword, fk) <= CONTENT_W - 60:
            break
    bb = draw.textbbox((0, 0), keyword, font=fk)
    kx = (CONTENT_W - (bb[2] - bb[0])) // 2 - bb[0]
    ky = band_y + (band_h - (bb[3] - bb[1])) // 2 - bb[1]
    draw.text((kx + 3, ky + 3), keyword, font=fk, fill=(0, 0, 0, 190))
    draw.text((kx, ky), keyword, font=fk, fill=_COLOR_WHITE)
    return img.convert("RGB")


def _content_keyword(fp, keyword, accent, sec_num, sec_label) -> Image.Image:
    """Keyword graphic for Shorts content area (写真が1枚も無いときの最後の手段)。"""
    r, g, b = accent
    img = Image.new("RGBA", (CONTENT_W, CONTENT_H), (18, 20, 32, 255))

    glow = Image.new("RGBA", (CONTENT_W, CONTENT_H), (0, 0, 0, 0))
    gd   = ImageDraw.Draw(glow)
    gd.ellipse([(-40, -40), (CONTENT_W + 40, CONTENT_H // 2 + 40)], fill=(r, g, b, 24))
    img  = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(70)))
    draw = ImageDraw.Draw(img)

    # Keyword centered
    avail_w = CONTENT_W - 60
    avail_h = CONTENT_H - 100
    for size in (100, 86, 72, 60, 50):
        fk  = _font(fp, size)
        ls  = _wrap_px(draw, keyword, fk, avail_w)
        lh  = max((_th(draw, ln, fk) for ln in ls), default=size) + 18
        if lh * len(ls) <= avail_h and len(ls) <= 3:
            font_kw, kw_lines = fk, ls
            break
    else:
        font_kw  = _font(fp, 50)
        kw_lines = _wrap_px(draw, keyword, font_kw, avail_w)

    kw_lh    = max((_th(draw, ln, font_kw) for ln in kw_lines), default=55) + 18
    total_kh = kw_lh * len(kw_lines)
    ty       = (CONTENT_H - total_kh) // 2

    for ln in kw_lines:
        lx = 30 + max(0, (avail_w - _tw(draw, ln, font_kw)) // 2)
        g_layer = Image.new("RGBA", (CONTENT_W, CONTENT_H), (0, 0, 0, 0))
        ImageDraw.Draw(g_layer).text((lx, ty), ln, font=font_kw, fill=(*accent, 150))
        img  = Image.alpha_composite(img, g_layer.filter(ImageFilter.GaussianBlur(16)))
        draw = ImageDraw.Draw(img)
        draw.text((lx, ty), ln, font=font_kw, fill=_COLOR_WHITE,
                  stroke_width=2, stroke_fill=(0, 0, 0, 80))
        ty += kw_lh

    draw.rectangle([(0, CONTENT_H - 6), (CONTENT_W, CONTENT_H)], fill=(*accent, 255))
    return img.convert("RGB")


def _content_section(fp, accent, section_num, sec_label) -> Image.Image:
    """Minimal section graphic for Shorts content area."""
    r, g, b = accent
    img  = Image.new("RGBA", (CONTENT_W, CONTENT_H), (18, 20, 32, 255))
    draw = ImageDraw.Draw(img)

    for size in (240, 200, 170):
        ft = _font(fp, size)
        if _tw(draw, str(section_num), ft) < CONTENT_W - 40:
            font_ghost = ft
            break
    else:
        font_ghost = _font(fp, 170)

    ns   = str(section_num)
    nw_n = _tw(draw, ns, font_ghost)
    nh_n = _th(draw, ns, font_ghost)
    ghost = Image.new("RGBA", (CONTENT_W, CONTENT_H), (0, 0, 0, 0))
    ImageDraw.Draw(ghost).text(
        ((CONTENT_W - nw_n) // 2, (CONTENT_H - nh_n) // 2),
        ns, font=font_ghost, fill=(min(255, r + 40), min(255, g + 40), min(255, b + 40), 55),
    )
    img  = Image.alpha_composite(img, ghost.filter(ImageFilter.GaussianBlur(6)))
    draw = ImageDraw.Draw(img)

    font_label = _font(fp, 56)
    lw = _tw(draw, sec_label, font_label)
    lh = _th(draw, sec_label, font_label)
    draw.text(((CONTENT_W - lw) // 2, (CONTENT_H - lh) // 2),
              sec_label, font=font_label, fill=(*accent, 255))
    draw.rectangle([(0, CONTENT_H - 6), (CONTENT_W, CONTENT_H)], fill=(*accent, 255))
    return img.convert("RGB")


# ── Public entry point ────────────────────────────────────────────────────────

def generate_shorts_slides(
    segments: list[dict],
    title: str,
    config: dict,
    output_dir: str,
    bg_image_path: str = None,
    segment_images: dict | None = None,
    photo_pool: list[str] | None = None,
) -> tuple[list[str], list[str | None]]:
    """Generate Shorts frame plates and content images.

    Returns (plate_paths, content_paths) — same pattern as generate_slides().
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
    char_cfg    = config.get("character", {})
    right_path  = char_cfg.get("image_path", "")
    left_path   = char_cfg.get("left_image_path", "")

    seg_imgs = segment_images or {}
    total    = max(len(segments), 1)
    plate_paths   = []
    content_paths = []

    pool     = [p for p in (photo_pool or []) if p and Path(p).exists()]
    pool_pos = 0

    for idx, seg in enumerate(segments):
        keyword = seg.get("keyword", "")
        vtype   = seg.get("visual_type", "detail")
        if idx == 0:
            vtype = "intro"
        side    = "right" if idx % 2 == 0 else "left"
        char_p  = right_path if side == "right" else left_path

        s         = min(int(idx * len(_SECTION_LABELS) / total), len(_SECTION_LABELS) - 1)
        sec_num   = s + 1
        sec_label = badge_label if idx == 0 else _SECTION_LABELS[s]
        seg_img   = seg_imgs.get(idx)

        plate_path   = os.path.join(plates_dir,  f"plate_{idx:03d}.jpg")
        content_path = os.path.join(content_dir, f"content_{idx:03d}.jpg")

        try:
            if vtype == "intro":
                plate = _plate_intro(fp, accent, keyword, seg.get("text", ""),
                                     sec_label, char_p, side, bg_image_path)
                plate.save(plate_path, "JPEG", quality=93)
                plate_paths.append(plate_path)
                content_paths.append(None)
            else:
                plate = _plate_standard(fp, accent, sec_num, sec_label,
                                        char_p, side, bg_image_path)
                plate.save(plate_path, "JPEG", quality=93)
                plate_paths.append(plate_path)

                # ⚠ 優先順位は 写真 > キーワード板 > セクション板。
                # keyword を先に見ていたため、バリデータが必ず keyword を埋める
                # 現状では `elif pool` に一度も到達せず、Shorts の全カットが
                # 「画面の48.8%を占める黒板に4〜6文字」になっていた
                # (実測: 明部画素率 1.47〜1.91%)。
                photo = None
                if seg_img and Path(seg_img).exists():
                    photo = seg_img
                elif pool:
                    photo = pool[pool_pos % len(pool)]
                    pool_pos += 1

                if photo:
                    c_img = _content_photo_caption(photo, keyword, accent, fp)
                elif keyword:
                    c_img = _content_keyword(fp, keyword, accent, sec_num, sec_label)
                else:
                    c_img = _content_section(fp, accent, sec_num, sec_label)

                c_img.save(content_path, "JPEG", quality=93)
                content_paths.append(content_path)

        except Exception as e:
            logger.error(f"Shorts slide {idx} failed ({vtype}): {e}")
            fallback = Image.new("RGB", (W, H), _DARK_BG)
            fallback.save(plate_path, "JPEG", quality=90)
            plate_paths.append(plate_path)
            _content_section(fp, accent, sec_num, sec_label).save(content_path, "JPEG", quality=90)
            content_paths.append(content_path)

        logger.info(f"Shorts slide {idx} [{vtype}]: plate + {'content' if content_paths[-1] else 'none'}")

    return plate_paths, content_paths
