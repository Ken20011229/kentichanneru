"""shorts_slide_gen.py — Generate 1080×1920 vertical slides for YouTube Shorts."""

import logging
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

W, H = 1080, 1920
BG_COLOR = (252, 247, 238)

DECO_CIRCLES = [
    (0.12, 0.08, 130, (245, 200, 90,  55)),
    (0.88, 0.06, 110, (100, 210, 200, 50)),
    (0.92, 0.30,  90, (200, 175, 240, 45)),
    (0.08, 0.55, 120, (255, 155, 175, 40)),
    (0.50, 0.03,  70, (180, 230, 180, 35)),
]

_COLOR_DARK    = (40,  40,  50)
_COLOR_WHITE   = (255, 255, 255)
_DEFAULT_ACCENT = (245, 166, 35)
_SECTION_LABELS = ["はじめに", "概要", "解説", "詳細", "まとめ"]

_CHAR_RATIO    = 0.63
_CHAR_H        = int(H * _CHAR_RATIO)   # 1209px
_CHAR_TOP      = H - _CHAR_H - 4        # 707px — character head Y
_CONTENT_BOTTOM = _CHAR_TOP - 20        # 687px — max Y for text cards


def _font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _tw(draw, text, font):
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0]


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


def _make_bg():
    deco = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(deco)
    for rx, ry, r, color in DECO_CIRCLES:
        cx, cy = int(W * rx), int(H * ry)
        d.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=color)
    deco = deco.filter(ImageFilter.GaussianBlur(radius=28))
    canvas = Image.new("RGBA", (W, H), (*BG_COLOR, 255))
    canvas = Image.alpha_composite(canvas, deco)
    rule = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rule)
    for y in range(0, H, 32):
        rd.line([(0, y), (W, y)], fill=(180, 165, 140, 14))
    return Image.alpha_composite(canvas, rule)


def _paste_char(canvas, path, side="right"):
    """Paste single character at bottom of vertical slide with auto-crop."""
    if not path or not Path(path).exists():
        return canvas
    try:
        img = Image.open(path).convert("RGBA")
        if side == "left":
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        ratio    = _CHAR_H / img.height
        target_w = int(img.width * ratio)
        img      = img.resize((target_w, _CHAR_H), Image.LANCZOS)
        bleed    = int(target_w * 0.08)
        if side == "right":
            char_x = W - target_w + bleed
        elif side == "left":
            char_x = -bleed
        else:
            char_x = (W - target_w) // 2
        char_y = H - _CHAR_H - 4
        canvas.paste(img, (char_x, char_y), img)
    except Exception as e:
        logger.warning(f"Vertical char paste failed ({side}): {e}")
    return canvas


def _shadow(canvas, x1, y1, x2, y2, radius=18):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle([(x1 + 6, y1 + 9), (x2 + 6, y2 + 9)], radius=radius, fill=(0, 0, 0, 38))
    return Image.alpha_composite(canvas, layer.filter(ImageFilter.GaussianBlur(12)))


