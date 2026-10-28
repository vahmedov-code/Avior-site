"""Поиск: клиент пишет модель обычным сообщением — бот отвечает подходящими товарами."""
import html

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import keyboards as kb
from .db import DB
from .pricing import fmt_price
from .search import search

# Этот роутер подключается последним: сюда попадает текст, который не забрал ни один мастер (корзина, вопрос…)
router = Router(name="search")


@router.callback_query(F.data == "find")
async def cb_find(call: CallbackQuery):
    await call.message.answer("🔍 Напишите, что ищете — модель, память, цвет. Например:\n"
                              "<code>15 Pro 256</code> · <code>macbook air m3</code> · <code>airpods pro</code>",
                              reply_markup=kb.back_home())
    await call.answer()


def render_results(db: DB, query: str, user_id: int):
    hits, total = search(db, query)
    if not hits:
        text = (f"🔍 По запросу «{html.escape(query[:60])}» ничего не нашлось.\n\n"
                "Попробуйте короче, например <code>15 Pro</code>, или спросите менеджера — привезём под заказ.")
        return text, InlineKeyboardMarkup(inline_keyboard=[[kb.BTN_QUESTION], [kb.BTN_HOME]])
    lines = [f"🔍 <b>{html.escape(query[:60])}</b> — найдено {total}"]
    if total > len(hits):
        lines[0] += f", показаны {len(hits)} самых подходящих. Уточните запрос, чтобы сузить."
    lines += ["", "Нажмите ➕ с номером, чтобы добавить в корзину.", ""]
    b = InlineKeyboardBuilder()
    for no, h in enumerate(hits, start=1):
        lines.append(f"{no}. {html.escape(h.label)} — <b>{fmt_price(h.price)}</b>\n    <i>{html.escape(h.path)}</i>")
        # б/у открываем карточкой с фото, позицию прайса кладём в корзину сразу
        b.button(text=f"{'📷' if h.kind == 'used' else '➕'}{no}",
                 callback_data=f"bi:{h.id}" if h.kind == "used" else f"fa:{h.id}")
    b.adjust(5)
    count = db.cart_count(user_id)
    if count:
        b.row(kb.cart_button(count))
    b.row(kb.BTN_HOME)
    return "\n".join(lines)[:4000], b.as_markup()


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def on_query(message: Message, db: DB):
    text, markup = render_results(db, message.text.strip(), message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("fa:"))
async def cb_found_add(call: CallbackQuery, db: DB):
    item = db.item(int(call.data[3:]))
    if not item:
        await call.answer("Прайс обновился — повторите поиск", show_alert=True)
        return
    db.add_to_cart(call.from_user.id, item)
    # обновляем только кнопку корзины, список остаётся
    markup = call.message.reply_markup
    if markup:
        rows = [r for r in markup.inline_keyboard if not any(btn.callback_data == "cart" for btn in r)]
        rows.insert(len(rows) - 1, [kb.cart_button(db.cart_count(call.from_user.id))])
        try:
            await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except TelegramBadRequest:
            pass
    await call.answer(f"✅ В корзине: {item['name']}")

