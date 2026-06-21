import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

_DEFAULT_ACCENT = (245, 166, 35)

# ── Dark-style thumbnail constants ─────────────────────────────────────────────
_DARK_BG       = (6, 10, 30)          # very dark navy
_TITLE_YELLOW  = (255, 220, 30)       # golden yellow for title text
_TITLE_STROKE  = (0, 0, 0)            # black outline on title

# Bokeh blobs: (cx_ratio, cy_ratio, radius, RGBA)
_BOKEH_DARK = [
    (0.78, 0.18, 260, (0,   90, 255,  75)),
    (0.95, 0.62, 210, (0,  180, 220,  70)),
    (0.58, 0.95, 190, (70,   0, 210,  60)),
    (0.08, 0.85, 170, (0,   60, 255,  50)),
    (0.68, 0.00, 160, (0,  150, 255,  65)),
    (0.35, 0.45, 130, (20,   0, 180,  35)),
    (0.50, 0.70, 110, (0,  100, 200,  40)),
]

# ── Shorts thumbnail keeps the original light-background palette ───────────────
_BG_COLOR   = (252, 247, 238)
_DECO_CIRCLES = [
    (0.07,  0.30, 110, (245, 200, 90,  55)),
    (0.92,  0.18,  90, (100, 210, 200, 50)),
    (0.88,  0.52,  70, (200, 175, 240, 45)),
    (0.14,  0.82,  80, (255, 155, 175, 40)),
    (0.55,  0.08,  55, (180, 230, 180, 35)),
]


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


def _extract_hook(text: str) -> tuple[str | None, str]:
    """Split 【hook】 prefix from main title text."""
    if text.startswith("【"):
        end = text.find("】")
        if end != -1:
            return text[1:end], text[end + 1:].strip()
    return None, text


def _draw_deco_circles(W: int, H: int) -> Image.Image:
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for rx, ry, r, color in _DECO_CIRCLES:
        cx, cy = int(W * rx), int(H * ry)
        d.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=color)
    return layer.filter(ImageFilter.GaussianBlur(radius=20))


def _card_shadow(W: int, H: int, x1: int, y1: int, x2: int, y2: int) -> Image.Image:
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle([(x1 + 5, y1 + 8), (x2 + 5, y2 + 8)], radius=18, fill=(0, 0, 0, 35))
    return layer.filter(ImageFilter.GaussianBlur(radius=10))


