"""Доставка сообщений клиенту в его мессенджер: Telegram или MAX (у клиентов MAX id отрицательные).

Всё, что бот пишет клиенту сам (статус заказа, ответ мастера, оценка трейд-ина, статус ремонта,
снижение цены, рассылка), идёт через этот модуль."""
from __future__ import annotations

import io
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, Message

from .config import Config
from .max_api import MaxApi, MaxError, buttons_from_markup, image, is_max_user, to_max

log = logging.getLogger(__name__)
_apis: dict[str, MaxApi] = {}


class DeliveryError(Exception):
    """Сообщение не доставлено: клиент заблокировал бота, MAX не подключён и т.п."""


def max_api(cfg: Config) -> MaxApi:
    if not cfg.max_token:
        raise DeliveryError("MAX не подключён (нет MAX_TOKEN в .env)")
    api = _apis.get(cfg.max_token)
    if api is None:
        api = _apis[cfg.max_token] = MaxApi(cfg.max_token, cfg.max_api_url, cfg.max_ca_file)
    return api


async def send(bot: Bot, cfg: Config, user_id: int, text: str, markup: InlineKeyboardMarkup | None = None,
               photo: bytes | None = None) -> None:
    """text — HTML (тот же, что для Telegram: <b>, <i>, <s>, <code>, <a>)."""
    try:
        if is_max_user(user_id):
            api = max_api(cfg)
            attachments = [image(await api.upload_image(photo))] if photo else None
            await api.send(to_max(user_id), text, buttons_from_markup(markup), attachments)
        elif photo:
            from aiogram.types import BufferedInputFile
            await bot.send_photo(user_id, BufferedInputFile(photo, "photo.jpg"), caption=text[:1024],
                                 reply_markup=markup)
        else:
            await bot.send_message(user_id, text, reply_markup=markup)
    except (TelegramBadRequest, TelegramForbiddenError, MaxError) as e:
        raise DeliveryError(str(e)) from e
    except Exception as e:  # сеть до MAX и т.п.
        log.exception("Не удалось доставить сообщение %s", user_id)
        raise DeliveryError(str(e)) from e


async def copy(bot: Bot, cfg: Config, user_id: int, message: Message,
               markup: InlineKeyboardMarkup | None = None) -> None:
    """Пересылка сообщения владельца клиенту «как есть» (рассылка, ответ с фото).
    В MAX уходят текст и фото; голосовые и файлы MAX-боту переслать нельзя — только подпись."""
    if not is_max_user(user_id):
        try:
            await bot.copy_message(user_id, message.chat.id, message.message_id, reply_markup=markup)
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            raise DeliveryError(str(e)) from e
        return
    text = message.html_text if (message.text or message.caption) else ""
    photo = None
    if message.photo:
        buf = io.BytesIO()
        await bot.download(message.photo[-1], destination=buf)
        photo = buf.getvalue()
    if not text and not photo:
        raise DeliveryError("в MAX можно переслать только текст или фото")
    await send(bot, cfg, user_id, text, markup, photo)