def _draw_badge(draw, fp, accent, sec_num, label, x=30, y=35):
    SQ       = 52
    font_sq  = _font(fp, 30)
    font_lbl = _font(fp, 28)
    draw.rounded_rectangle([(x, y), (x + SQ, y + SQ)], radius=8, fill=(*accent, 255))
    n  = str(sec_num)
    nw = _tw(draw, n, font_sq)
    nh = _th(draw, n, font_sq)
    draw.text((x + (SQ - nw) // 2, y + (SQ - nh) // 2), n, font=font_sq, fill=_COLOR_WHITE)
    bw  = _tw(draw, label, font_lbl)
    bh  = _th(draw, label, font_lbl)
    cx  = x + SQ + 10
    cw  = bw + 24
    draw.rounded_rectangle([(cx, y), (cx + cw, y + SQ)], radius=8, fill=(35, 35, 35, 235))
    draw.text((cx + 12, y + (SQ - bh) // 2), label, font=font_lbl, fill=_COLOR_WHITE)


def _draw_accent_stripe(draw, accent):
    draw.rectangle([(0, H - 8), (W, H)], fill=(*accent, 255))


def generate_shorts_slides(
    segments: list[dict],
    title: str,
    config: dict,
    output_dir: str,
) -> list[str]:
    """Generate one 1080×1920 slide per Shorts segment. Returns list of paths."""
    os.makedirs(output_dir, exist_ok=True)
    fp          = config.get("thumbnail", {}).get("font_path", "")
    ch          = config.get("active_channel", {})
    accent      = tuple(ch.get("accent_color", list(_DEFAULT_ACCENT)))
    badge_label = ch.get("badge_label", "テック速報")
    char_cfg    = config.get("character", {})
    right_path  = char_cfg.get("image_path", "")
    left_path   = char_cfg.get("left_image_path", "")

    total  = max(len(segments), 1)
    PAD_X  = 55   # horizontal padding for text card
    PAD_Y  = 42   # inner vertical padding

    paths = []
    for idx, seg in enumerate(segments):
        text    = seg.get("text", "")
        keyword = seg.get("keyword", "")
        speaker = seg.get("speaker_side", "right" if idx % 2 == 0 else "left")

        s = min(int(idx * len(_SECTION_LABELS) / total), len(_SECTION_LABELS) - 1)
        sec_num    = s + 1
        sec_label  = _SECTION_LABELS[s]
        slide_label = badge_label if idx == 0 else sec_label

        canvas = _make_bg()

        # Character at bottom (single character, matching speaker)
        char_path = right_path if speaker == "right" else left_path
        canvas = _paste_char(canvas, char_path, side=speaker)

        draw = ImageDraw.Draw(canvas)
        _draw_badge(draw, fp, accent, sec_num, slide_label)

        cur_y = 110

        # Keyword hero box
        if keyword:
            max_kw_w = W - PAD_X * 2 - 20
            fk = _font(fp, 70)
            for size in (100, 82, 70, 58, 48):
                fk = _font(fp, size)
                if _tw(draw, keyword, fk) <= max_kw_w:
                    break
            kw_w = _tw(draw, keyword, fk)
            kw_h = _th(draw, keyword, fk)
            kpx, kpy = 40, 18
            bx  = (W - kw_w - kpx * 2) // 2
            by  = cur_y
            bw  = kw_w + kpx * 2
            bh  = kw_h + kpy * 2
            canvas = _shadow(canvas, bx, by, bx + bw, by + bh, radius=16)
            draw   = ImageDraw.Draw(canvas)
            draw.rounded_rectangle([(bx, by), (bx + bw, by + bh)], radius=16, fill=(*accent, 255))
            draw.text((bx + kpx, by + kpy), keyword, font=fk, fill=_COLOR_WHITE)
            cur_y = by + bh + 28

        # Text card
        card_x1 = PAD_X
        card_x2 = W - PAD_X
        card_y1 = cur_y
        max_w   = card_x2 - card_x1 - PAD_Y * 2
        avail_h = _CONTENT_BOTTOM - card_y1 - PAD_Y * 2

        chosen_font, lines = None, []
        for size in (54, 46, 40, 34, 28, 24):
            ft = _font(fp, size)
            ls = _wrap_px(draw, text, ft, max_w)
            lh = max((_th(draw, ln, ft) for ln in ls), default=size) + 14
            if lh * len(ls) <= avail_h:
                chosen_font, lines = ft, ls
                break
        if chosen_font is None:
            chosen_font = _font(fp, 24)
            lines = _wrap_px(draw, text, chosen_font, max_w)

        lh     = max((_th(draw, ln, chosen_font) for ln in lines), default=24) + 14
        card_h = lh * len(lines) + PAD_Y * 2
        card_y2 = min(card_y1 + card_h, _CONTENT_BOTTOM)

        canvas = _shadow(canvas, card_x1, card_y1, card_x2, card_y2)
        draw   = ImageDraw.Draw(canvas)
        draw.rounded_rectangle([(card_x1, card_y1), (card_x2, card_y2)],
                                radius=22, fill=(255, 255, 255, 235))
        draw.rounded_rectangle([(card_x1, card_y1), (card_x1 + 7, card_y2)],
                                radius=5, fill=(*accent, 255))

        ty = card_y1 + PAD_Y
        for ln in lines:
            if ty + lh > card_y2 - PAD_Y // 2:
                break
            draw.text((card_x1 + PAD_Y + 7, ty), ln, font=chosen_font, fill=_COLOR_DARK)
            ty += lh

        _draw_accent_stripe(draw, accent)

        out_path = os.path.join(output_dir, f"shorts_slide_{idx:03d}.jpg")
        canvas.convert("RGB").save(out_path, "JPEG", quality=93)
        paths.append(out_path)
        logger.info(f"Shorts slide {idx} saved: {out_path}")

    return paths
