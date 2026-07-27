import json
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from src.images.safe_open import safe_open_rgba
# 折り返しの禁則判定は字幕と同じものを使う（熟語・数字＋助数詞・漢数字の
# 位取りを行またぎで割らないための共通ルール）
from src.video.subtitle_gen import _splits_word

try:
    import resource
except ImportError:
    resource = None

logger = logging.getLogger(__name__)


def _memlog(tag: str) -> None:
    """Debug instrumentation for tracking down the 2026-07-15 thumbnail-stage OOM."""
    if resource is None:
        return
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    logger.info(f"[thumbmem] {tag}: rss={rss_mb:.0f}MB")


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


# 行頭に置いてはいけない文字 / 行末に置いてはいけない文字（サムネイル用）
_TH_NO_LINE_START = "、。，．）」』】〉》!?！？ー〜…・:;：；%％"
_TH_NO_LINE_END   = "（「『【〈《"


def _wrap_px(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    """max_w に収まるよう折り返す。日本語の禁則処理つき。

    禁則が無かったため、実生成したサムネイルで「5日連 / 続」「過 / 去最長」の
    ような熟語分断や、閉じ括弧「」」が行頭に来る割れ方が出ていた。
    """
    lines, cur = [], ""
    for i, ch in enumerate(text):
        test = cur + ch
        if _tw(draw, test, font) > max_w and cur:
            # 行末禁則: 開き括弧で終わるなら、その1文字を次行へ送る
            if cur[-1] in _TH_NO_LINE_END and len(cur) > 1:
                lines.append(cur[:-1])
                cur = cur[-1] + ch
                continue
            # 行頭禁則: 次行の先頭に来る文字が禁則なら、この行に押し込む
            if ch in _TH_NO_LINE_START:
                cur = test
                continue
            # 語中分断を避けて数文字だけ戻す（「観測史 / 上2番目」→
            # 「観測 / 史上2番目」）。判定は字幕側と同じものを使う。
            back = 0
            while back < 3 and len(cur) - back > 2 and _splits_word(text, i - back):
                back += 1
            if 0 < back < len(cur) - 1:
                lines.append(cur[:-back])
                cur = cur[-back:] + ch
                continue
            lines.append(cur)
            cur = ch
        else:
            cur = test
    if cur:
        lines.append(cur)
    # 孤児行（最終行が1文字）を解消する
    if len(lines) >= 2 and len(lines[-1]) == 1 and len(lines[-2]) >= 4:
        lines[-1] = lines[-2][-1] + lines[-1]
        lines[-2] = lines[-2][:-1]
    return lines


def _normalize_emphasis(text: str) -> str:
    """Normalize highlight brackets so the LLM's 「」 intent is respected even when
    it uses [ ] or full-width ［ ］ instead. Those become 「 」 so _split_highlights
    picks the span up as an accent-colored highlight (【 】 is left untouched — it is
    the hook-badge prefix handled separately)."""
    if not text:
        return text
    return (text.replace("［", "「").replace("］", "」")
                .replace("[", "「").replace("]", "」"))


def _extract_hook(text: str) -> tuple[str | None, str]:
    """Split 【hook】 prefix from main title text."""
    if text.startswith("【"):
        end = text.find("】")
        if end != -1:
            return text[1:end], text[end + 1:].strip()
    return None, text


_LAST_EPISODE: dict[str, int] = {}


def _get_and_increment_episode(channel_id: str) -> int:
    """Read, increment, and persist episode count for the given channel."""
    state_path = Path("data/episode_counts.json")
    state: dict = {}
    if state_path.exists():
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            pass
    count = state.get(channel_id, 0) + 1
    state[channel_id] = count
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    _LAST_EPISODE[channel_id] = count
    return count


def _peek_episode(channel_id: str) -> int:
    """同じ回の本編で発行済みの話数を、増やさずに読む。

    Shorts は以前 `channel_id + '_shorts'` という別カウンタを使っていたため、
    同じ動画なのに本編が「第10回」、Shortsが「第2回」と食い違っていた。
    Shorts から本編に来た視聴者にはシリーズが別物に見える。
    """
    if channel_id in _LAST_EPISODE:
        return _LAST_EPISODE[channel_id]
    state_path = Path("data/episode_counts.json")
    if state_path.exists():
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                return max(1, json.load(f).get(channel_id, 1))
        except Exception:
            pass
    return 1


def _fit_centered(draw, text: str, fp: str, max_w: int, top: int, bottom: int,
                  sizes: tuple, max_lines: int) -> tuple:
    """テキストを最大サイズで折り返し、縦方向の中央に置く。

    以前はどのスタイルも上詰めで描いていたため、短いサムネ文言だと下半分が
    まるごと空白になっていた（実測: 1280×720 のうち文字は左上の約470×160px
    だけで、右〜下の6割が真っ黒）。

    Returns: (font, lines, line_height, start_y)
    """
    font, lines = None, None
    for size in sizes:
        ft = _font(fp, size)
        cand = _wrap_px(draw, text, ft, max_w)
        lh   = max((_th(draw, ln, ft) for ln in cand), default=size) + int(size * 0.22)
        if len(cand) <= max_lines and lh * len(cand) <= (bottom - top):
            font, lines = ft, cand
            break
    if font is None:
        font = _font(fp, sizes[-1])
        lines = _wrap_px(draw, text, font, max_w)[:max_lines]

    lh    = max((_th(draw, ln, font) for ln in lines), default=44) + int(font.size * 0.22)
    total = lh * len(lines)
    start = top + max(0, (bottom - top - total) // 2)
    return font, lines, lh, start


def _split_highlights(text: str) -> list[tuple[str, bool]]:
    """Split text into (segment, is_highlighted) pairs for 「」 quoted text."""
    parts: list[tuple[str, bool]] = []
    i = 0
    while i < len(text):
        if text[i] == "「":
            end = text.find("」", i + 1)
            if end != -1:
                parts.append((text[i:end + 1], True))
                i = end + 1
                continue
        # Not a highlight start, or an unclosed 「 — search strictly after
        # the current position so `i` always advances (an unclosed 「 would
        # otherwise match itself at the same index and spin forever).
        next_open = text.find("「", i + 1)
        if next_open == -1:
            parts.append((text[i:], False))
            break
        parts.append((text[i:next_open], False))
        i = next_open
    return parts or [(text, False)]


def _draw_mixed_line(draw: ImageDraw.ImageDraw, line: str, font,
                     x: int, y: int, base_color: tuple, accent: tuple,
                     stroke_width: int = 5) -> None:
    """Render a single line with 「」 quoted spans in accent color."""
    parts = _split_highlights(line)
    cx = x
    for seg_text, highlighted in parts:
        color = (*accent, 255) if highlighted else base_color
        draw.text((cx, y), seg_text, font=font, fill=color,
                  stroke_width=stroke_width, stroke_fill=_TITLE_STROKE)
        bb = draw.textbbox((cx, y), seg_text, font=font)
        cx += bb[2] - bb[0]


def _highlight_flags(text: str) -> list[bool]:
    """Per-character mask: True when the char is inside a 「」 span (quotes
    included). Computed on the *full* title so highlighting survives wrapping."""
    flags = [False] * len(text)
    i = 0
    while i < len(text):
        if text[i] == "「":
            end = text.find("」", i + 1)
            if end != -1:
                for j in range(i, end + 1):
                    flags[j] = True
                i = end + 1
                continue
        i += 1
    return flags


def _draw_wrapped_highlighted(draw: ImageDraw.ImageDraw, full_text: str,
                              lines: list[str], font, x: int, y0: int,
                              base_color: tuple, accent: tuple, line_h: int,
                              stroke_width: int = 5) -> None:
    """Draw pre-wrapped `lines` (whose concatenation == full_text) applying the
    accent color to 「」 spans even when a span straddles a line break."""
    flags = _highlight_flags(full_text)
    pos = 0
    y = y0
    for ln in lines:
        lf = flags[pos:pos + len(ln)]
        pos += len(ln)
        cx = x
        seg, cur = "", None
        for ch, fl in zip(ln, lf):
            if cur is None:
                cur = fl
            if fl != cur:
                color = (*accent, 255) if cur else base_color
                draw.text((cx, y), seg, font=font, fill=color,
                          stroke_width=stroke_width, stroke_fill=_TITLE_STROKE)
                cx += draw.textbbox((cx, y), seg, font=font)[2] - cx
                seg, cur = ch, fl
            else:
                seg += ch
        if seg:
            color = (*accent, 255) if cur else base_color
            draw.text((cx, y), seg, font=font, fill=color,
                      stroke_width=stroke_width, stroke_fill=_TITLE_STROKE)
        y += line_h


def _draw_speech_bubble(canvas: Image.Image, text: str, font,
                        bx: int, by: int, accent: tuple) -> Image.Image:
    """Draw a comic speech bubble. Returns updated canvas."""
    tmp_draw = ImageDraw.Draw(canvas)
    pad_x, pad_y = 18, 12
    tw = _tw(tmp_draw, text, font)
    th = _th(tmp_draw, text, font)
    bw = tw + pad_x * 2
    bh = th + pad_y * 2
    tail = 22

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    # Bubble body
    ld.rounded_rectangle([(bx, by), (bx + bw, by + bh)],
                         radius=14, fill=(255, 255, 255, 245))
    ld.rounded_rectangle([(bx, by), (bx + bw, by + bh)],
                         radius=14, outline=(*accent, 255), width=4)
    # Triangular tail pointing down-left (toward character area)
    tail_pts = [(bx + 24, by + bh), (bx + 48, by + bh),
                (bx + 14, by + bh + tail)]
    ld.polygon(tail_pts, fill=(255, 255, 255, 245))
    ld.polygon(tail_pts, outline=(*accent, 220))
    canvas = Image.alpha_composite(canvas, layer)
    # Text
    d = ImageDraw.Draw(canvas)
    d.text((bx + pad_x, by + pad_y), text, font=font, fill=(20, 20, 20))
    return canvas


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
    _memlog("paste_chars_dark:start")

    # Zundamon (right, front, larger)
    if right_path and Path(right_path).exists():
        img = safe_open_rgba(right_path)
        _memlog(f"paste_chars_dark:right:opened size={img.size}")
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        target_h = int(H * 0.92)
        ratio    = target_h / img.height
        target_w = int(img.width * ratio)
        img      = img.resize((target_w, target_h), Image.LANCZOS)
        _memlog("paste_chars_dark:right:resized")
        bleed    = int(target_w * 0.08)
        zx       = W - target_w + bleed
        zy       = H - target_h
        canvas.paste(img, (zx, zy), img)
        inner_x  = zx
        _memlog("paste_chars_dark:right:pasted")
    else:
        zx = W

    # Tsumugi (left of Zundamon, slightly smaller, behind)
    if left_path and Path(left_path).exists():
        img = safe_open_rgba(left_path)
        _memlog(f"paste_chars_dark:left:opened size={img.size}")
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        target_h = int(H * 0.80)
        ratio    = target_h / img.height
        target_w = int(img.width * ratio)
        img      = img.resize((target_w, target_h), Image.LANCZOS)
        _memlog("paste_chars_dark:left:resized")
        overlap  = int(target_w * 0.12)
        tx       = zx - target_w + overlap
        ty       = H - target_h
        canvas.paste(img, (tx, ty), img)
        inner_x  = tx
        _memlog("paste_chars_dark:left:pasted")

    _memlog("paste_chars_dark:end")
    return canvas, inner_x


def generate_thumbnail(
    background_image_path: str,
    title_text: str,
    output_path: str,
    config: dict,
    reaction_text: str = "",
    style: str | None = None,
) -> str:
    """Generate dark-style 1280×720 YouTube thumbnail with both characters."""
    title_text = _normalize_emphasis(title_text)
    cfg      = config["thumbnail"]
    W, H     = cfg["width"], cfg["height"]
    fp       = cfg["font_path"]
    char_cfg = config.get("character", {})
    ch       = config.get("active_channel", {})
    accent   = tuple(ch.get("accent_color", list(_DEFAULT_ACCENT)))
    channel_id  = ch.get("id", "default")
    channel_name = ch.get("name", "")
    episode_num  = _get_and_increment_episode(channel_id)
    episode_label = f"第{episode_num}回"

    right_path = char_cfg.get("image_path", "")
    left_path  = char_cfg.get("left_image_path", "")

    # ── Dispatch to style-specific renderers ──────────────────────
    _render_kwargs = dict(
        bg_image_path=background_image_path, title_text=title_text,
        reaction_text=reaction_text, W=W, H=H, fp=fp, accent=accent,
        episode_label=episode_label, channel_name=channel_name,
        right_path=right_path, left_path=left_path,
    )
    _RENDERERS = {
        "IMPACT": _render_impact,
        "NEON":   _render_neon,
        "CARD":   _render_card,
        "BAND":   _render_band,
    }
    if style in _RENDERERS:
        canvas = _RENDERERS[style](**_render_kwargs)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        canvas.convert("RGB").save(output_path, "JPEG", quality=95)
        _memlog(f"generate_thumbnail:{style}:saved")
        logger.info(f"Thumbnail ({style}) → {output_path}")
        return output_path

    _memlog("generate_thumbnail:SPLIT:start")
    # ── 1. Background: photo if available, else dark bokeh ───────
    if background_image_path and Path(background_image_path).exists():
        img = safe_open_rgba(background_image_path)
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
    _memlog("generate_thumbnail:SPLIT:bg_done")

    # ── 2. Both characters on right half ─────────────────────────
    canvas, char_inner_x = _paste_chars_dark(canvas, right_path, left_path, W, H)
    _memlog("generate_thumbnail:SPLIT:chars_done")

    draw = ImageDraw.Draw(canvas)

    # ── 3. Top-left episode badge + channel name ──────────────────
    BX, BY = 30, 26
    ep_label   = f"第{episode_num}回"
    font_ep    = _font(fp, 38)
    ep_w       = _tw(draw, ep_label, font_ep)
    ep_h       = _th(draw, ep_label, font_ep)
    ep_px, ep_py = 16, 8
    ep_box_w   = ep_w + ep_px * 2
    ep_box_h   = ep_h + ep_py * 2
    draw.rounded_rectangle(
        [(BX, BY), (BX + ep_box_w, BY + ep_box_h)],
        radius=10, fill=(*accent, 255)
    )
    draw.text((BX + ep_px, BY + ep_py), ep_label, font=font_ep,
              fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0, 80))

    # Channel name small label below badge
    label_y = BY + ep_box_h + 8
    if channel_name:
        font_ch = _font(fp, 24)
        draw.text((BX, label_y), channel_name, font=font_ch,
                  fill=(255, 255, 255, 210), stroke_width=3, stroke_fill=_TITLE_STROKE)
        label_y += _th(draw, channel_name, font_ch) + 10

    # ── 4. Large golden title on left ─────────────────────────────
    hook, main_text = _extract_hook(title_text)

    TX      = 54
    TITLE_Y = label_y
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
    _memlog("generate_thumbnail:SPLIT:before_font_loop")
    font_title, lines, lh, title_top = _fit_centered(
        draw, main_text, fp, MAX_TW, TITLE_Y, H - 60,
        (156, 138, 120, 104, 88, 74, 60, 48), max_lines=3,
    )
    _memlog(f"generate_thumbnail:SPLIT:after_font_loop lines={len(lines)}")

    _draw_wrapped_highlighted(draw, main_text, lines, font_title, TX, title_top,
                              _TITLE_YELLOW, accent, lh, stroke_width=5)
    _memlog("generate_thumbnail:SPLIT:title_drawn")

    # ── 5. Speech bubble (reaction_text) ─────────────────────────
    if reaction_text and reaction_text.strip():
        font_bub = _font(fp, 32)
        bub_x = char_inner_x + 10
        bub_y = 26
        # Clamp so bubble doesn't go off right edge
        tmp_d = ImageDraw.Draw(canvas)
        bub_content_w = _tw(tmp_d, reaction_text.strip(), font_bub)
        if bub_x + bub_content_w + 40 > W - 20:
            bub_x = max(char_inner_x - 20, W - bub_content_w - 80)
        canvas = _draw_speech_bubble(canvas, reaction_text.strip(), font_bub,
                                     bub_x, bub_y, accent)
        draw = ImageDraw.Draw(canvas)
        _memlog("generate_thumbnail:SPLIT:bubble_drawn")

    # ── 6. Bottom accent stripe ───────────────────────────────────
    draw.rectangle([(0, H - 6), (W, H)], fill=(*accent, 255))

    _memlog("generate_thumbnail:SPLIT:before_save")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output_path, "JPEG", quality=95)
    _memlog("generate_thumbnail:SPLIT:saved")
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
    title_text = _normalize_emphasis(title_text)
    cfg      = config["thumbnail"]
    fp       = cfg["font_path"]
    W, H     = 1080, 1920
    ch       = config.get("active_channel", {})
    accent   = tuple(ch.get("accent_color", list(_DEFAULT_ACCENT)))
    channel_id   = ch.get("id", "default")
    channel_name = ch.get("name", "")
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
        char_img = safe_open_rgba(char_path)
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

    # ── Top-left episode badge + channel name ────────────────────
    BX, BY = 30, 34
    # 本編と同じ話数を使う（別カウンタだと同じ動画で「第10回」と「第2回」が
    # 併存し、Shorts→本編の回遊でシリーズが別物に見える）
    ep_label_s = f"第{_peek_episode(channel_id)}回"
    font_ep_s  = _font(fp, 40)
    ep_w_s     = _tw(draw, ep_label_s, font_ep_s)
    ep_h_s     = _th(draw, ep_label_s, font_ep_s)
    ep_px_s, ep_py_s = 18, 10
    ep_bw_s    = ep_w_s + ep_px_s * 2
    ep_bh_s    = ep_h_s + ep_py_s * 2
    draw.rounded_rectangle(
        [(BX, BY), (BX + ep_bw_s, BY + ep_bh_s)],
        radius=12, fill=(*accent, 255)
    )
    draw.text((BX + ep_px_s, BY + ep_py_s), ep_label_s, font=font_ep_s,
              fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0, 80))
    label_y_s = BY + ep_bh_s + 10
    if channel_name:
        font_ch_s = _font(fp, 28)
        draw.text((BX, label_y_s), channel_name, font=font_ch_s,
                  fill=(255, 255, 255, 210), stroke_width=3, stroke_fill=(0, 0, 0))
        label_y_s += _th(draw, channel_name, font_ch_s) + 12

    # ── White card with title ─────────────────────────────────────
    hook, main_text = _extract_hook(title_text)

    CARD_L   = 48
    CARD_R   = W - 48
    CARD_T   = label_y_s + 10
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

    # Render 「」 spans in accent color so the emphasized keyword pops, even if
    # the span wraps across lines.
    _draw_wrapped_highlighted(draw, main_text, lines, font_title, tx, ty,
                              (20, 20, 20), accent, lh, stroke_width=0)

    # ── Bottom accent stripe ──────────────────────────────────────
    draw.rectangle([(0, H - 8), (W, H)], fill=(*accent, 255))

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output_path, "JPEG", quality=95)
    logger.info(f"Shorts thumbnail generated: {output_path}")
    return output_path


