"""Анимированные значки (premium custom emoji) во всём Telegram-боте: в тексте и на кнопках.

Telegram разрешает боту такие значки в личных чатах, если у владельца бота есть Telegram Premium.
Замены владелец задаёт командой /emoji: присылает пары «обычный значок → анимированный». Подмена
делается на отправке любого сообщения (EmojiMiddleware), поэтому экраны бота менять не нужно.
Если Telegram значок не принял (закончилась подписка, пост в канал), сообщение уходит с обычными.
MAX таких значков не знает — там всё как было."""
from __future__ import annotations

import json
import logging
import re

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, MessageEntity

from .db import DB

log = logging.getLogger(__name__)
SETTING = "custom_emoji"
VS16 = "️"  # «вариант эмодзи» после некоторых значков (⬅️, ♻️)
# Подсказка в /emoji: значки, которые чаще всего видят клиенты
COMMON = "👋 🛍 🔧 💰 💬 📞 🏠 📍 🚚 📝 🔎 🛒 🔍 ♻️ ⬅️ ✅ ❌ 📦 🕘 🔋 ✨ 📱 💻"


def base(ch: str) -> str:
    return ch.replace(VS16, "")


def emoji_map(db: DB) -> dict[str, str]:
    raw = db.get_setting(SETTING)
    return json.loads(raw) if raw else {}


def save(db: DB, m: dict[str, str] | None) -> None:
    db.set_setting(SETTING, json.dumps(m, ensure_ascii=False) if m else "")


def utf16_slice(text: str, offset: int, length: int) -> str:
    """Telegram считает позиции в UTF-16 — так эмодзи из двух «половинок» вырезаются правильно."""
    raw = text.encode("utf-16-le")
    return raw[offset * 2:(offset + length) * 2].decode("utf-16-le")


def pairs_from_message(text: str, entities: list[MessageEntity] | None) -> dict[str, str]:
    """Из сообщения владельца: обычный значок перед анимированным → {обычный: id анимированного}.
    Если перед анимированным нет обычного — берём значок, которым он подписан в наборе."""
    found = {}
    prev_end = 0
    for e in sorted(entities or [], key=lambda e: e.offset):
        if e.type != "custom_emoji":
            continue
        before = utf16_slice(text, prev_end, e.offset - prev_end).strip()
        alt = utf16_slice(text, e.offset, e.length)
        key = base(before) if before else base(alt)
        if key:
            found[key] = e.custom_emoji_id
        prev_end = e.offset + e.length
    return found


def animate_text(text: str, m: dict[str, str]) -> str:
    for key, custom_id in m.items():
        text = re.sub(re.escape(key) + VS16 + "?",
                      lambda mt, cid=custom_id: f'<tg-emoji emoji-id="{cid}">{mt.group(0)}</tg-emoji>', text)
    return text


def animate_markup(markup: InlineKeyboardMarkup, m: dict[str, str]) -> InlineKeyboardMarkup:
    """Значок в начале надписи кнопки → анимированная иконка кнопки (icon_custom_emoji_id)."""
    rows = []
    for row in markup.inline_keyboard:
        out = []
        for b in row:
            for key, custom_id in m.items():
                if b.text.startswith(key) and not b.icon_custom_emoji_id:
                    rest = b.text[len(key):].lstrip(VS16).lstrip()
                    if rest:  # у кнопки должна остаться надпись
                        b = b.model_copy(update={"text": rest, "icon_custom_emoji_id": custom_id})
                    break
            out.append(b)
        rows.append(out)
    return InlineKeyboardMarkup(inline_keyboard=rows)


class EmojiMiddleware(BaseRequestMiddleware):
    """Подменяет значки на анимированные во всех исходящих сообщениях бота."""

    def __init__(self, db: DB):
        self.db = db

    def animated(self, method, m: dict[str, str]):
        update = {}
        for field in ("text", "caption"):
            value = getattr(method, field, None)
            if isinstance(value, str) and value:
                new = animate_text(value, m)
                if new != value:
                    update[field] = new
        markup = getattr(method, "reply_markup", None)
        if isinstance(markup, InlineKeyboardMarkup):
            new_markup = animate_markup(markup, m)
            if new_markup != markup:
                update["reply_markup"] = new_markup
        return method.model_copy(update=update) if update else None

    async def __call__(self, make_request, bot, method):
        try:
            m = emoji_map(self.db)
        except Exception:
            m = {}
        changed = self.animated(method, m) if m else None
        if changed is None:
            return await make_request(bot, method)
        try:
            return await make_request(bot, changed)
        except TelegramBadRequest as e:
            log.info("Анимированные значки не приняты (%s) — отправляю с обычными", e.message[:100])
            return await make_request(bot, method)
