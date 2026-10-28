"""Публикация б/у устройств в Telegram-канал магазина: пост с фото и кнопкой «Купить в боте».
Продано — пост помечается «ПРОДАНО», изменилась цена — пост обновляется."""
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import Config
from .db import DB

log = logging.getLogger(__name__)
SETTING = "used_channel"


def channel_id(db: DB) -> int | None:
    value = db.get_setting(SETTING)
    return int(value) if value else None


async def buy_markup(bot: Bot, used_id: int) -> InlineKeyboardMarkup:
    me = await bot.me()
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛒 Купить в боте", url=f"https://t.me/{me.username}?start=bu{used_id}")]])


def _caption(cfg: Config, row, sold: bool) -> str:
    from .handlers_used import card_text  # здесь, а не вверху: handlers_used сам импортирует этот модуль
    text = card_text(cfg, row)
    return ("✅ <b>ПРОДАНО</b>\n\n" + text)[:1024] if sold else text


async def post_used(bot: Bot, db: DB, cfg: Config, used_id: int) -> str | None:
    """Публикует устройство в канал. Возвращает текст ошибки для владельца или None."""
    chat = channel_id(db)
    row = db.used(used_id)
    if not chat or not row or row["channel_msg_id"]:
        return None
    photos = db.used_photos(row)
    try:
        markup = await buy_markup(bot, used_id)
        if photos:
            msg = await bot.send_photo(chat, photos[0], caption=_caption(cfg, row, False), reply_markup=markup)
        else:
            msg = await bot.send_message(chat, _caption(cfg, row, False), reply_markup=markup)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        return f"Не получилось опубликовать в канал: {e.message}. Бот должен быть администратором канала."
    db.set_used_channel_msg(used_id, msg.message_id)
    return None


async def update_post(bot: Bot, db: DB, cfg: Config, used_id: int, sold: bool = False) -> None:
    """Новая цена или продажа — правим пост в канале (если он есть)."""
    chat = channel_id(db)
    row = db.used(used_id)
    if not chat or not row or not row["channel_msg_id"]:
        return
    markup = None if sold else await buy_markup(bot, used_id)
    try:
        if db.used_photos(row):
            await bot.edit_message_caption(chat_id=chat, message_id=row["channel_msg_id"],
                                           caption=_caption(cfg, row, sold), reply_markup=markup)
        else:
            await bot.edit_message_text(_caption(cfg, row, sold), chat_id=chat, message_id=row["channel_msg_id"],
                                        reply_markup=markup)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.warning("Пост б/у №%s в канале не обновлён: %s", used_id, e)
