"""slide_gen.py — Two-layer 1920×1080 slide system.

generate_slides() returns (plate_paths, content_paths):
  plate_paths   — static chrome frames (background + characters + badge + accent stripe)
  content_paths — animated content images (image/keyword graphic, or None for intro slides)

The FFmpeg composer overlays content_paths onto plate_paths with a zoompan animation,
while plate frames (including characters) remain completely static.
"""

import logging
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from src.images.safe_open import safe_open_rgba

logger = logging.getLogger(__name__)

# ── Canvas dimensions ──────────────────────────────────────────────────────────
W, H = 1920, 1080

# Content overlay area (between characters, passed to composer.py)
CONTENT_X1, CONTENT_Y1 = 220, 108
CONTENT_X2, CONTENT_Y2 = 1700, 782
CONTENT_W  = CONTENT_X2 - CONTENT_X1   # 1480
CONTENT_H  = CONTENT_Y2 - CONTENT_Y1   # 674

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

def _paste_char(canvas, path, h_ratio, side="right", flip=False):
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
        bleed    = int(target_w * 0.06)
        if side == "right":
            char_x     = W - target_w + bleed
            inner_edge = char_x
        else:
            char_x     = -bleed
            inner_edge = target_w - bleed
        canvas.paste(img, (char_x, H - target_h - 6), img)
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


def _paste_characters(canvas, right_path, left_path, char_ratio=0.72):
    """Paste both characters at fixed positions. Returns canvas (characters don't move)."""
    canvas, _ = _paste_char(canvas, left_path,  char_ratio, side="left",  flip=True)
    canvas, _ = _paste_char(canvas, right_path, char_ratio, side="right")
    return canvas


# ── Chrome element drawers ─────────────────────────────────────────────────────

