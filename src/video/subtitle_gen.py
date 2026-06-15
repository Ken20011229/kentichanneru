import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_LINE_CHARS = 24


def _ass_color(r: int, g: int, b: int, alpha: int = 0x00) -> str:
    """Return ASS color string &HAABBGGRR."""
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _make_header(accent_rgb: tuple) -> str:
    """Build ASS header with channel-specific accent color for banner text."""
    r, g, b = accent_rgb
    lr = min(255, r + 55)
    lg = min(255, g + 55)
    lb = min(255, b + 55)
    banner_text = _ass_color(lr, lg, lb)
    banner_box  = "&H88201810"
    # Accent colour used for outline glow on main subtitles
    accent_outline = _ass_color(r, g, b)
    return f"""\
[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 1
Timer: 100.0000

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans CJK JP,72,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,-1,0,1,5,3,2,100,100,80,1
Style: Accent,Noto Sans CJK JP,72,&H00FFFFFF,&H00FFFFFF,{accent_outline},&HAA000000,-1,0,1,5,3,2,100,100,80,1
Style: Banner_L,Noto Sans CJK JP,30,{banner_text},&H00000000,&H00000000,{banner_box},-1,0,3,3,0,7,16,0,14,1
Style: Banner_R,Noto Sans CJK JP,26,{banner_text},&H00000000,&H00000000,{banner_box},-1,0,3,3,0,9,0,16,14,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _to_ass_time(secs: float) -> str:
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = secs % 60
    cs = int((s % 1) * 100)
    return f"{h}:{m:02d}:{int(s):02d}.{cs:02d}"


def _split_lines(text: str, max_chars: int = _MAX_LINE_CHARS) -> list[str]:
    """Break text into lines of at most max_chars, preferring punctuation boundaries."""
    if len(text) <= max_chars:
        return [text]
    lines = []
    while len(text) > max_chars:
        break_at = max_chars
        for i in range(max_chars, max(max_chars - 6, 0), -1):
            if i < len(text) and text[i - 1] in "、。！？…,.!? ":
                break_at = i
                break
        lines.append(text[:break_at])
        text = text[break_at:].lstrip()
    if text:
        lines.append(text)
    return lines


def generate_shorts_ass(segments: list[dict], output_path: str, channel: dict = None, max_dur: float = 58.0) -> str:
    """Generate ASS subtitles optimized for vertical Shorts (larger font, center-bottom)."""
    ch = channel or {}
    accent = tuple(ch.get("accent_color", [50, 200, 80]))
    r, g, b = accent

    header = f"""\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 1
Timer: 100.0000

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
Style: Default,Noto Sans CJK JP,72,&H00FFFFFF,&H00000000,&H88000000,-1,0,3,5,0,2,60,60,120

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header.rstrip()]
    cursor = 0.0
    for seg in segments:
        if cursor >= max_dur:
            break
        end_t = min(cursor + seg["duration_sec"], max_dur)
        start = _to_ass_time(cursor)
        end   = _to_ass_time(end_t)
        text_lines = _split_lines(seg["text"], max_chars=16)
        text = "\\N".join(
            line.replace("\\", "\\\\").replace("{", "\\{")
            for line in text_lines
        )
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{{\\fad(200,150)}}{text}")
        cursor += seg["duration_sec"]

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")
    logger.info(f"Shorts subtitle generated: {output_path}")
    return output_path


def generate_ass(segments: list[dict], output_path: str, channel: dict = None) -> str:
    ch = channel or {}
    accent = tuple(ch.get("accent_color", [50, 200, 80]))
    header = _make_header(accent)

    lines = [header.rstrip()]

    total_dur = sum(s["duration_sec"] for s in segments)
    vid_end = _to_ass_time(total_dur)
    today = date.today().strftime("%Y.%m.%d")

    # Persistent top-corner branding (full video duration, Layer 0)
    lines.append(f"Dialogue: 0,0:00:00.00,{vid_end},Banner_L,,0,0,0,,VOICEVOX NEWS")
    lines.append(f"Dialogue: 0,0:00:00.00,{vid_end},Banner_R,,0,0,0,,{today}")

    cursor = 0.0
    for seg in segments:
        start = _to_ass_time(cursor)
        end = _to_ass_time(cursor + seg["duration_sec"])
        text_lines = _split_lines(seg["text"])
        text = "\\N".join(
            line.replace("\\", "\\\\").replace("{", "\\{")
            for line in text_lines
        )
        # Alternate accent outline on keyword segments for variety
        style = "Accent" if seg.get("visual_type") in ("keyword", "point") else "Default"
        lines.append(f"Dialogue: 0,{start},{end},{style},,0,0,0,,{{\\fad(200,150)\\move(960,{1080-80},960,{1080-80})}}{text}")
        cursor += seg["duration_sec"]

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")

    logger.info(f"Subtitle file generated: {output_path} ({len(segments)} cues, {cursor:.1f}s total)")
    return output_path
