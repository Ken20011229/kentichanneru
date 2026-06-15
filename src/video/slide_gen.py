"""slide_gen.py — Generate one 1920×1080 PIL slide image per script segment.

Visual types:
    intro   — Full-width white card with large title, characters at both sides
    point   — Section number badge + keyword chip + narration card + characters
    keyword — Hero keyword box centered top, supporting text card, characters
    detail  — Text card left + accent arrow → keyword, characters
"""

import logging
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

W, H = 1920, 1080

BG_COLOR = (252, 247, 238)

DECO_CIRCLES = [
    (0.07, 0.30, 140, (245, 200, 90,  55)),
    (0.92, 0.18, 110, (100, 210, 200, 50)),
    (0.88, 0.52,  90, (200, 175, 240, 45)),
    (0.14, 0.82, 100, (255, 155, 175, 40)),
    (0.55, 0.08,  70, (180, 230, 180, 35)),
]

_COLOR_TITLE   = (25,  25,  35)
_COLOR_DARK    = (40,  40,  50)
_COLOR_SUPPORT = (80,  80,  90)
_COLOR_WHITE   = (255, 255, 255)

_DEFAULT_ACCENT = (245, 166, 35)

# Lower boundary for content cards — content can overlap character bodies (not heads)
_CONTENT_BOTTOM = int(H * 0.73)   # 788px — characters' heads are at ~35% (378px)

# Section label names (mapped by section index 0-4)
_SECTION_LABELS = ["はじめに", "概要", "解説", "詳細", "まとめ"]


# ── Font & measurement helpers ─────────────────────────────────────────────────

def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _tw(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0]


def _th(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[3] - bb[1]


def _wrap_px(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
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


# ── Background helpers ─────────────────────────────────────────────────────────

def _draw_deco_circles() -> Image.Image:
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for rx, ry, r, color in DECO_CIRCLES:
        cx, cy = int(W * rx), int(H * ry)
        d.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=color)
    return layer.filter(ImageFilter.GaussianBlur(radius=28))


def _make_bg() -> Image.Image:
    canvas = Image.new("RGBA", (W, H), (*BG_COLOR, 255))
    canvas = Image.alpha_composite(canvas, _draw_deco_circles())
    rule = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rule)
    for y in range(0, H, 32):
        rd.line([(0, y), (W, y)], fill=(180, 165, 140, 14))
    return Image.alpha_composite(canvas, rule)


def _make_dark_bg(accent: tuple) -> Image.Image:
    """Dark intro background with subtle accent glow — matches title-card style."""
    r, g, b = accent
    canvas = Image.new("RGBA", (W, H), (10, 12, 22, 255))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for cx, cy, radius, alpha in [
        (int(W * 0.72), int(H * 0.30), 340, 20),
        (int(W * 0.18), int(H * 0.70), 240, 12),
        (int(W * 0.50), int(H * 0.85), 200, 8),
    ]:
        gd.ellipse([(cx - radius, cy - radius), (cx + radius, cy + radius)],
                   fill=(r, g, b, alpha))
    return Image.alpha_composite(canvas, glow.filter(ImageFilter.GaussianBlur(radius=90)))


def _card_shadow(canvas: Image.Image, x1: int, y1: int, x2: int, y2: int,
                 radius: int = 24) -> Image.Image:
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle([(x1 + 7, y1 + 10), (x2 + 7, y2 + 10)],
                        radius=radius, fill=(0, 0, 0, 40))
    return Image.alpha_composite(canvas, layer.filter(ImageFilter.GaussianBlur(radius=14)))


# ── Character helpers ──────────────────────────────────────────────────────────

