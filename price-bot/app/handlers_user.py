import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

from . import keyboards as kb
from .config import Config
from .db import DB, CartLine
from .pricing import fmt_price

router = Router(name="user")

PAGE_SIZE = 25


class Lead(StatesGroup):
    text = State()


class Checkout(StatesGroup):
    delivery = State()
    phone = State()


def welcome(cfg: Config) -> str:
    return (
        f"👋 Добро пожаловать в <b>{html.escape(cfg.shop_name)}</b>!\n\n"
        f"{html.escape(cfg.welcome_text)}\n\nВыберите категорию:"
    )


def main_menu(db: DB, user_id: int):
    return kb.main_menu(db.categories(), db.cart_count(user_id))


async def safe_edit(call: CallbackQuery, text: str, markup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(text, reply_markup=markup)


async def notify_admins(bot: Bot, cfg: Config, text: str, markup=None) -> None:
    for admin_id in cfg.admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=markup)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass


def user_link(user) -> str:
    who = f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'
    if user.username:
        who += f" @{user.username}"
    return who


# --- меню и каталог ---

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db: DB, cfg: Config):
    await state.clear()
    await message.answer(welcome(cfg), reply_markup=main_menu(db, message.from_user.id))


@router.callback_query(F.data == "home")
async def cb_home(call: CallbackQuery, state: FSMContext, db: DB, cfg: Config):
    await state.clear()
    await safe_edit(call, welcome(cfg), main_menu(db, call.from_user.id))
    await call.answer()


@router.callback_query(F.data == "contacts")
async def cb_contacts(call: CallbackQuery, cfg: Config):
    await safe_edit(call, f"<b>{html.escape(cfg.shop_name)}</b>\n\n{html.escape(cfg.contacts_text)}", kb.back_home())
    await call.answer()


def render_section(db: DB, cfg: Config, user_id: int, cat, sec_idx: int, page: int):
    sections = db.sections(cat["id"])
    section = sections[sec_idx]
    items = db.items(cat["id"], section)
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    chunk = items[start:start + PAGE_SIZE]

    header = f"<b>{html.escape(cat['title'])}</b>"
    if section and section != cat["title"]:
        header += f"\n<b>{html.escape(section)}</b>"
    if cat["updated_at"]:
        stamp = datetime.fromtimestamp(cat["updated_at"], ZoneInfo(cfg.timezone)).strftime("%d.%m %H:%M")
        header += f"\n<i>Цены обновлены {stamp}</i>"
    if pages > 1:
        header += f"\nСтраница {page + 1} из {pages}"

    lines = [header, "", "Нажмите ➕ с номером товара, чтобы добавить его в корзину.", ""]
    numbered = []
    for no, it in enumerate(chunk, start=start + 1):
        line = f"{no}. {html.escape(it['name'])} — <b>{fmt_price(db.sell_price(it, cat))}</b>"
        if it["note"]:
            line += f" {html.escape(it['note'])}"
        lines.append(line)
        numbered.append((no, it["id"]))

    back = f"c:{cat['id']}" if len(sections) > 1 else "home"
    markup = kb.section_page(
        cat["id"], sec_idx, page, numbered, page > 0, page < pages - 1, back, db.cart_count(user_id)
    )
    return "\n".join(lines), markup


@router.callback_query(F.data.startswith("c:"))
async def cb_category(call: CallbackQuery, db: DB, cfg: Config):
    cat = db.category(int(call.data.split(":")[1]))
    sections = db.sections(cat["id"]) if cat else []
    if not sections:
        await call.answer("Цены скоро появятся", show_alert=True)
        return
    if len(sections) == 1:
        await safe_edit(call, *render_section(db, cfg, call.from_user.id, cat, 0, 0))
    else:
        await safe_edit(
            call,
            f"<b>{html.escape(cat['title'])}</b>\n\nВыберите раздел:",
            kb.sections_menu(cat["id"], sections, db.cart_count(call.from_user.id)),
        )
    await call.answer()


