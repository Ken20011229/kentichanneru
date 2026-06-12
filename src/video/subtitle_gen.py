import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ASS_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
Timer: 100.0000

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
Style: Default,Noto Sans CJK JP,54,&H00FFFFFF,&H00000000,&H80000000,-1,0,1,3,1,2,40,40,80

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _to_ass_time(secs: float) -> str:
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = secs % 60
    cs = int((s % 1) * 100)
    return f"{h}:{m:02d}:{int(s):02d}.{cs:02d}"


def generate_ass(segments: list[dict], output_path: str) -> str:
    """Generate an ASS subtitle file from TTS segments with timing."""
    lines = [ASS_HEADER.rstrip()]
    cursor = 0.0
    for seg in segments:
        start = _to_ass_time(cursor)
        end = _to_ass_time(cursor + seg["duration_sec"])
        text = (
            seg["text"]
            .replace("\\", "\\\\")
            .replace("{", "\\{")
            .replace("\n", "\\N")
        )
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
        cursor += seg["duration_sec"]

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    # Write with UTF-8 BOM required by FFmpeg ASS filter on Windows
    with open(output_path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")

    logger.info(f"Subtitle file generated: {output_path} ({len(segments)} cues, {cursor:.1f}s total)")
    return output_path