def _paste_char(canvas: Image.Image, path: str, h_ratio: float,
                side: str = "right", flip: bool = False) -> tuple:
    """Paste one character at the given side (left/right), bottom-aligned.
    Auto-crops transparent padding so visual height is consistent across images.
    Returns (canvas, inner_edge_x) where inner_edge_x is the character's inner boundary."""
    fallback = W if side == "right" else 0
    if not path or not Path(path).exists():
        return canvas, fallback
    try:
        img = Image.open(path).convert("RGBA")
        if flip:
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        # Remove transparent padding so both characters scale to the same visual height
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        target_h = int(H * h_ratio)
        ratio    = target_h / img.height
        target_w = int(img.width * ratio)
        img      = img.resize((target_w, target_h), Image.LANCZOS)
        bleed    = int(target_w * 0.06)
        if side == "right":
            char_x     = W - target_w + bleed
            inner_edge = char_x          # left boundary of right character
        else:
            char_x     = -bleed
            inner_edge = target_w - bleed  # right boundary of left character
        char_y = H - target_h - 6
        canvas.paste(img, (char_x, char_y), img)
        return canvas, inner_edge
    except Exception as e:
        logger.warning(f"Character paste failed ({side}): {e}")
        return canvas, fallback


def _char_variant(base_path: str, suffix: str) -> str:
    """Return base_path with suffix before extension if it exists, else base_path."""
    if not base_path:
        return base_path
    p = Path(base_path)
    alt = p.parent / f"{p.stem}{suffix}{p.suffix}"
    return str(alt) if alt.exists() else base_path


def _resolve_char_paths(char_cfg: dict, visual_type: str) -> tuple[str, str]:
    """Select character image variants by visual_type using filename convention."""
    right = char_cfg.get("image_path", "")
    left  = char_cfg.get("left_image_path", "")
    if visual_type == "keyword":
        right = _char_variant(right, "1")
        left  = _char_variant(left,  "1")
    elif visual_type == "point":
        right = _char_variant(right, "2")
    return right, left


def _paste_characters(canvas: Image.Image,
                      right_path: str, left_path: str,
                      char_ratio: float = 0.72) -> tuple:
    """Paste left (flipped) then right character at equal size.
    Returns (canvas, card_l, card_r) — safe horizontal bounds for content cards."""
    GAP = 40  # minimum gap between character inner edge and card
    canvas, left_inner  = _paste_char(canvas, left_path,  char_ratio, side="left",  flip=True)
    canvas, right_inner = _paste_char(canvas, right_path, char_ratio, side="right")
    card_l = max(left_inner  + GAP, 80)
    card_r = min(right_inner - GAP, W - 80)
    return canvas, card_l, card_r


# ── Component drawing helpers ──────────────────────────────────────────────────

