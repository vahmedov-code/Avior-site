"""Уведомления клиентам: товар из корзины подешевел или снова появился в прайсе."""
import asyncio
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .db import DB
from .pricing import fmt_price

log = logging.getLogger(__name__)
MARKUP = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🛒 Открыть корзину", callback_data="cart")],
    [InlineKeyboardButton(text="🔕 Не сообщать о ценах", callback_data="alertsoff")],
])


def format_alert(items) -> str:
    lines = ["🔔 <b>Хорошие новости по вашей корзине</b>", ""]
    for label, old, new, path in items[:15]:
        if old is None:
            lines.append(f"✅ {html.escape(label)} — снова в наличии: <b>{fmt_price(new)}</b>")
        else:
            lines.append(f"📉 {html.escape(label)}: <s>{fmt_price(old)}</s> → <b>{fmt_price(new)}</b>")
        if path:
            lines.append(f"    <i>{html.escape(path)}</i>")
    return "\n".join(lines)


async def send_price_alerts(bot: Bot, db: DB) -> int:
    """Вызывается после каждого обновления прайса. Возвращает, скольким клиентам написали."""
    sent = 0
    for user_id, items in db.price_alerts().items():
        try:
            await bot.send_message(user_id, format_alert(items), reply_markup=MARKUP)
            sent += 1
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        except Exception:  # сеть: уведомления не должны ломать обновление цен
            log.exception("Не удалось отправить уведомление о цене")
        await asyncio.sleep(0.05)  # не больше ~20 сообщений в секунду
    return sent
