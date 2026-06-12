import logging
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)


def generate_thumbnail(background_image_path: str, title_text: str, output_path: str, config: dict) -> str:
    cfg = config["thumbnail"]
    W, H = cfg["width"], cfg["height"]

    bg = Image.open(background_image_path).convert("RGBA")
    bg = bg.resize((W, H), Image.LANCZOS)

    # Apply blur to background for text readability
    from PIL import ImageFilter
    bg = bg.filter(ImageFilter.GaussianBlur(radius=3))

    # Dark gradient overlay
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, int(255 * cfg["overlay_opacity"])))
    composite = Image.alpha_composite(bg, overlay)
    draw = ImageDraw.Draw(composite)

    try:
        font_title = ImageFont.truetype(cfg["font_path"], cfg["font_size_title"])
    except Exception as e:
        logger.warning(f"Font load failed ({e}), falling back to default font")
        font_title = ImageFont.load_default()

    # Wrap title text
    max_chars = cfg.get("max_title_chars_per_line", 16)
    wrapped = textwrap.wrap(title_text, width=max_chars)
    line_height = cfg["font_size_title"] + 16
    total_h = len(wrapped) * line_height
    y = (H - total_h) // 2

    for line in wrapped:
        bbox = draw.textbbox((0, 0), line, font=font_title)
        text_w = bbox[2] - bbox[0]
        x = (W - text_w) // 2
        # Shadow layer
        draw.text((x + 4, y + 4), line, font=font_title, fill=(0, 0, 0, 220))
        draw.text((x, y), line, font=font_title, fill=tuple(cfg["title_color"]) + (255,))
        y += line_height

    result = composite.convert("RGB")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    result.save(output_path, "JPEG", quality=95)
    logger.info(f"Thumbnail generated: {output_path}")
    return output_path
