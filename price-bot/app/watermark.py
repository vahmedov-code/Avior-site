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


def _open(data: bytes) -> Image.Image:
    """Открывает фото сразу уменьшенным: полноразмерный снимок с телефона (48 Мп) занял бы
    сотни мегабайт, а боту выделено мало памяти."""
    img = Image.open(io.BytesIO(data))
    img.draft("RGB", (MAX_SIDE, MAX_SIDE))  # JPEG декодируется сразу в уменьшенном виде
    img = ImageOps.exif_transpose(img)
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    return img if img.mode == "RGB" else img.convert("RGB")


def apply(data: bytes) -> bytes:
    """Возвращает JPEG с логотипом и адресом сайта в правом нижнем углу."""
    photo = _open(data)
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

    # Рисуем только в углу с логотипом — слои размером со всё фото съели бы память
    pad = side // 4
    x0, y0 = max(0, x_text - pad), max(0, y_logo - pad)
    corner = photo.crop((x0, y0, w, h)).convert("RGBA")
    shadow = Image.new("RGBA", corner.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((x_text - x0, y_text - y0), SITE, font=font, fill=(0, 0, 0, 200),
                                stroke_width=max(2, side // 18), stroke_fill=(0, 0, 0, 200))
    # мягкая тень под надписью — чтобы читалась и на светлом фоне
    corner.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(max(1, side // 20))))
    ImageDraw.Draw(corner).text((x_text - x0, y_text - y0), SITE, font=font, fill=GOLD + (235,))
    corner.alpha_composite(logo, (x_logo - x0, y_logo - y0))
    photo.paste(corner.convert("RGB"), (x0, y0))

    buf = io.BytesIO()
    photo.save(buf, "JPEG", quality=90)
    return buf.getvalue()