@router.callback_query(F.data.startswith("s:"))
async def cb_section(call: CallbackQuery, db: DB, cfg: Config):
    _, cat_id, sec_idx, page = call.data.split(":")
    cat = db.category(int(cat_id))
    if not cat or int(sec_idx) >= len(db.sections(cat["id"])):
        await call.answer("Прайс обновился, откройте категорию заново", show_alert=True)
        return
    await safe_edit(call, *render_section(db, cfg, call.from_user.id, cat, int(sec_idx), int(page)))
    await call.answer()


@router.callback_query(F.data.startswith("a:"))
async def cb_add(call: CallbackQuery, db: DB, cfg: Config):
    _, item_id, page = call.data.split(":")
    item = db.item(int(item_id))
    if not item:
        await call.answer("Прайс только что обновился — откройте раздел заново", show_alert=True)
        return
    db.add_to_cart(call.from_user.id, item)
    cat = db.category(item["category_id"])
    sec_idx = db.sections(cat["id"]).index(item["section"])
    _, markup = render_section(db, cfg, call.from_user.id, cat, sec_idx, int(page))
    try:
        await call.message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest:
        pass
    await call.answer(f"✅ В корзине: {item['name']}")


# --- корзина ---

def render_cart(lines: list[CartLine]) -> str:
    if not lines:
        return "🛒 Корзина пуста.\n\nВыберите товары в каталоге."
    out = ["🛒 <b>Корзина</b>", ""]
    total = 0
    for no, line in enumerate(lines, start=1):
        if line.available:
            total += line.qty * line.price
            qty = f"{line.qty} × " if line.qty > 1 else ""
            out.append(f"{no}. {html.escape(line.label)} — {qty}<b>{fmt_price(line.price)}</b>")
            if line.path:
                out.append(f"    <i>{html.escape(line.path)}</i>")
        else:
            out.append(f"{no}. <s>{html.escape(line.label)}</s> — ⚠️ закончился")
    out += ["", f"<b>Итого: {fmt_price(total)}</b>", "", "❌ с номером — убрать товар."]
    return "\n".join(out)


@router.callback_query(F.data == "cart")
async def cb_cart(call: CallbackQuery, state: FSMContext, db: DB):
    await state.clear()
    lines = db.cart_lines(call.from_user.id)
    await safe_edit(call, render_cart(lines), kb.cart_menu(lines) if lines else kb.back_home())
    await call.answer()


@router.callback_query(F.data.startswith("rm:"))
async def cb_cart_remove(call: CallbackQuery, db: DB):
    db.remove_cart_line(call.from_user.id, int(call.data.split(":")[1]))
    lines = db.cart_lines(call.from_user.id)
    await safe_edit(call, render_cart(lines), kb.cart_menu(lines) if lines else kb.back_home())
    await call.answer("Убрано")


@router.callback_query(F.data == "cartclear")
async def cb_cart_clear(call: CallbackQuery, db: DB):
    db.clear_cart(call.from_user.id)
    await safe_edit(call, render_cart([]), kb.back_home())
    await call.answer("Корзина очищена")


# --- оформление заказа ---

@router.callback_query(F.data == "checkout")
async def cb_checkout(call: CallbackQuery, state: FSMContext, db: DB, cfg: Config):
    lines = db.cart_lines(call.from_user.id)
    if not any(line.available for line in lines):
        await call.answer("В корзине нет доступных товаров", show_alert=True)
        return
    await state.set_state(Checkout.delivery)
    await safe_edit(call, render_cart(lines).rsplit("\n\n", 1)[0] + "\n\nКак удобно получить заказ?",
                    kb.delivery_menu(cfg.delivery_options))
    await call.answer()


@router.callback_query(StateFilter(Checkout.delivery), F.data.startswith("d:"))
async def cb_delivery(call: CallbackQuery, state: FSMContext, cfg: Config):
    idx = int(call.data.split(":")[1])
    if idx >= len(cfg.delivery_options):
        await call.answer()
        return
    await state.update_data(delivery=cfg.delivery_options[idx])
    await state.set_state(Checkout.phone)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(
        f"Доставка: {html.escape(cfg.delivery_options[idx])}\n\n"
        "Отправьте номер телефона кнопкой ниже или напишите его сообщением.",
        reply_markup=kb.phone_request(),
    )
    await call.answer()