def _draw_section_badge(draw, fp, accent, section_num, badge_label, x=36, y=30):
    SQ = 54
    font_sq    = _font(fp, 32)
    font_badge = _font(fp, 30)
    draw.rounded_rectangle([(x, y), (x + SQ, y + SQ)], radius=8, fill=(*accent, 255))
    num_str = str(section_num)
    nw = _tw(draw, num_str, font_sq)
    nh = _th(draw, num_str, font_sq)
    draw.text((x + (SQ - nw) // 2, y + (SQ - nh) // 2), num_str, font=font_sq, fill=_COLOR_WHITE)
    bw = _tw(draw, badge_label, font_badge)
    bh = _th(draw, badge_label, font_badge)
    cx = x + SQ + 10
    cw = bw + 28
    draw.rounded_rectangle([(cx, y), (cx + cw, y + SQ)], radius=8, fill=(35, 35, 35, 235))
    draw.text((cx + 14, y + (SQ - bh) // 2), badge_label, font=font_badge, fill=_COLOR_WHITE)


def _draw_accent_stripe(draw, accent):
    draw.rectangle([(0, H - 6), (W, H)], fill=(*accent, 255))


def _draw_content_border(draw, accent):
    """Subtle accent border marking the content overlay area on the plate."""
    draw.rounded_rectangle(
        [(CONTENT_X1 - 3, CONTENT_Y1 - 3), (CONTENT_X2 + 3, CONTENT_Y2 + 3)],
        radius=18, outline=(*accent, 55), width=2,
    )


# ── Frame plate builders ───────────────────────────────────────────────────────

def generate_character_overlay(config: dict, output_path: str) -> str:
    """Generate a static RGBA character overlay (both characters, transparent background)."""
    char_cfg   = config.get("character", {})
    right_path = char_cfg.get("image_path", "")
    left_path  = char_cfg.get("left_image_path", "")

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    canvas, _ = _paste_char(canvas, left_path,  0.72, side="left",  flip=True)
    canvas, _ = _paste_char(canvas, right_path, 0.72, side="right")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, "PNG")
    logger.info(f"Character overlay generated: {output_path}")
    return output_path


def _plate_intro(fp, title, accent, badge_label, bg_image_path=None) -> Image.Image:
    """INTRO plate: dark slide with title baked in. Characters are separate overlay."""
    canvas = _make_dark_bg(accent, bg_image_path)
    draw   = ImageDraw.Draw(canvas)

    font_badge = _font(fp, 30)
    bw = _tw(draw, badge_label, font_badge)
    draw.text(((W - bw) // 2, 52), badge_label, font=font_badge, fill=(*accent, 220))
    draw.rectangle([(W // 2 - 160, 98), (W // 2 + 160, 101)], fill=(*accent, 140))

    inner_w = CONTENT_X2 - CONTENT_X1 - 80
    text_x  = CONTENT_X1 + 44
    MAX_TITLE_LINES = 2

    font_title, lines = None, None
    for size in (92, 76, 64, 54, 44, 38, 32):
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


def _content_keyword(fp, keyword, accent, section_num, sec_label) -> Image.Image:
    """Keyword graphic: dark background with large accent keyword + section info."""
    r, g, b = accent
    img = Image.new("RGBA", (CONTENT_W, CONTENT_H), (18, 20, 32, 255))

    # Accent glow blob
    glow = Image.new("RGBA", (CONTENT_W, CONTENT_H), (0, 0, 0, 0))
    gd   = ImageDraw.Draw(glow)
    gd.ellipse([(-60, -60), (CONTENT_W // 2 + 100, CONTENT_H + 60)], fill=(r, g, b, 22))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(90)))

    draw = ImageDraw.Draw(img)

    # Section badge top-left
    BADGE_SZ = 68
    BX, BY = 36, 36
    draw.rounded_rectangle([(BX, BY), (BX + BADGE_SZ, BY + BADGE_SZ)],
                           radius=10, fill=(*accent, 255))
    font_num = _font(fp, 38)
    ns   = str(section_num)
    nw_n = _tw(draw, ns, font_num)
    nh_n = _th(draw, ns, font_num)
    draw.text((BX + (BADGE_SZ - nw_n) // 2, BY + (BADGE_SZ - nh_n) // 2),
              ns, font=font_num, fill=_COLOR_WHITE)

    # Section label next to badge
    font_label = _font(fp, 30)
    lh_label   = _th(draw, sec_label, font_label)
    draw.text((BX + BADGE_SZ + 16, BY + (BADGE_SZ - lh_label) // 2),
              sec_label, font=font_label, fill=(200, 205, 215, 255))

    # Divider
    div_y = BY + BADGE_SZ + 18
    draw.rectangle([(36, div_y), (CONTENT_W - 36, div_y + 2)], fill=(*accent, 100))

    # Large keyword centered vertically in remaining space
    if keyword:
        start_y  = div_y + 44
        avail_h  = CONTENT_H - start_y - 56
        avail_w  = CONTENT_W - 72

        for size in (112, 96, 80, 66, 54, 48):
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
        ns, font=font_ghost, fill=(r, g, b, 18),
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
) -> tuple[list[str], list[str | None]]:
    """Generate frame plates and content images per segment.

    Returns:
        (plate_paths, content_paths)
        plate_paths   — static chrome frames (1920×1080) with fixed characters
        content_paths — content images (CONTENT_W×CONTENT_H) to animate, or None for intro
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

    plate_paths   = []
    content_paths = []

    for seg in segments:
        idx         = seg.get("segment_index", 0)
        text        = seg.get("text", "")
        keyword     = seg.get("keyword", "")
        visual_type = seg.get("visual_type", "detail")
        if idx == 0:
            visual_type = "intro"

        sec_num, sec_label    = _section(idx)
        slide_label = badge_label if visual_type == "intro" else sec_label
        seg_img     = (segment_images or {}).get(idx)

        plate_path   = os.path.join(plates_dir,  f"plate_{idx:03d}.jpg")
        content_path = os.path.join(content_dir, f"content_{idx:03d}.jpg")

        try:
            if visual_type == "intro":
                plate = _plate_intro(fp, title, accent, slide_label, bg_image_path)
                plate.save(plate_path, "JPEG", quality=93)
                plate_paths.append(plate_path)
                content_paths.append(None)

            else:
                plate = _plate_standard(fp, accent, sec_num, slide_label,
                                        bg_image_path=bg_image_path)
                plate.save(plate_path, "JPEG", quality=93)
                plate_paths.append(plate_path)

                # Content image: actual image > keyword graphic > section graphic
                if seg_img and Path(seg_img).exists():
                    content_img = _content_from_image(seg_img)
                elif keyword:
                    content_img = _content_keyword(fp, keyword, accent, sec_num, sec_label)
                else:
                    content_img = _content_section(fp, accent, sec_num, sec_label)

                content_img.save(content_path, "JPEG", quality=93)
                content_paths.append(content_path)

        except Exception as e:
            logger.error(f"Slide {idx} generation failed ({visual_type}): {e}")
            fallback = _make_bg().convert("RGB")
            fallback.save(plate_path, "JPEG", quality=90)
            plate_paths.append(plate_path)
            _content_section(fp, accent, sec_num, sec_label).save(content_path, "JPEG", quality=90)
            content_paths.append(content_path)

        logger.info(f"Slide {idx} [{visual_type}]: plate={'ok'} content={'img' if content_paths[-1] else 'none'}")

    return plate_paths, content_paths
