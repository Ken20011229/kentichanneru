import logging
import os

from PIL import Image, ImageDraw, ImageFilter

logger = logging.getLogger(__name__)

_PALETTES = [
    ((15, 32, 78), (109, 213, 237)),
    ((11, 72, 107), (245, 98, 23)),
    ((36, 11, 54), (150, 20, 190)),
    ((10, 40, 10), (50, 200, 50)),
    ((60, 10, 10), (200, 50, 50)),
    ((20, 20, 60), (70, 130, 200)),
    ((40, 30, 10), (200, 160, 50)),
    ((10, 40, 50), (20, 160, 160)),
]


def generate_fallback_images(output_dir: str, count: int, width: int = 1920, height: int = 1080) -> list[str]:
    os.makedirs(output_dir, exist_ok=True)
    paths = []
    for i in range(count):
        c1, c2 = _PALETTES[i % len(_PALETTES)]
        img = _gradient(width, height, c1, c2)
        path = os.path.join(output_dir, f"fallback_{i:02d}.jpg")
        img.save(path, "JPEG", quality=90)
        paths.append(path)
    logger.info(f"Generated {count} fallback gradient images in {output_dir}")
    return paths


def _gradient(w: int, h: int, c1: tuple, c2: tuple) -> Image.Image:
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        r = int(c1[0] + (c2[0] - c1[0]) * t)
        g = int(c1[1] + (c2[1] - c1[1]) * t)
        b = int(c1[2] + (c2[2] - c1[2]) * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))
    return img.filter(ImageFilter.GaussianBlur(radius=2))
