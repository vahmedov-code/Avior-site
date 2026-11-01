"""Анимированные значки (premium custom emoji) на первой странице Telegram-бота.

Telegram разрешает боту такие значки в личных чатах, если у владельца бота есть Telegram Premium.
Коды значков владелец задаёт командой /emoji: присылает анимированные версии значков по порядку.
MAX таких значков не знает — там остаются обычные."""
from __future__ import annotations

import json

from aiogram.types import InlineKeyboardMarkup

from .db import DB

# Значки первой страницы — в этом порядке их нужно прислать в /emoji
START_EMOJI = ("👋", "🛍", "🔧", "💰", "💬", "📞")
SETTING = "custom_emoji"
VS16 = "️"  # «вариант эмодзи» после некоторых значков


def emoji_map(db: DB) -> dict[str, str]:
    raw = db.get_setting(SETTING)
    return json.loads(raw) if raw else {}


def save(db: DB, ids: list[str] | None) -> None:
    db.set_setting(SETTING, json.dumps(dict(zip(START_EMOJI, ids))) if ids else "")


def animate_text(text: str, m: dict[str, str]) -> str:
    for base, custom_id in m.items():
        text = text.replace(base, f'<tg-emoji emoji-id="{custom_id}">{base}</tg-emoji>')
    return text


def animate_markup(markup: InlineKeyboardMarkup, m: dict[str, str]) -> InlineKeyboardMarkup:
    """Значок в начале надписи кнопки → анимированная иконка кнопки (icon_custom_emoji_id)."""
    rows = []
    for row in markup.inline_keyboard:
        out = []
        for b in row:
            for base, custom_id in m.items():
                if b.text.startswith(base):
                    rest = b.text[len(base):].lstrip(VS16).lstrip()
                    b = b.model_copy(update={"text": rest, "icon_custom_emoji_id": custom_id})
                    break
            out.append(b)
        rows.append(out)
    return InlineKeyboardMarkup(inline_keyboard=rows)