@router.message(StateFilter(Checkout.phone), F.text == "Отмена")
async def checkout_cancel(message: Message, state: FSMContext, db: DB, cfg: Config):
    await state.clear()
    await message.answer("Оформление отменено, корзина сохранена.", reply_markup=ReplyKeyboardRemove())
    await message.answer(welcome(cfg), reply_markup=main_menu(db, message.from_user.id))


@router.message(StateFilter(Checkout.phone), F.contact | F.text)
async def checkout_phone(message: Message, state: FSMContext, bot: Bot, db: DB, cfg: Config):
    phone = message.contact.phone_number if message.contact else message.text.strip()
    if len(re.sub(r"\D", "", phone)) < 10:
        await message.answer("Не похоже на номер телефона. Пример: +7 900 123-45-67", reply_markup=kb.phone_request())
        return

    user = message.from_user
    data = await state.get_data()
    lines = db.cart_lines(user.id)
    if not any(line.available for line in lines):
        await state.clear()
        await message.answer("Корзина пуста — товары закончились.", reply_markup=ReplyKeyboardRemove())
        return

    order_id = db.create_order(user.id, user.full_name, user.username, phone, data["delivery"], lines)
    await state.clear()

    order = db.order(order_id)
    await notify_admins(bot, cfg, render_order_for_admin(db, order), kb.order_actions(order_id))
    await message.answer(
        f"✅ <b>Заказ №{order_id} оформлен!</b>\n\nСумма: {fmt_price(order['total'])}\n"
        f"Получение: {html.escape(order['delivery'])}\n\nМы скоро свяжемся с вами для подтверждения.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer(welcome(cfg), reply_markup=main_menu(db, user.id))


ORDER_STATUS = {"new": "🆕 новый", "ok": "✅ подтверждён", "no": "❌ отменён"}


def render_order_for_admin(db: DB, order) -> str:
    who = f'<a href="tg://user?id={order["user_id"]}">{html.escape(order["user_name"])}</a>'
    if order["username"]:
        who += f" @{order['username']}"
    out = [
        f"🛒 <b>Заказ №{order['id']}</b>",
        f"👤 {who}",
        f"📱 {html.escape(order['phone'])}",
        f"🚚 {html.escape(order['delivery'])}",
        "",
    ]
    for no, it in enumerate(db.order_items(order), start=1):
        qty = f"{it['qty']} × " if it["qty"] > 1 else ""
        out.append(f"{no}. {html.escape(it['name'])} — {qty}{fmt_price(it['price'])} (закуп {fmt_price(it['cost'])})")
        if it.get("path"):
            out.append(f"    📂 {html.escape(it['path'])}")
    out += [
        "",
        f"<b>Итого: {fmt_price(order['total'])}</b>",
        f"Закупка: {fmt_price(order['cost'])} · Наценка: {fmt_price(order['total'] - order['cost'])}",
    ]
    if order["status"] != "new":
        out += ["", f"<b>Статус: {ORDER_STATUS[order['status']]}</b>"]
    return "\n".join(out)


# --- вопрос менеджеру ---

@router.callback_query(F.data == "lead")
async def cb_lead(call: CallbackQuery, state: FSMContext):
    await state.set_state(Lead.text)
    await call.message.answer(
        "Напишите ваш вопрос — менеджер ответит в ближайшее время.",
        reply_markup=kb.cancel_lead(),
    )
    await call.answer()


@router.message(StateFilter(Lead.text), F.text)
async def lead_text(message: Message, state: FSMContext, bot: Bot, db: DB, cfg: Config):
    await notify_admins(bot, cfg, f"💬 <b>Вопрос</b> от {user_link(message.from_user)}\n\n{html.escape(message.text)}")
    await state.clear()
    await message.answer("✅ Спасибо! Вопрос отправлен, скоро ответим.")
    await message.answer(welcome(cfg), reply_markup=main_menu(db, message.from_user.id))
