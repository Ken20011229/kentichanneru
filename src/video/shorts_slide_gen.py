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
_CHAR_RATIO = 0.63
_CHAR_H     = int(H * _CHAR_RATIO)   # 1209px
_CHAR_TOP   = H - _CHAR_H - 4        # 707px

# Content area (above character)
CONTENT_X1 = 0
CONTENT_Y1 = 108
CONTENT_X2 = W
CONTENT_Y2 = _CHAR_TOP - 16          # 691px
CONTENT_W  = W                        # 1080
CONTENT_H  = CONTENT_Y2 - CONTENT_Y1  # 583px

_BADGE_TOP = 35

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

def _draw_badge(draw, fp, accent, sec_num, label, x=30, y=_BADGE_TOP):
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
    canvas = Image.new("RGBA", (W, H), (*_DARK_BG, 255))

    # Subtle accent glow
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd   = ImageDraw.Draw(glow)
    r, g, b = accent
    gd.ellipse([(-100, -100), (W + 100, H // 2)], fill=(r, g, b, 28))
    canvas = Image.alpha_composite(canvas, glow.filter(ImageFilter.GaussianBlur(80)))

    canvas = _paste_char(canvas, char_path, side=side)
    draw   = ImageDraw.Draw(canvas)

    cur_y = CONTENT_Y1
    if keyword:
        for sz in (110, 92, 78, 64, 52):
            fk = _font(fp, sz)
            if _tw(draw, keyword, fk) <= W - 60:
                break
        kw_w = _tw(draw, keyword, fk)
        kw_h = _th(draw, keyword, fk)
        kx   = (W - kw_w) // 2
        draw.text((kx + 4, cur_y + 4), keyword, font=fk, fill=(0, 0, 0, 140))
        draw.text((kx, cur_y), keyword, font=fk, fill=_COLOR_WHITE)
        uy = cur_y + kw_h + 8
        draw.rectangle([(kx, uy), (kx + kw_w, uy + 6)], fill=(*accent, 255))
        cur_y = uy + 28

    # Narration preview (subtitle will show actual text)
    if text:
        font_sub  = _font(fp, 38)
        sub_lines = _wrap_px(draw, text, font_sub, W - 80)[:2]
        for ln in sub_lines:
            draw.text((40, cur_y), ln, font=font_sub, fill=(190, 195, 210, 180))
            cur_y += _th(draw, ln, font_sub) + 12

    draw.rectangle([(0, 0), (W, 5)],     fill=(*accent, 255))
    draw.rectangle([(0, H - 8), (W, H)], fill=(*accent, 255))
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


def _content_keyword(fp, keyword, accent, sec_num, sec_label) -> Image.Image:
    """Keyword graphic for Shorts content area."""
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
        ns, font=font_ghost, fill=(r, g, b, 18),
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

                if seg_img and Path(seg_img).exists():
                    c_img = _content_from_image(seg_img)
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