# ═══════════════════════════════════════════════════════════════
#  Style-specific thumbnail renderers (all return RGBA Image)
# ═══════════════════════════════════════════════════════════════

def _glow_text(canvas: Image.Image, text: str, font,
               x: int, y: int, glow_color: tuple, text_color: tuple,
               glow_r: int = 14) -> Image.Image:
    """Draw text with colored glow blob under a sharp layer."""
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).text((x, y), text, font=font, fill=(*glow_color, 220))
    canvas = Image.alpha_composite(canvas, glow.filter(ImageFilter.GaussianBlur(glow_r)))
    ImageDraw.Draw(canvas).text((x, y), text, font=font, fill=text_color,
                                stroke_width=2, stroke_fill=(0, 0, 0))
    return canvas


def _bg_from_photo(bg_image_path: str, W: int, H: int,
                   blur: int = 6, overlay_alpha: int = 185) -> Image.Image:
    """Crop+blur a photo and add a dark overlay. Returns RGBA canvas."""
    if bg_image_path and Path(bg_image_path).exists():
        img = safe_open_rgba(bg_image_path)
        ir, cr = img.width / img.height, W / H
        if ir > cr:
            nw, nh = int(H * ir), H
        else:
            nw, nh = W, int(W / ir)
        img = img.resize((nw, nh), Image.LANCZOS)
        lf, tp = (nw - W) // 2, (nh - H) // 2
        img = img.crop((lf, tp, lf + W, tp + H))
        img = img.filter(ImageFilter.GaussianBlur(blur))
        ov = Image.new("RGBA", (W, H), (8, 10, 20, overlay_alpha))
        return Image.alpha_composite(img, ov)
    return None


