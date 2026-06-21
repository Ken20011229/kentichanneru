import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_LINE_CHARS = 22


def _ass_color(r: int, g: int, b: int, alpha: int = 0x00) -> str:
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _make_header(accent_rgb: tuple, channel_name: str = "NEWS") -> str:
    r, g, b = accent_rgb
    lr = min(255, r + 70)
    lg = min(255, g + 70)
    lb = min(255, b + 70)

    accent_outline = _ass_color(r, g, b)
    accent_box     = _ass_color(r, g, b, 0x10)
    accent_text    = _ass_color(lr, lg, lb)
    banner_box     = "&H99201810"

    return f"""\
[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
Timer: 100.0000

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Detail,Noto Sans CJK JP,70,&H00FFFFFF,&H00FFFFFF,&H99000000,&H00000000,0,0,1,10,2,2,80,80,88,1
Style: Point,Noto Sans CJK JP,78,&H00FFFFFF,&H00FFFFFF,{accent_outline},&H00000000,-1,0,1,9,2,2,80,80,88,1
Style: Keyword,Noto Sans CJK JP,84,&H00FFFFFF,&H00FFFFFF,&H00000000,{accent_box},-1,0,3,0,0,2,80,80,88,1
Style: Intro,Noto Sans CJK JP,88,{accent_text},&H00FFFFFF,&H00000000,&HAA0A0A14,-1,0,3,0,0,2,80,80,88,1
Style: Banner_L,Noto Sans CJK JP,28,{accent_text},&H00000000,&H00000000,{banner_box},-1,0,3,3,0,7,16,0,14,1
Style: Banner_R,Noto Sans CJK JP,24,{accent_text},&H00000000,&H00000000,{banner_box},-1,0,3,3,0,9,0,16,14,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _to_ass_time(secs: float) -> str:
    h  = int(secs // 3600)
    m  = int((secs % 3600) // 60)
    s  = secs % 60
    cs = int((s % 1) * 100)
    return f"{h}:{m:02d}:{int(s):02d}.{cs:02d}"


def _split_lines(text: str, max_chars: int = _MAX_LINE_CHARS) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    lines = []
    while len(text) > max_chars:
        break_at = max_chars
        for i in range(max_chars, max(max_chars - 8, 0), -1):
            if i < len(text) and text[i - 1] in "、。！？…,.!? ":
                break_at = i
                break
        lines.append(text[:break_at])
        text = text[break_at:].lstrip()
    if text:
        lines.append(text)
    return lines


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{")


# ── Animation tag library ──────────────────────────────────────────────────────
# Each value is an ASS override tag block that controls entrance/exit animation.
# PlayRes: 1920×1080, default text anchor bottom-center (Y=992 at MarginV=88).

_ANIM_TAGS: dict[str, str] = {
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
    "slide_up":    "{\\move(960,1250,960,992,0,320)\\fad(160,130)}",
    # Drop from above screen
    "slide_down":  "{\\move(960,-150,960,992,0,320)\\fad(160,130)}",
    # Wipe in from right edge
    "slide_left":  "{\\move(2100,992,960,992,0,320)\\fad(160,130)}",
    # Wipe in from left edge
    "slide_right": "{\\move(-300,992,960,992,0,320)\\fad(160,130)}",
    # Float up slowly — calm / narrative
    "float_up":    "{\\move(960,1060,960,992,0,540)\\fad(300,150)}",
    # Typewriter-like: fast fade in with slight scale — snappy reveal
    "snap":        "{\\fscx95\\fscy95\\fad(60,160)\\t(0,120,\\fscx100\\fscy100)}",
}

# Visual-type default animations when LLM does not specify animation_style
_DEFAULT_ANIM: dict[str, str] = {
    "intro":   "scale_intro",
    "keyword": "scale_in",
    "point":   "slide_up",
    "detail":  "fade",
}


def _format_event(start: str, end: str, text: str,
                  visual_type: str, is_first: bool,
                  animation_style: str = None) -> str:
    """Build one ASS Dialogue line with per-type style and animation."""
    lines = _split_lines(text)
    body  = "\\N".join(_escape(ln) for ln in lines)

    # Style selection
    if is_first:
        style = "Intro"
    elif visual_type == "keyword":
        style = "Keyword"
        body  = "　" + body + "　"
    elif visual_type == "point":
        style = "Point"
    else:
        style = "Detail"

    # Animation selection: LLM choice > visual_type default
    anim_key = animation_style if (animation_style and animation_style in _ANIM_TAGS) \
               else _DEFAULT_ANIM.get("intro" if is_first else visual_type, "fade")
    tags = _ANIM_TAGS[anim_key]

    return f"Dialogue: 0,{start},{end},{style},,0,0,0,,{tags}{body}"


def generate_ass(segments: list[dict], output_path: str, channel: dict = None) -> str:
    ch          = channel or {}
    accent      = tuple(ch.get("accent_color", [50, 200, 80]))
    chan_name   = ch.get("badge_label", "NEWS")
    header      = _make_header(accent, chan_name)

    lines       = [header.rstrip()]
    total_dur   = sum(s["duration_sec"] for s in segments)
    vid_end     = _to_ass_time(total_dur)
    today       = date.today().strftime("%Y.%m.%d")

    lines.append(f"Dialogue: 0,0:00:00.00,{vid_end},Banner_L,,0,0,0,,{chan_name}")
    lines.append(f"Dialogue: 0,0:00:00.00,{vid_end},Banner_R,,0,0,0,,{today}")

    cursor = 0.0
    for i, seg in enumerate(segments):
        start = _to_ass_time(cursor)
        end   = _to_ass_time(cursor + seg["duration_sec"])
        lines.append(_format_event(
            start, end,
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
    dark_box = _ass_color(r // 4, g // 4, b // 4, 0x22)

    header = f"""\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
