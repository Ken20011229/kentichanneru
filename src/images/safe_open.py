import logging

from PIL import Image

logger = logging.getLogger(__name__)

# 動画/サムネ用途では数百万〜1千万台のピクセル数で十分。外部API/フェッチャーが
# 想定外に巨大な画像を返した場合、そのままconvert("RGBA")すると数GB単位の
# メモリを一気に確保してOOMを招くため、デコード時点で縮小してから読み込む。
_MAX_PIXELS = 25_000_000


def safe_open_rgba(path: str) -> Image.Image:
    """外部由来の画像を安全に開く。巨大画像はデコード前に縮小してメモリ膨張を防ぐ。"""
    img = Image.open(path)
    if img.width * img.height > _MAX_PIXELS:
        ratio = (_MAX_PIXELS / (img.width * img.height)) ** 0.5
        new_size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
        logger.warning(
            f"Oversized image {path} ({img.width}x{img.height}), downscaling to {new_size}"
        )
        try:
            img.draft(img.mode, new_size)
        except Exception:
            pass
        img = img.resize(new_size, Image.LANCZOS)
    return img.convert("RGBA")
