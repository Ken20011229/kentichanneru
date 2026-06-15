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
    """INTRO: centered large white card with title, characters at both sides."""
    canvas = _make_bg()
    canvas, card_l, card_r = _paste_characters(canvas, right_path, left_path, char_ratio=0.70)
    draw = ImageDraw.Draw(canvas)
    _draw_section_badge(draw, fp, accent, section_num, badge_label)

    # White card: bounded by character inner edges
    CARD_L = card_l
    CARD_R = card_r
    CARD_T = 100
    CARD_B = _CONTENT_BOTTOM - 10
    PAD    = 48

    canvas = _card_shadow(canvas, CARD_L, CARD_T, CARD_R, CARD_B)
    draw   = ImageDraw.Draw(canvas)
    draw.rounded_rectangle([(CARD_L, CARD_T), (CARD_R, CARD_B)],
                           radius=24, fill=(255, 255, 255, 238))
    draw.rounded_rectangle([(CARD_L, CARD_T), (CARD_L + 8, CARD_B)],
                           radius=6, fill=(*accent, 255))

    inner_w = CARD_R - CARD_L - PAD * 2 - 8
    text_x  = CARD_L + PAD + 8

    for size in (88, 72, 60, 50, 42):
        ft    = _font(fp, size)
        lines = _wrap_px(draw, title, ft, inner_w)
        max_lh = max((_th(draw, ln, ft) for ln in lines), default=size)
        lh     = max_lh + 18
        if lh * len(lines) < (CARD_B - CARD_T) - PAD * 2 - 80 and len(lines) <= 4:
            font_title = ft
            break
    else:
        font_title = _font(fp, 42)
        lines = _wrap_px(draw, title, font_title, inner_w)

    lh    = max((_th(draw, ln, font_title) for ln in lines), default=42) + 18
    total = lh * len(lines)
    ty    = CARD_T + PAD + max(0, ((CARD_B - CARD_T) - PAD * 2 - total) // 2)

    for ln in lines:
        draw.text((text_x, ty), ln, font=font_title, fill=_COLOR_TITLE)
        ty += lh

    if text and ty < CARD_B - 60:
        font_sub  = _font(fp, 30)
        sub_lines = _wrap_px(draw, text, font_sub, inner_w)
        ty += 18
        for ln in sub_lines[:3]:
            if ty + _th(draw, ln, font_sub) + 10 > CARD_B - PAD:
                break
            draw.text((text_x, ty), ln, font=font_sub, fill=_COLOR_SUPPORT)
            ty += _th(draw, ln, font_sub) + 10

    _draw_accent_stripe(draw, accent)
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
    """KEYWORD: hero keyword box centered top, narration card below, characters."""
    canvas = _make_bg()
    canvas, card_l, card_r = _paste_characters(canvas, right_path, left_path, char_ratio=0.75)
    draw = ImageDraw.Draw(canvas)
    _draw_section_badge(draw, fp, accent, section_num, badge_label)

    # Hero keyword box — centered within card bounds, with auto font sizing
    kw_text  = keyword if keyword else "POINT"
    max_box_w = card_r - card_l - 40
    font_kw  = _font(fp, 60)
    for size in (110, 90, 74, 60):
        fk   = _font(fp, size)
        kw_w = _tw(draw, kw_text, fk)
        if kw_w < max_box_w:
            font_kw = fk
            break

    kw_w = _tw(draw, kw_text, font_kw)
    kw_h = _th(draw, kw_text, font_kw)
    kpx, kpy = 60, 24
    box_w  = kw_w + kpx * 2
    box_h  = kw_h + kpy * 2
    box_x  = card_l + (card_r - card_l - box_w) // 2
    box_y  = 100

    canvas = _card_shadow(canvas, box_x, box_y, box_x + box_w, box_y + box_h, radius=20)
    draw   = ImageDraw.Draw(canvas)
    draw.rounded_rectangle([(box_x, box_y), (box_x + box_w, box_y + box_h)],
                           radius=20, fill=(*accent, 255))
    draw.text((box_x + kpx, box_y + kpy), kw_text, font=font_kw, fill=_COLOR_WHITE)

    # Supporting text card below
    CARD_T = box_y + box_h + 36

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
    right_path  = char_cfg.get("image_path", "")
    left_path   = char_cfg.get("left_image_path", "")

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