def _make_dark_bg(W: int, H: int, accent: tuple) -> Image.Image:
    """Dark navy background with bokeh glow blobs."""
    canvas = Image.new("RGBA", (W, H), (*_DARK_BG, 255))
    for rx, ry, r, color in _BOKEH_DARK:
        blob = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d    = ImageDraw.Draw(blob)
        cx, cy = int(W * rx), int(H * ry)
        d.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=color)
        canvas = Image.alpha_composite(canvas, blob.filter(ImageFilter.GaussianBlur(r // 1.4)))
    # Accent-tinted glow near bottom-center
    accent_blob = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ad = ImageDraw.Draw(accent_blob)
    ad.ellipse([(W//2 - 200, H - 100), (W//2 + 200, H + 200)], fill=(*accent, 40))
    canvas = Image.alpha_composite(canvas, accent_blob.filter(ImageFilter.GaussianBlur(80)))
    return canvas


def _paste_chars_dark(canvas: Image.Image, right_path: str, left_path: str,
                      W: int, H: int) -> tuple:
    """Paste both characters on the right half. Returns inner_left_x of leftmost char."""
    inner_x = W

    # Zundamon (right, front, larger)
    if right_path and Path(right_path).exists():
        img = Image.open(right_path).convert("RGBA")
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        target_h = int(H * 0.92)
        ratio    = target_h / img.height
        target_w = int(img.width * ratio)
        img      = img.resize((target_w, target_h), Image.LANCZOS)
        bleed    = int(target_w * 0.08)
        zx       = W - target_w + bleed
        zy       = H - target_h
        canvas.paste(img, (zx, zy), img)
        inner_x  = zx
    else:
        zx = W

    # Tsumugi (left of Zundamon, slightly smaller, behind)
    if left_path and Path(left_path).exists():
        img = Image.open(left_path).convert("RGBA")
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        target_h = int(H * 0.80)
        ratio    = target_h / img.height
        target_w = int(img.width * ratio)
        img      = img.resize((target_w, target_h), Image.LANCZOS)
        overlap  = int(target_w * 0.12)
        tx       = zx - target_w + overlap
        ty       = H - target_h
        canvas.paste(img, (tx, ty), img)
        inner_x  = tx

    return canvas, inner_x


def generate_thumbnail(
    background_image_path: str,
    title_text: str,
    output_path: str,
    config: dict,
) -> str:
    """Generate dark-style 1280×720 YouTube thumbnail with both characters."""
    cfg      = config["thumbnail"]
    W, H     = cfg["width"], cfg["height"]
    fp       = cfg["font_path"]
    char_cfg = config.get("character", {})
    ch       = config.get("active_channel", {})
    accent   = tuple(ch.get("accent_color", list(_DEFAULT_ACCENT)))
    badge_label = ch.get("badge_label", "速報")

    right_path = char_cfg.get("image_path", "")
    left_path  = char_cfg.get("left_image_path", "")

    # ── 1. Background: photo if available, else dark bokeh ───────
    if background_image_path and Path(background_image_path).exists():
        img = Image.open(background_image_path).convert("RGBA")
        ir = img.width / img.height
        cr = W / H
        if ir > cr:
            nw, nh = int(H * ir), H
        else:
            nw, nh = W, int(W / ir)
        img = img.resize((nw, nh), Image.LANCZOS)
        left, top = (nw - W) // 2, (nh - H) // 2
        img = img.crop((left, top, left + W, top + H))
        img = img.filter(ImageFilter.GaussianBlur(radius=6))
        overlay = Image.new("RGBA", (W, H), (8, 10, 20, 185))
        canvas = Image.alpha_composite(img, overlay)
        # Add bokeh glow on top for cinematic feel
        for rx, ry, r, color in _BOKEH_DARK:
            blob = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(blob)
            cx_b, cy_b = int(W * rx), int(H * ry)
            d.ellipse([(cx_b - r, cy_b - r), (cx_b + r, cy_b + r)], fill=color)
            canvas = Image.alpha_composite(canvas, blob.filter(ImageFilter.GaussianBlur(r // 1.4)))
    else:
        canvas = _make_dark_bg(W, H, accent)

    # ── 2. Both characters on right half ─────────────────────────
    canvas, char_inner_x = _paste_chars_dark(canvas, right_path, left_path, W, H)

    draw = ImageDraw.Draw(canvas)

    # ── 3. Top-left badge ─────────────────────────────────────────
    BX, BY = 30, 26
    SQ = 48
    draw.rounded_rectangle([(BX, BY), (BX + SQ, BY + SQ)], radius=7, fill=(*accent, 255))
    font_sq = _font(fp, 28)
    sym = "▶"
    sw, sh = _tw(draw, sym, font_sq), _th(draw, sym, font_sq)
    draw.text((BX + (SQ - sw) // 2, BY + (SQ - sh) // 2), sym, font=font_sq, fill=(255, 255, 255))
    font_badge = _font(fp, 27)
    bw = _tw(draw, badge_label, font_badge)
    bh = _th(draw, badge_label, font_badge)
    cx = BX + SQ + 8
    cw = bw + 24
    draw.rounded_rectangle([(cx, BY), (cx + cw, BY + SQ)], radius=7, fill=(20, 20, 20, 230))
    draw.text((cx + 12, BY + (SQ - bh) // 2), badge_label, font=font_badge, fill=(255, 255, 255))

    # ── 4. Large golden title on left ─────────────────────────────
    hook, main_text = _extract_hook(title_text)

    TX      = 54
    TITLE_Y = BY + SQ + 28
    MAX_TW  = char_inner_x - TX - 30   # don't overlap characters

    # Hook badge
    if hook:
        font_hook = _font(fp, 40)
        hw = _tw(draw, hook, font_hook)
        hh = _th(draw, hook, font_hook)
        hpx, hpy = 16, 7
        draw.rounded_rectangle(
            [(TX, TITLE_Y), (TX + hw + hpx * 2, TITLE_Y + hh + hpy * 2)],
            radius=7, fill=(*accent, 255),
        )
        draw.text((TX + hpx, TITLE_Y + hpy), hook, font=font_hook, fill=(255, 255, 255))
        TITLE_Y += hh + hpy * 2 + 18

    # Auto-size title font
    for size in (114, 96, 80, 66, 54, 44):
        ft    = _font(fp, size)
        lines = _wrap_px(draw, main_text, ft, MAX_TW)
        max_lh = max((_th(draw, ln, ft) for ln in lines), default=size)
        lh     = max_lh + 12
        if len(lines) <= 3 and TITLE_Y + lh * len(lines) <= H - 60:
            font_title = ft
            break
    else:
        font_title = _font(fp, 44)
        lines = _wrap_px(draw, main_text, font_title, MAX_TW)

    lh = max((_th(draw, ln, font_title) for ln in lines), default=52) + 12
    ty = TITLE_Y
    for ln in lines:
        # Bold effect: draw text with black stroke, then golden fill
        draw.text((TX, ty), ln, font=font_title,
                  fill=_TITLE_YELLOW, stroke_width=5, stroke_fill=_TITLE_STROKE)
        ty += lh

    # ── 5. Bottom accent stripe ───────────────────────────────────
    draw.rectangle([(0, H - 6), (W, H)], fill=(*accent, 255))

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output_path, "JPEG", quality=95)
    logger.info(f"Thumbnail generated: {output_path}")
    return output_path


_SHORTS_DECO = [
    (0.15, 0.06, 140, (245, 200, 90,  55)),
    (0.85, 0.04, 110, (100, 210, 200, 50)),
    (0.90, 0.25,  90, (200, 175, 240, 45)),
    (0.10, 0.48, 120, (255, 155, 175, 40)),
    (0.50, 0.02,  70, (180, 230, 180, 35)),
]


def generate_shorts_thumbnail(
    title_text: str,
    output_path: str,
    config: dict,
) -> str:
    """Generate 1080×1920 vertical Shorts thumbnail."""
    cfg      = config["thumbnail"]
    fp       = cfg["font_path"]
    W, H     = 1080, 1920
    ch       = config.get("active_channel", {})
    accent   = tuple(ch.get("accent_color", list(_DEFAULT_ACCENT)))
    badge_label = ch.get("badge_label", "速報")
    char_cfg = config.get("character", {})

    # ── Background ────────────────────────────────────────────────
    deco = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dd   = ImageDraw.Draw(deco)
    for rx, ry, r, color in _SHORTS_DECO:
        cx, cy = int(W * rx), int(H * ry)
        dd.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=color)
    deco   = deco.filter(ImageFilter.GaussianBlur(radius=28))
    canvas = Image.new("RGBA", (W, H), (*_BG_COLOR, 255))
    canvas = Image.alpha_composite(canvas, deco)
    rule   = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    rd     = ImageDraw.Draw(rule)
    for y in range(0, H, 32):
        rd.line([(0, y), (W, y)], fill=(180, 165, 140, 14))
    canvas = Image.alpha_composite(canvas, rule)

    # ── Character at bottom-right ─────────────────────────────────
    char_path = char_cfg.get("image_path", "")
    char_top  = H  # fallback if no character
    if char_path and Path(char_path).exists():
        char_img = Image.open(char_path).convert("RGBA")
        bbox = char_img.getbbox()
        if bbox:
            char_img = char_img.crop(bbox)
        target_h = int(H * 0.62)
        ratio    = target_h / char_img.height
        target_w = int(char_img.width * ratio)
        char_img = char_img.resize((target_w, target_h), Image.LANCZOS)
        bleed    = int(target_w * 0.10)
        char_x   = W - target_w + bleed
        char_y   = H - target_h - 4
        canvas.paste(char_img, (char_x, char_y), char_img)
        char_top = char_y  # record where character head starts

    draw = ImageDraw.Draw(canvas)

    # ── Top-left badge ────────────────────────────────────────────
    BX, BY = 30, 34
    SQ = 52
    draw.rounded_rectangle([(BX, BY), (BX + SQ, BY + SQ)], radius=8, fill=(*accent, 255))
    font_sq = _font(fp, 30)
    sym = "▶"
    sw, sh = _tw(draw, sym, font_sq), _th(draw, sym, font_sq)
    draw.text((BX + (SQ - sw) // 2, BY + (SQ - sh) // 2), sym, font=font_sq, fill=(255, 255, 255))
    font_badge = _font(fp, 28)
    bw = _tw(draw, badge_label, font_badge)
    bh = _th(draw, badge_label, font_badge)
    cx = BX + SQ + 8
    cw = bw + 24
    draw.rounded_rectangle([(cx, BY), (cx + cw, BY + SQ)], radius=8, fill=(35, 35, 35, 240))
    draw.text((cx + 12, BY + (SQ - bh) // 2), badge_label, font=font_badge, fill=(255, 255, 255))

    # ── White card with title ─────────────────────────────────────
    hook, main_text = _extract_hook(title_text)

    CARD_L   = 48
    CARD_R   = W - 48
    CARD_T   = BY + SQ + 30
    CARD_BOT = char_top - 30   # don't overlap character head
    PAD      = 36
    HOOK_H   = 68 if hook else 0
    MAX_TW   = (CARD_R - CARD_L) - PAD * 2

    for size in (100, 84, 70, 58, 48, 40):
        ft    = _font(fp, size)
        lines = _wrap_px(draw, main_text, ft, MAX_TW)
        max_lh = max((_th(draw, ln, ft) for ln in lines), default=size)
        lh     = max_lh + 16
        total  = HOOK_H + (16 if hook else 0) + lh * len(lines)
        if len(lines) <= 5 and total <= (CARD_BOT - CARD_T) - PAD * 2:
            font_title = ft
            break
    else:
        font_title = _font(fp, 40)
        lines = _wrap_px(draw, main_text, font_title, MAX_TW)

    lh_vals  = [_th(draw, ln, font_title) for ln in lines]
    max_lh   = max(lh_vals) if lh_vals else 44
    lh       = max_lh + 16
    content_h = HOOK_H + (16 if hook else 0) + lh * len(lines)
    card_h   = content_h + PAD * 2
    card_y   = CARD_T + max(0, (CARD_BOT - CARD_T - card_h) // 2)
    card_y2  = card_y + card_h

    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle([(CARD_L + 6, card_y + 9), (CARD_R + 6, card_y2 + 9)],
                         radius=22, fill=(0, 0, 0, 38))
    canvas = Image.alpha_composite(canvas, shadow.filter(ImageFilter.GaussianBlur(12)))
    draw   = ImageDraw.Draw(canvas)
    draw.rounded_rectangle([(CARD_L, card_y), (CARD_R, card_y2)],
                           radius=22, fill=(255, 255, 255, 235))
    draw.rounded_rectangle([(CARD_L, card_y), (CARD_L + 7, card_y2)],
                           radius=5, fill=(*accent, 255))

    tx = CARD_L + PAD + 7
    ty = card_y + PAD

    if hook:
        font_hook = _font(fp, 40)
        hw = _tw(draw, hook, font_hook)
        hh = _th(draw, hook, font_hook)
        hpx, hpy = 16, 8
        draw.rounded_rectangle(
            [(tx, ty), (tx + hw + hpx * 2, ty + hh + hpy * 2)],
            radius=8, fill=(*accent, 255),
        )
        draw.text((tx + hpx, ty + hpy), hook, font=font_hook, fill=(255, 255, 255))
        ty += hh + hpy * 2 + 16

    for ln in lines:
        draw.text((tx, ty), ln, font=font_title, fill=(20, 20, 20))
        ty += lh

    # ── Bottom accent stripe ──────────────────────────────────────
    draw.rectangle([(0, H - 8), (W, H)], fill=(*accent, 255))

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output_path, "JPEG", quality=95)
    logger.info(f"Shorts thumbnail generated: {output_path}")
    return output_path
