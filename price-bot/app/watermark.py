"""Логотип AVIOR в углу фотографии б/у устройства."""
from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

LOGO_PATH = Path(__file__).with_name("assets") / "logo.png"
SITE = "avior.moscow"
GOLD = (240, 200, 118)
MAX_SIDE = 2560  # больше Telegram всё равно не покажет


@lru_cache(maxsize=1)
def _logo() -> Image.Image:
    return Image.open(LOGO_PATH).convert("RGBA")


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)  # встроенный в Pillow шрифт, отдельно ставить не нужно
    except TypeError:  # старый Pillow без FreeType
        return ImageFont.load_default()


def apply(data: bytes) -> bytes:
    """Возвращает JPEG с логотипом и адресом сайта в правом нижнем углу."""
    photo = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    photo.thumbnail((MAX_SIDE, MAX_SIDE))
    w, h = photo.size
    side = max(40, round(min(w, h) * 0.13))
    margin = max(8, round(min(w, h) * 0.03))
    logo = _logo().resize((side, side), Image.LANCZOS)

    font = _font(max(12, round(side * 0.34)))
    left, top, right, bottom = font.getbbox(SITE)
    gap = round(side * 0.18)
    x_logo, y_logo = w - margin - side, h - margin - side
    x_text = x_logo - gap - (right - left)
    y_text = y_logo + (side - (bottom - top)) // 2 - top

    layer = Image.new("RGBA", photo.size, (0, 0, 0, 0))
    # мягкая тень под надписью — чтобы читалась и на светлом фоне
    shadow = Image.new("RGBA", photo.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((x_text, y_text), SITE, font=font, fill=(0, 0, 0, 200),
                                stroke_width=max(2, side // 18), stroke_fill=(0, 0, 0, 200))
    layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(max(1, side // 20))))
    ImageDraw.Draw(layer).text((x_text, y_text), SITE, font=font, fill=GOLD + (235,))
    layer.alpha_composite(logo, (x_logo, y_logo))

    out = Image.alpha_composite(photo.convert("RGBA"), layer).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=90)
    return buf.getvalue()