Timer: 100.0000

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
Style: Main,Noto Sans CJK JP,88,&H00FFFFFF,&H00000000,{dark_box},-1,0,3,0,0,2,60,60,140
Style: Hook,Noto Sans CJK JP,96,{_ass_color(r, g, b)},&H99000000,{dark_box},-1,0,3,0,0,2,60,60,140

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    # Shorts-specific animation pool (varied per segment index)
    _SHORTS_ANIMS = [
        "{\\fscx88\\fscy88\\fad(220,140)\\t(0,320,\\fscx100\\fscy100)}",   # scale_in
        "{\\move(540,2000,540,1780,0,300)\\fad(160,130)}",                  # slide_up (PlayRes 1080×1920)
        "{\\fscx65\\fscy65\\fad(80,150)\\t(0,200,\\fscx110\\fscy110)\\t(200,360,\\fscx100\\fscy100)}",  # pop
        "{\\blur10\\fad(280,140)\\t(0,420,\\blur0)}",                       # blur_in
        "{\\fscx92\\fscy92\\fad(180,140)\\t(0,240,\\fscx106\\fscy106)\\t(240,400,\\fscx100\\fscy100)}", # bounce
        "{\\frz-6\\fscx88\\fscy88\\fad(200,140)\\t(0,360,\\frz0\\fscx100\\fscy100)}",                  # spin_in
        "{\\move(1200,1780,540,1780,0,300)\\fad(150,130)}",                 # slide_left
        "{\\fad(60,150)\\fscx96\\fscy96\\t(0,120,\\fscx100\\fscy100)}",    # snap
    ]

    event_lines = [header.rstrip()]
    cursor = 0.0

    for i, seg in enumerate(segments):
        if cursor >= max_dur:
            break
        end_t  = min(cursor + seg["duration_sec"], max_dur)
        start  = _to_ass_time(cursor)
        end    = _to_ass_time(end_t)
        body   = "\\N".join(
            _escape(ln) for ln in _split_lines(seg["text"], max_chars=16)
        )
        style = "Hook" if i < 2 else "Main"

        # Use LLM-specified animation if available, else cycle through pool
        anim_key = seg.get("animation_style")
        if anim_key and anim_key in _ANIM_TAGS:
            tags = _ANIM_TAGS[anim_key]
        else:
            tags = _SHORTS_ANIMS[i % len(_SHORTS_ANIMS)]

        event_lines.append(f"Dialogue: 0,{start},{end},{style},,0,0,0,,{tags}{body}")
        cursor += seg["duration_sec"]

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(event_lines) + "\n")
    logger.info(f"Shorts subtitle generated: {output_path}")
    return output_path