def _ep_badge(draw: ImageDraw.ImageDraw, fp: str, episode_label: str, accent: tuple,
              bx: int, by: int, font_size: int = 38) -> tuple[int, int]:
    """Draw episode badge. Returns (box_width, box_height)."""
    font_ep = _font(fp, font_size)
    ew = _tw(draw, episode_label, font_ep)
    eh = _th(draw, episode_label, font_ep)
    px, py = 14, 7
    draw.rounded_rectangle([(bx, by), (bx + ew + px * 2, by + eh + py * 2)],
                           radius=9, fill=(*accent, 255))
    draw.text((bx + px, by + py), episode_label, font=font_ep,
              fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0, 60))
    return ew + px * 2, eh + py * 2


def _ch_label(draw: ImageDraw.ImageDraw, fp: str, channel_name: str,
              x: int, y: int, size: int = 24) -> int:
    """Draw channel name. Returns height consumed."""
    if not channel_name:
        return 0
    font_ch = _font(fp, size)
    draw.text((x, y), channel_name, font=font_ch,
              fill=(255, 255, 255, 205), stroke_width=3, stroke_fill=_TITLE_STROKE)
    return _th(draw, channel_name, font_ch) + 10


def _paste_chars_small(canvas: Image.Image, right_path: str, left_path: str,
                       W: int, H: int,
                       right_scale: float = 0.62, left_scale: float = 0.50) -> tuple:
    """Paste smaller characters (for IMPACT). Returns (canvas, char_inner_x)."""
    inner_x = W
    if right_path and Path(right_path).exists():
        img = safe_open_rgba(right_path)
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        th = int(H * right_scale)
        tw = int(img.width * th / img.height)
        img = img.resize((tw, th), Image.LANCZOS)
        bleed = int(tw * 0.06)
        zx = W - tw + bleed
        canvas.paste(img, (zx, H - th), img)
        inner_x = zx

    if left_path and Path(left_path).exists():
        img = safe_open_rgba(left_path)
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        th = int(H * left_scale)
        tw = int(img.width * th / img.height)
        img = img.resize((tw, th), Image.LANCZOS)
        tx = inner_x - tw + int(tw * 0.15)
        canvas.paste(img, (tx, H - th), img)
        inner_x = tx
    return canvas, inner_x


