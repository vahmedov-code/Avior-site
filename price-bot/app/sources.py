"""Метки источников: откуда клиент пришёл в бота (сайт, Авито, QR-код в сервисе…).

Ссылка вида t.me/AVIORShop_bot?start=avito: при первом заходе бот запоминает метку «avito»,
а /stats показывает, сколько людей и заказов дал каждый источник."""
from __future__ import annotations

import io
import re

# Готовые метки для /links: код → подпись
SOURCES = {
    "site": "🌐 Сайт avior.moscow",
    "avito": "🟢 Авито",
    "qr": "📱 QR-код на стойке в сервисе",
    "receipt": "🧾 Квитанция ремонта",
    "card": "💳 Визитка",
    "yandex": "🗺 Яндекс Карты",
    "2gis": "🗺 2ГИС",
    "channel": "📢 Telegram-канал",
}
# Telegram разрешает в start-параметре латиницу, цифры, _ и -; bu12 — ссылка на карточку б/у, не метка
SOURCE_RE = re.compile(r"(?!bu\d+$)[A-Za-z0-9_-]{1,32}")


def clean(code: str | None) -> str | None:
    return code.lower() if code and SOURCE_RE.fullmatch(code) else None


def label(code: str | None) -> str:
    if not code:
        return "без метки (нашли сами, по @нику)"
    return SOURCES.get(code, f"🏷 {code}")


def link(bot_username: str, code: str) -> str:
    return f"https://t.me/{bot_username}?start={code}"


def qr_png(url: str) -> bytes:
    import qrcode  # только для /links — боту при запуске не нужен

    img = qrcode.make(url, box_size=12, border=3)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()