def _draw_section_badge(draw: ImageDraw.ImageDraw, fp: str, accent: tuple,
                        section_num: int, badge_label: str,
                        x: int = 36, y: int = 30):
    """Draw [N][badge_label] chip at top-left. N is the section number."""
    SQ         = 54
    font_sq    = _font(fp, 32)
    font_badge = _font(fp, 30)

    # Accent square with section number
    draw.rounded_rectangle([(x, y), (x + SQ, y + SQ)], radius=8, fill=(*accent, 255))
    num_str = str(section_num)
    nw = _tw(draw, num_str, font_sq)
    nh = _th(draw, num_str, font_sq)
    draw.text((x + (SQ - nw) // 2, y + (SQ - nh) // 2),
              num_str, font=font_sq, fill=_COLOR_WHITE)

    # Dark chip with badge_label
    bw = _tw(draw, badge_label, font_badge)
    bh = _th(draw, badge_label, font_badge)
    cx = x + SQ + 10
    cw = bw + 28
    draw.rounded_rectangle([(cx, y), (cx + cw, y + SQ)], radius=8, fill=(35, 35, 35, 235))
    draw.text((cx + 14, y + (SQ - bh) // 2), badge_label, font=font_badge, fill=_COLOR_WHITE)


def _draw_accent_stripe(draw: ImageDraw.ImageDraw, accent: tuple):
    draw.rectangle([(0, H - 6), (W, H)], fill=(*accent, 255))


def _text_card(canvas: Image.Image, draw: ImageDraw.ImageDraw,
               fp: str, text: str, accent: tuple,
               x1: int, y1: int, x2: int,
               font_sizes: tuple = (52, 44, 38, 32, 28),
               pad: int = 40,
               max_bottom: int = _CONTENT_BOTTOM):
    """White rounded card with narration text. Returns (card_bottom_y, updated_canvas)."""
    max_w = (x2 - x1) - pad * 2
    avail_h = max_bottom - y1 - pad * 2

    chosen_font, lines = None, []
    for size in font_sizes:
        ft  = _font(fp, size)
        ls  = _wrap_px(draw, text, ft, max_w)
        lh  = max((_th(draw, ln, ft) for ln in ls), default=size) + 16
        if lh * len(ls) <= avail_h:
            chosen_font, lines = ft, ls
            break
    if chosen_font is None:
        chosen_font = _font(fp, font_sizes[-1])
        lines = _wrap_px(draw, text, chosen_font, max_w)

    lh     = max((_th(draw, ln, chosen_font) for ln in lines), default=28) + 16
    card_h = lh * len(lines) + pad * 2
    y2     = min(y1 + card_h, max_bottom)

    canvas = _card_shadow(canvas, x1, y1, x2, y2)
    draw   = ImageDraw.Draw(canvas)
    draw.rounded_rectangle([(x1, y1), (x2, y2)], radius=22, fill=(255, 255, 255, 235))
    draw.rounded_rectangle([(x1, y1), (x1 + 7, y2)], radius=5, fill=(*accent, 255))

    ty = y1 + pad
    for ln in lines:
        if ty + lh > y2 - pad // 2:
            break
        draw.text((x1 + pad + 7, ty), ln, font=chosen_font, fill=_COLOR_DARK)
        ty += lh

    return y2, canvas


# ── Slide generators ──────────────────────────────────────────────────────────

def _slide_intro(fp: str, title: str, text: str, accent: tuple,
                 section_num: int, badge_label: str,
                 right_path: str, left_path: str) -> Image.Image:
    """INTRO: dark background title card — channel label top, large title, subtle subtext."""
    canvas = _make_dark_bg(accent)
    canvas, card_l, card_r = _paste_characters(canvas, right_path, left_path, char_ratio=0.68)
    draw   = ImageDraw.Draw(canvas)

    # Badge label (channel name) centered at top in accent color
    font_badge = _font(fp, 30)
    bw = _tw(draw, badge_label, font_badge)
    draw.text(((W - bw) // 2, 52), badge_label, font=font_badge, fill=(*accent, 220))

    # Short accent line below badge
    line_y = 98
    draw.rectangle([(W // 2 - 160, line_y), (W // 2 + 160, line_y + 3)],
                   fill=(*accent, 140))

    # Main title: white, large, left-aligned within character bounds
    inner_w = card_r - card_l - 80
    text_x  = card_l + 44

    for size in (92, 76, 64, 54, 44):
        ft     = _font(fp, size)
        lines  = _wrap_px(draw, title, ft, inner_w)
        max_lh = max((_th(draw, ln, ft) for ln in lines), default=size)
        lh     = max_lh + 22
        if lh * len(lines) < int(H * 0.50) and len(lines) <= 4:
            font_title = ft
            break
    else:
        font_title = _font(fp, 44)
        lines = _wrap_px(draw, title, font_title, inner_w)

    lh    = max((_th(draw, ln, font_title) for ln in lines), default=44) + 22
    total = lh * len(lines)
    ty    = 130 + max(0, (int(H * 0.48) - total) // 2)

    for ln in lines:
        draw.text((text_x, ty), ln, font=font_title, fill=(255, 255, 255, 255))
        ty += lh

    # Narration preview in muted color below title
    if text and ty + 48 < _CONTENT_BOTTOM:
        font_sub  = _font(fp, 30)
        sub_lines = _wrap_px(draw, text, font_sub, inner_w)
        ty += 26
        for ln in sub_lines[:2]:
            if ty + _th(draw, ln, font_sub) > _CONTENT_BOTTOM - 20:
                break
            draw.text((text_x, ty), ln, font=font_sub, fill=(190, 195, 210, 180))
            ty += _th(draw, ln, font_sub) + 12

    # Accent stripes top and bottom
    draw.rectangle([(0, 0), (W, 5)], fill=(*accent, 255))
    draw.rectangle([(0, H - 5), (W, H)], fill=(*accent, 255))
    return canvas.convert("RGB")


def _slide_point(fp: str, text: str, keyword: str, accent: tuple,
                 section_num: int, badge_label: str, seg_idx: int,
                 right_path: str, left_path: str) -> Image.Image:
    """POINT: section badge + keyword chip + narration card + characters."""
    canvas = _make_bg()
    canvas, card_l, card_r = _paste_characters(canvas, right_path, left_path, char_ratio=0.78)
    draw = ImageDraw.Draw(canvas)
    _draw_section_badge(draw, fp, accent, section_num, badge_label)

    # Keyword chip (accent-colored pill, top-left area below badge — safe above characters)
    chip_y = 108
    chip_x = card_l
    kw_h = 0
    kpy  = 10
    if keyword:
        font_kw = _font(fp, 38)
        kw_w = _tw(draw, keyword, font_kw)
        kw_h = _th(draw, keyword, font_kw)
        kpx = 20
        draw.rounded_rectangle(
            [(chip_x, chip_y), (chip_x + kw_w + kpx * 2, chip_y + kw_h + kpy * 2)],
            radius=10, fill=(*accent, 240),
        )
        draw.text((chip_x + kpx, chip_y + kpy), keyword, font=font_kw, fill=_COLOR_WHITE)

    # Narration text card
    CARD_T = 108 + (kw_h + kpy * 2 + 18 if keyword else 0)
    CARD_T = max(CARD_T, 180)

    _, canvas = _text_card(
        canvas, draw, fp, text, accent,
        x1=card_l, y1=CARD_T, x2=card_r,
        font_sizes=(54, 46, 40, 34, 28),
        pad=44,
    )
    draw = ImageDraw.Draw(canvas)
    _draw_accent_stripe(draw, accent)
    return canvas.convert("RGB")


def _slide_keyword(fp: str, text: str, keyword: str, accent: tuple,
                   section_num: int, badge_label: str,
                   right_path: str, left_path: str) -> Image.Image:
    """KEYWORD: large keyword text directly on background (no box), narration card below."""
    canvas = _make_bg()
    canvas, card_l, card_r = _paste_characters(canvas, right_path, left_path, char_ratio=0.75)
    draw = ImageDraw.Draw(canvas)
    _draw_section_badge(draw, fp, accent, section_num, badge_label)

    # Keyword as large hero text directly on the background
    kw_text = keyword if keyword else "POINT"
    max_w   = card_r - card_l - 60
    font_kw = _font(fp, 60)
    for size in (120, 100, 84, 68, 54):
        fk   = _font(fp, size)
        kw_w = _tw(draw, kw_text, fk)
        if kw_w < max_w:
            font_kw = fk
            break

    kw_w = _tw(draw, kw_text, font_kw)
    kw_h = _th(draw, kw_text, font_kw)
    kw_x = card_l + (card_r - card_l - kw_w) // 2
    kw_y = 108

    # Subtle accent highlight band behind the keyword
    hi = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hi)
    hd.rounded_rectangle(
        [(kw_x - 28, kw_y - 14), (kw_x + kw_w + 28, kw_y + kw_h + 14)],
        radius=18, fill=(*accent, 28),
    )
    canvas = Image.alpha_composite(canvas, hi)
    draw   = ImageDraw.Draw(canvas)

    # Keyword text in dark color (cream bg so dark text reads better than white)
    draw.text((kw_x, kw_y), kw_text, font=font_kw, fill=_COLOR_TITLE)

    # Accent underline
    ul_y = kw_y + kw_h + 10
    draw.rectangle([(kw_x, ul_y), (kw_x + kw_w, ul_y + 5)], fill=(*accent, 255))

    # Narration text card below
    CARD_T = ul_y + 32
    _, canvas = _text_card(
        canvas, draw, fp, text, accent,
        x1=card_l, y1=CARD_T, x2=card_r,
        font_sizes=(46, 40, 34, 28, 24),
        pad=38,
    )
    draw = ImageDraw.Draw(canvas)
    _draw_accent_stripe(draw, accent)
    return canvas.convert("RGB")


def _slide_detail(fp: str, text: str, keyword: str, accent: tuple,
                  section_num: int, badge_label: str,
                  right_path: str, left_path: str) -> Image.Image:
    """DETAIL: text card center + optional arrow→keyword + characters."""
    canvas = _make_bg()
    canvas, card_l, card_r = _paste_characters(canvas, right_path, left_path, char_ratio=0.76)
    draw = ImageDraw.Draw(canvas)
    _draw_section_badge(draw, fp, accent, section_num, badge_label)

    CARD_T = 110

    card_bot, canvas = _text_card(
        canvas, draw, fp, text, accent,
        x1=card_l, y1=CARD_T, x2=card_r,
        font_sizes=(52, 46, 40, 34, 28),
        pad=42,
    )
    draw = ImageDraw.Draw(canvas)

    # Accent arrow → keyword below card
    if keyword and card_bot + 80 < _CONTENT_BOTTOM:
        font_arrow = _font(fp, 52)
        font_kw    = _font(fp, 46)
        arrow_x    = card_l + 60
        arrow_y    = card_bot + 22
        draw.text((arrow_x, arrow_y), "→", font=font_arrow, fill=(*accent, 255))
        kw_x = arrow_x + _tw(draw, "→", font_arrow) + 18
        draw.text((kw_x, arrow_y + 4), keyword, font=font_kw, fill=_COLOR_TITLE)

    _draw_accent_stripe(draw, accent)
    return canvas.convert("RGB")


# ── Public entry point ─────────────────────────────────────────────────────────

def generate_slides(
    segments: list[dict],
    title: str,
    config: dict,
    output_dir: str,
) -> list[str]:
    """Generate one 1920×1080 slide image per segment. Returns list of paths."""
    os.makedirs(output_dir, exist_ok=True)

    fp          = config.get("thumbnail", {}).get("font_path", "")
    ch          = config.get("active_channel", {})
    accent      = tuple(ch.get("accent_color", list(_DEFAULT_ACCENT)))
    badge_label = ch.get("badge_label", "テック速報")
    char_cfg    = config.get("character", {})

    total = max(len(segments), 1)

    def _section(idx: int) -> tuple[int, str]:
        """Map segment index to (section_number, section_label)."""
        s = min(int(idx * len(_SECTION_LABELS) / total), len(_SECTION_LABELS) - 1)
        return s + 1, _SECTION_LABELS[s]

    paths = []
    for seg in segments:
        idx         = seg.get("segment_index", 0)
        text        = seg.get("text", "")
        keyword     = seg.get("keyword", "")
        visual_type = seg.get("visual_type", "detail")

        if idx == 0:
            visual_type = "intro"

        right_path, left_path = _resolve_char_paths(char_cfg, visual_type)

        sec_num, sec_label = _section(idx)
        # For intro, use the channel badge_label; other slides use section label
        slide_label = badge_label if visual_type == "intro" else sec_label

        logger.debug(f"Slide {idx}: [{sec_num}]{slide_label} type={visual_type!r} kw={keyword!r}")

        try:
            if visual_type == "intro":
                img = _slide_intro(fp, title, text, accent,
                                   sec_num, slide_label, right_path, left_path)
            elif visual_type == "keyword":
                img = _slide_keyword(fp, text, keyword, accent,
                                     sec_num, slide_label, right_path, left_path)
            elif visual_type == "point":
                img = _slide_point(fp, text, keyword, accent,
                                   sec_num, slide_label, idx, right_path, left_path)
            else:
                img = _slide_detail(fp, text, keyword, accent,
                                    sec_num, slide_label, right_path, left_path)
        except Exception as e:
            logger.error(f"Slide {idx} generation failed ({visual_type}): {e}")
            img = _make_bg().convert("RGB")

        out_path = os.path.join(output_dir, f"slide_{idx:03d}.jpg")
        img.save(out_path, "JPEG", quality=93)
        paths.append(out_path)
        logger.info(f"Slide {idx} saved: {out_path}")

    return paths