# ── IMPACT ────────────────────────────────────────────────────
def _render_impact(*, bg_image_path: str, title_text: str, reaction_text: str,
                   W: int, H: int, fp: str, accent: tuple,
                   episode_label: str, channel_name: str,
                   right_path: str, left_path: str) -> Image.Image:
    """Giant text spans full width, characters smaller in bottom-right corner."""
    # overlay_alpha 210 だと背景写真がほぼ黒く潰れ、何が写っているか分からない
    # 「暗い板」になっていた。写真として認識できる程度に残す。
    canvas = _bg_from_photo(bg_image_path, W, H, blur=6, overlay_alpha=155)
    if canvas is None:
        canvas = _make_dark_bg(W, H, accent)
    # Bokeh on top
    for rx, ry, r, color in _BOKEH_DARK:
        blob = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(blob).ellipse(
            [(int(W * rx) - r, int(H * ry) - r),
             (int(W * rx) + r, int(H * ry) + r)], fill=color)
        canvas = Image.alpha_composite(canvas, blob.filter(ImageFilter.GaussianBlur(r // 1.4)))

    canvas, char_inner_x = _paste_chars_small(canvas, right_path, left_path, W, H, 0.62, 0.50)

    draw = ImageDraw.Draw(canvas)

    # Tiny episode badge top-left
    BX, BY = 22, 18
    bw, bh = _ep_badge(draw, fp, episode_label, accent, BX, BY, font_size=28)
    TITLE_Y = BY + bh + 14

    # HUGE title — spans near-full width
    hook, main_text = _extract_hook(title_text)
    TX, MAX_TW = 22, W - 100
    if hook:
        font_hook = _font(fp, 40)
        hw, hh = _tw(draw, hook, font_hook), _th(draw, hook, font_hook)
        draw.rounded_rectangle([(TX, TITLE_Y), (TX + hw + 28, TITLE_Y + hh + 12)],
                               radius=7, fill=(*accent, 255))
        draw.text((TX + 14, TITLE_Y + 6), hook, font=font_hook, fill=(255, 255, 255))
        TITLE_Y += hh + 24

    font_title, lines, lh, ty = _fit_centered(
        draw, main_text, fp, MAX_TW, TITLE_Y, H - 40,
        (200, 176, 152, 132, 112, 94, 78, 64), max_lines=4,
    )
    # ⚠ 行単位で _draw_mixed_line を呼ぶと、「」が行をまたいだ瞬間に
    # `find("」")` が -1 を返してハイライトが丸ごと失われる（実測: CARD/NEON で
    # 「5日連続」が一切色付かなかった）。行をまたいでも効く実装を使う。
    _draw_wrapped_highlighted(draw, main_text, lines, font_title, TX, ty,
                              (255, 255, 255), accent, lh, stroke_width=10)

    # Speech bubble — 他の4スタイルは描いているのに IMPACT だけ reaction_text を
    # 引数で受け取ったまま一度も使っておらず、5本に1本のサムネから LLM に
    # 書かせたリアクションが黙って消えていた。
    if reaction_text and reaction_text.strip():
        font_bub = _font(fp, 30)
        tmp_d = ImageDraw.Draw(canvas)
        bub_w = _tw(tmp_d, reaction_text.strip(), font_bub) + 40
        bub_x = max(char_inner_x, W - bub_w - 20)
        canvas = _draw_speech_bubble(canvas, reaction_text.strip(), font_bub, bub_x, 26, accent)
        draw = ImageDraw.Draw(canvas)

    draw.rectangle([(0, H - 6), (W, H)], fill=(*accent, 255))
    return canvas


# ── NEON ──────────────────────────────────────────────────────
_NEON_BG = (4, 4, 18)

def _render_neon(*, bg_image_path: str, title_text: str, reaction_text: str,
                 W: int, H: int, fp: str, accent: tuple,
                 episode_label: str, channel_name: str,
                 right_path: str, left_path: str) -> Image.Image:
    """Near-black background, glowing accent text, character with edge light."""
    canvas = Image.new("RGBA", (W, H), (*_NEON_BG, 255))
    # Neon bokeh — alternate accent vs channel-shifted complementary
    for i, (rx, ry, r, _) in enumerate(_BOKEH_DARK):
        blob = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        col = (*accent, 90) if i % 2 == 0 else (accent[2], accent[0], accent[1], 70)
        ImageDraw.Draw(blob).ellipse(
            [(int(W * rx) - r, int(H * ry) - r),
             (int(W * rx) + r, int(H * ry) + r)], fill=col)
        canvas = Image.alpha_composite(canvas, blob.filter(ImageFilter.GaussianBlur(r // 1.2)))

    # Subtle horizontal scan lines
    scan = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(scan)
    for y in range(0, H, 4):
        sd.line([(0, y), (W, y)], fill=(*accent, 6))
    canvas = Image.alpha_composite(canvas, scan)

    canvas, char_inner_x = _paste_chars_dark(canvas, right_path, left_path, W, H)

    # Colored edge glow behind character area
    glow_stripe = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow_stripe).rectangle(
        [(char_inner_x - 30, 0), (W, H)], fill=(*accent, 28))
    canvas = Image.alpha_composite(
        canvas, glow_stripe.filter(ImageFilter.GaussianBlur(30)))

    draw = ImageDraw.Draw(canvas)

    # Episode badge
    BX, BY = 30, 26
    bw, bh = _ep_badge(draw, fp, episode_label, accent, BX, BY)
    label_y = BY + bh + 8
    label_y += _ch_label(draw, fp, channel_name, BX, label_y, size=24)

    # Neon glow title
    hook, main_text = _extract_hook(title_text)
    TX, TITLE_Y = 54, label_y
    MAX_TW = char_inner_x - TX - 30
    if hook:
        font_hook = _font(fp, 40)
        hw, hh = _tw(draw, hook, font_hook), _th(draw, hook, font_hook)
        draw.rounded_rectangle([(TX, TITLE_Y), (TX + hw + 28, TITLE_Y + hh + 12)],
                               radius=7, fill=(*accent, 255))
        draw.text((TX + 14, TITLE_Y + 6), hook, font=font_hook, fill=(255, 255, 255))
        TITLE_Y += hh + 22

    font_title, lines, lh, ty = _fit_centered(
        draw, main_text, fp, MAX_TW, TITLE_Y, H - 60,
        (150, 132, 114, 96, 80, 66, 54, 44), max_lines=3,
    )
    # グローは行ごとに、文字は行をまたぐハイライト対応の実装でまとめて描く
    glow_y = ty
    for ln in lines:
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(glow).text((TX, glow_y), ln, font=font_title, fill=(*accent, 200))
        canvas = Image.alpha_composite(canvas, glow.filter(ImageFilter.GaussianBlur(14)))
        glow_y += lh
    draw = ImageDraw.Draw(canvas)
    _draw_wrapped_highlighted(draw, main_text, lines, font_title, TX, ty,
                              (255, 255, 255), accent, lh, stroke_width=3)

    # Speech bubble
    if reaction_text and reaction_text.strip():
        font_bub = _font(fp, 30)
        tmp_d = ImageDraw.Draw(canvas)
        bub_w = _tw(tmp_d, reaction_text.strip(), font_bub) + 40
        bub_x = max(char_inner_x, W - bub_w - 20)
        canvas = _draw_speech_bubble(canvas, reaction_text.strip(), font_bub, bub_x, 26, accent)
        draw = ImageDraw.Draw(canvas)

    draw.rectangle([(0, H - 6), (W, H)], fill=(*accent, 255))
    return canvas


# ── CARD ──────────────────────────────────────────────────────
def _render_card(*, bg_image_path: str, title_text: str, reaction_text: str,
                 W: int, H: int, fp: str, accent: tuple,
                 episode_label: str, channel_name: str,
                 right_path: str, left_path: str) -> Image.Image:
    """Bright accent-color background, white card left, dark text, chars right."""
    # Bright accent background with diagonal stripe texture
    canvas = Image.new("RGBA", (W, H), (*accent, 255))
    pattern = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pattern)
    darker = tuple(max(0, c - 35) for c in accent[:3])
    for i in range(-W, W + H, 55):
        pd.polygon([(i, 0), (i + 38, 0), (i + 38 + H, H), (i + H, H)],
                   fill=(*darker, 255))
    canvas = Image.alpha_composite(canvas, pattern)
    # Slight dark overlay on right half (character area)
    right_overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(right_overlay).rectangle(
        [(W // 2, 0), (W, H)], fill=(0, 0, 0, 50))
    canvas = Image.alpha_composite(canvas, right_overlay)

    canvas, char_inner_x = _paste_chars_dark(canvas, right_path, left_path, W, H)

    # White card on left side
    CARD_L, CARD_T = 20, 18
    CARD_R = min(char_inner_x + 45, int(W * 0.60))
    CARD_B = H - 18
    shadow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow_layer).rounded_rectangle(
        [(CARD_L + 8, CARD_T + 10), (CARD_R + 8, CARD_B + 10)],
        radius=22, fill=(0, 0, 0, 55))
    canvas = Image.alpha_composite(canvas, shadow_layer.filter(ImageFilter.GaussianBlur(14)))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle([(CARD_L, CARD_T), (CARD_R, CARD_B)],
                           radius=22, fill=(255, 255, 255, 238))
    # Accent left border on card
    draw.rounded_rectangle([(CARD_L, CARD_T), (CARD_L + 8, CARD_B)],
                           radius=6, fill=(*accent, 255))

    # Episode badge inside card
    BX, BY = CARD_L + 22, CARD_T + 22
    bw, bh = _ep_badge(draw, fp, episode_label, accent, BX, BY, font_size=36)
    label_y = BY + bh + 8

    # Channel name in accent color on card
    if channel_name:
        font_ch = _font(fp, 22)
        draw.text((BX, label_y), channel_name, font=font_ch, fill=(*accent, 255))
        label_y += _th(draw, channel_name, font_ch) + 12

    # Title — dark text on white card
    _DARK_TEXT = (18, 22, 44)
    hook, main_text = _extract_hook(title_text)
    TX, TITLE_Y = BX, label_y
    MAX_TW = CARD_R - TX - 24
    if hook:
        font_hook = _font(fp, 36)
        hw, hh = _tw(draw, hook, font_hook), _th(draw, hook, font_hook)
        draw.rounded_rectangle([(TX, TITLE_Y), (TX + hw + 24, TITLE_Y + hh + 10)],
                               radius=7, fill=(*accent, 255))
        draw.text((TX + 12, TITLE_Y + 5), hook, font=font_hook, fill=(255, 255, 255))
        TITLE_Y += hh + 22

    font_title, lines, lh, ty = _fit_centered(
        draw, main_text, fp, MAX_TW, TITLE_Y, CARD_B - 24,
        (128, 112, 96, 82, 70, 58, 48, 40), max_lines=4,
    )
    _draw_wrapped_highlighted(draw, main_text, lines, font_title, TX, ty,
                              _DARK_TEXT, accent, lh, stroke_width=0)

    # Speech bubble
    if reaction_text and reaction_text.strip():
        font_bub = _font(fp, 30)
        tmp_d = ImageDraw.Draw(canvas)
        bub_w = _tw(tmp_d, reaction_text.strip(), font_bub) + 40
        bub_x = max(char_inner_x, W - bub_w - 20)
        canvas = _draw_speech_bubble(canvas, reaction_text.strip(), font_bub, bub_x, 26, accent)

    return canvas


# ── BAND ──────────────────────────────────────────────────────
def _render_band(*, bg_image_path: str, title_text: str, reaction_text: str,
                 W: int, H: int, fp: str, accent: tuple,
                 episode_label: str, channel_name: str,
                 right_path: str, left_path: str) -> Image.Image:
    """Dark bg + thick left accent stripe, title vertically centered — bold graphic design."""
    _memlog("render_band:start")
    canvas = _bg_from_photo(bg_image_path, W, H, blur=6, overlay_alpha=180)
    if canvas is None:
        canvas = _make_dark_bg(W, H, accent)
    _memlog("render_band:bg_done")
    # Extra bokeh for depth
    for i, (rx, ry, r, color) in enumerate(_BOKEH_DARK):
        blob = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(blob).ellipse(
            [(int(W * rx) - r, int(H * ry) - r),
             (int(W * rx) + r, int(H * ry) + r)], fill=color)
        canvas = Image.alpha_composite(canvas, blob.filter(ImageFilter.GaussianBlur(r // 1.4)))
        _memlog(f"render_band:bokeh:{i}")

    canvas, char_inner_x = _paste_chars_dark(canvas, right_path, left_path, W, H)
    _memlog("render_band:chars_done")

    draw = ImageDraw.Draw(canvas)

    # Bold left accent band (full height)
    BAND_W = 14
    draw.rectangle([(0, 0), (BAND_W, H)], fill=(*accent, 255))
    # Top + bottom stripes
    draw.rectangle([(0, 0), (W, 5)], fill=(*accent, 255))
    draw.rectangle([(0, H - 5), (W, H)], fill=(*accent, 255))

    # Episode badge next to band
    BX, BY = BAND_W + 18, 24
    bw, bh = _ep_badge(draw, fp, episode_label, accent, BX, BY, font_size=36)
    label_y = BY + bh + 8
    label_y += _ch_label(draw, fp, channel_name, BX, label_y, size=22)

    # Title — vertically centered in remaining space
    hook, main_text = _extract_hook(title_text)
    TX = BAND_W + 18
    MAX_TW = char_inner_x - TX - 30

    _memlog("render_band:before_font_loop")
    for size in (110, 94, 80, 66, 54, 44):
        ft = _font(fp, size)
        lines = _wrap_px(draw, main_text, ft, MAX_TW)
        lh = max((_th(draw, ln, ft) for ln in lines), default=size) + 14
        total_h = lh * len(lines)
        avail = H - 30 - label_y
        if len(lines) <= 4 and total_h <= avail:
            font_title = ft
            break
    else:
        font_title = _font(fp, 44)
        lines = _wrap_px(draw, main_text, font_title, MAX_TW)
        lh = max((_th(draw, ln, font_title) for ln in lines), default=44) + 14
        total_h = lh * len(lines)
    _memlog(f"render_band:after_font_loop lines={len(lines)}")

    avail = H - 30 - label_y
    TITLE_Y = label_y + max(0, (avail - total_h) // 2)

    if hook:
        font_hook = _font(fp, 38)
        hw, hh = _tw(draw, hook, font_hook), _th(draw, hook, font_hook)
        hy = TITLE_Y - hh - 20
        if hy > label_y:
            draw.rounded_rectangle([(TX, hy - 8), (TX + hw + 28, hy + hh + 8)],
                                   radius=7, fill=(*accent, 255))
            draw.text((TX + 14, hy), hook, font=font_hook, fill=(255, 255, 255))

    lh_actual = max((_th(draw, ln, font_title) for ln in lines), default=52) + 14
    _draw_wrapped_highlighted(draw, main_text, lines, font_title, TX, TITLE_Y,
                              (255, 255, 255), accent, lh_actual, stroke_width=5)
    _memlog("render_band:title_drawn")

    # Speech bubble
    if reaction_text and reaction_text.strip():
        font_bub = _font(fp, 30)
        tmp_d = ImageDraw.Draw(canvas)
        bub_w = _tw(tmp_d, reaction_text.strip(), font_bub) + 40
        bub_x = max(char_inner_x, W - bub_w - 20)
        canvas = _draw_speech_bubble(canvas, reaction_text.strip(), font_bub, bub_x, 26, accent)
        draw = ImageDraw.Draw(canvas)
        _memlog("render_band:bubble_drawn")

    _memlog("render_band:end")
    return canvas
