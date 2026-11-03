"""Доставка сообщений клиенту в его мессенджер: Telegram, MAX или WhatsApp (см. app/platforms.py).

Всё, что бот пишет клиенту сам (статус заказа, ответ мастера, оценка трейд-ина, статус ремонта,
снижение цены, рассылка), идёт через этот модуль."""
from __future__ import annotations

import io
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, Message

from .config import Config
from .max_api import MaxApi, MaxError, buttons_from_markup, image, to_max
from .platforms import MAX, TELEGRAM, WHATSAPP, platform, wa_phone
from .wa_api import WaApi, WaError, flat_buttons, html_to_wa

log = logging.getLogger(__name__)
_apis: dict[str, MaxApi] = {}
_wa: dict[str, WaApi] = {}


class DeliveryError(Exception):
    """Сообщение не доставлено: клиент заблокировал бота, MAX не подключён и т.п."""


def max_api(cfg: Config) -> MaxApi:
    if not cfg.max_token:
        raise DeliveryError("MAX не подключён (нет MAX_TOKEN в .env)")
    api = _apis.get(cfg.max_token)
    if api is None:
        api = _apis[cfg.max_token] = MaxApi(cfg.max_token, cfg.max_api_url, cfg.max_ca_file)
    return api


def wa_api(cfg: Config) -> WaApi:
    if not (cfg.wa_token and cfg.wa_phone_id):
        raise DeliveryError("WhatsApp не подключён (нет WA_TOKEN и WA_PHONE_ID в .env)")
    api = _wa.get(cfg.wa_token)
    if api is None:
        api = _wa[cfg.wa_token] = WaApi(cfg.wa_token, cfg.wa_phone_id, cfg.wa_api_version)
    return api


async def send_wa(cfg: Config, user_id: int, text: str, markup: InlineKeyboardMarkup | None,
                  photo: bytes | None) -> None:
    api, to = wa_api(cfg), wa_phone(user_id)
    if not photo:
        await api.send(to, text, markup)
        return
    media_id = await api.upload_image(photo)
    buttons = flat_buttons(markup)
    if 0 < len(buttons) <= 3:
        await api.buttons(to, html_to_wa(text) or " ", buttons, image_id=media_id)
    else:
        await api.image(to, media_id, html_to_wa(text) or None)


async def send(bot: Bot, cfg: Config, user_id: int, text: str, markup: InlineKeyboardMarkup | None = None,
               photo: bytes | None = None) -> None:
    """text — HTML (тот же, что для Telegram: <b>, <i>, <s>, <code>, <a>)."""
    try:
        if platform(user_id) == WHATSAPP:
            await send_wa(cfg, user_id, text, markup, photo)
        elif platform(user_id) == MAX:
            api = max_api(cfg)
            attachments = [image(await api.upload_image(photo))] if photo else None
            await api.send(to_max(user_id), text, buttons_from_markup(markup), attachments)
        elif photo:
            from aiogram.types import BufferedInputFile
            await bot.send_photo(user_id, BufferedInputFile(photo, "photo.jpg"), caption=text[:1024],
                                 reply_markup=markup)
        else:
            await bot.send_message(user_id, text, reply_markup=markup)
    except (TelegramBadRequest, TelegramForbiddenError, MaxError, WaError) as e:
        raise DeliveryError(str(e)) from e
    except Exception as e:  # сеть до MAX и т.п.
        log.exception("Не удалось доставить сообщение %s", user_id)
        raise DeliveryError(str(e)) from e


async def copy(bot: Bot, cfg: Config, user_id: int, message: Message,
               markup: InlineKeyboardMarkup | None = None) -> None:
    """Пересылка сообщения владельца клиенту «как есть» (рассылка, ответ с фото).
    В MAX и WhatsApp уходят текст и фото; голосовые и файлы туда переслать нельзя — только подпись."""
    if platform(user_id) == TELEGRAM:
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
        raise DeliveryError("в MAX и WhatsApp можно переслать только текст или фото")
    await send(bot, cfg, user_id, text, markup, photo)
