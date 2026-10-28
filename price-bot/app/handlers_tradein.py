"""Трейд-ин от клиента: «хочу сдать устройство» → заявка владельцу → оценка → ответ клиента."""
import asyncio
import html
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InputMediaPhoto, Message, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import keyboards as kb
from .config import Config
from .db import DB
from .handlers_admin import IsAdmin
from .handlers_used import CONDITIONS, buttons, parse_battery
from .handlers_user import main_menu, notify_admins, user_link, welcome

router = Router(name="tradein")
admin_router = Router(name="tradein_admin")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())

MAX_PHOTOS = 5
_photo_lock = asyncio.Lock()


class TradeIn(StatesGroup):
    model = State()
    battery = State()
    condition = State()
    photos = State()
    phone = State()


class TradeOffer(StatesGroup):
    text = State()


def cancel() -> kb.InlineKeyboardMarkup:
    return kb.cancel_lead()  # «✖ Отмена» — в главное меню


@router.callback_query(F.data == "ti")
async def cb_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(TradeIn.model)
    await call.message.answer(
        "💰 <b>Сдать устройство в трейд-ин</b>\n\n"
        "Примем ваш смартфон, планшет, ноутбук или часы в зачёт покупки или выкупим. "
        "Ответьте на 5 коротких вопросов — мастер оценит и напишет сюда.\n\n"
        "<b>Шаг 1 из 5.</b> Какая модель? Например: <code>iPhone 13 128GB</code>",
        reply_markup=cancel())
    await call.answer()


@router.message(StateFilter(TradeIn.model), F.text, ~F.text.startswith("/"))
async def on_model(message: Message, state: FSMContext):
    await state.update_data(model=" ".join(message.text.split())[:120])
    await state.set_state(TradeIn.battery)
    await message.answer("<b>Шаг 2 из 5.</b> Какой процент аккумулятора? "
                         "На iPhone: Настройки → Аккумулятор → Состояние. Например: <code>87</code>",
                         reply_markup=buttons([("Не знаю", "tibat")], [("✖ Отмена", "home")]))


async def ask_condition(message: Message, state: FSMContext) -> None:
    await state.set_state(TradeIn.condition)
    b = InlineKeyboardBuilder()
    for idx, c in enumerate(CONDITIONS):
        b.button(text=c, callback_data=f"ticond:{idx}")
    b.adjust(2)
    b.row(kb.InlineKeyboardButton(text="✖ Отмена", callback_data="home"))
    await message.answer("<b>Шаг 3 из 5.</b> Внешнее состояние? Выберите или опишите своими словами "
                         "(сколы, трещины, ремонты).", reply_markup=b.as_markup())


@router.message(StateFilter(TradeIn.battery), F.text, ~F.text.startswith("/"))
async def on_battery(message: Message, state: FSMContext):
    await state.update_data(battery=parse_battery(message.text) or message.text.strip()[:40])
    await ask_condition(message, state)


@router.callback_query(StateFilter(TradeIn.battery), F.data == "tibat")
async def cb_battery_unknown(call: CallbackQuery, state: FSMContext):
    await state.update_data(battery="не знаю")
    await call.message.edit_reply_markup(reply_markup=None)
    await ask_condition(call.message, state)
    await call.answer()


async def ask_photos(message: Message, state: FSMContext) -> None:
    await state.update_data(photos=[])
    await state.set_state(TradeIn.photos)
    await message.answer(f"<b>Шаг 4 из 5.</b> Пришлите до {MAX_PHOTOS} фото устройства со всех сторон — "
                         "так оценка будет точнее. Можно пропустить.", reply_markup=photos_buttons())


def photos_buttons():
    return buttons([("✅ Готово", "tiphotos"), ("Пропустить", "tiphotos")], [("✖ Отмена", "home")])


@router.callback_query(StateFilter(TradeIn.condition), F.data.startswith("ticond:"))
async def cb_condition(call: CallbackQuery, state: FSMContext):
    await state.update_data(condition=CONDITIONS[int(call.data.split(":")[1])])
    await call.message.edit_reply_markup(reply_markup=None)
    await ask_photos(call.message, state)
    await call.answer()


@router.message(StateFilter(TradeIn.condition), F.text, ~F.text.startswith("/"))
async def on_condition(message: Message, state: FSMContext):
    await state.update_data(condition=message.text.strip()[:300])
    await ask_photos(message, state)


@router.message(StateFilter(TradeIn.photos), F.photo)
async def on_photo(message: Message, state: FSMContext):
    async with _photo_lock:
        photos = (await state.get_data()).get("photos", [])
        if len(photos) < MAX_PHOTOS:
            photos = photos + [message.photo[-1].file_id]
            await state.update_data(photos=photos)
    count = len(photos)
    await asyncio.sleep(1.5)  # альбом приходит пачкой — отвечаем один раз
    if len((await state.get_data()).get("photos", [])) == count:
        await message.answer(f"📷 Фото: {count}. Можно добавить ещё или нажать «✅ Готово».",
                             reply_markup=photos_buttons())


@router.callback_query(StateFilter(TradeIn.photos), F.data == "tiphotos")
async def cb_photos_done(call: CallbackQuery, state: FSMContext):
    await state.set_state(TradeIn.phone)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("<b>Шаг 5 из 5.</b> Оставьте номер телефона — кнопкой ниже или сообщением.",
                              reply_markup=kb.phone_request())
    await call.answer()


@router.message(StateFilter(TradeIn.phone), F.text == "Отмена")
async def on_phone_cancel(message: Message, state: FSMContext, db: DB, cfg: Config):
    await state.clear()
    await message.answer("Заявка отменена.", reply_markup=ReplyKeyboardRemove())
    await message.answer(welcome(cfg), reply_markup=main_menu(db, message.from_user.id, cfg))


@router.message(StateFilter(TradeIn.phone), F.contact | F.text)
async def on_phone(message: Message, state: FSMContext, bot: Bot, db: DB, cfg: Config):
    phone = message.contact.phone_number if message.contact else message.text.strip()
    if len(re.sub(r"\D", "", phone)) < 10:
        await message.answer("Не похоже на номер телефона. Пример: +7 900 123-45-67", reply_markup=kb.phone_request())
        return
    d = await state.get_data()
    user = message.from_user
    req_id = db.add_tradein(user.id, user.full_name, user.username, d["model"], d["battery"], d["condition"],
                            d.get("photos", []), phone)
    await state.clear()

    text = (f"💰 <b>Заявка на трейд-ин №{req_id}</b>\n👤 {user_link(user)}\n📱 {html.escape(phone)}\n\n"
            f"<b>{html.escape(d['model'])}</b>\n🔋 АКБ: {html.escape(d['battery'])}\n"
            f"✨ Состояние: {html.escape(d['condition'])}")
    for admin_id in cfg.admin_ids:
        try:
            if d.get("photos"):
                await bot.send_media_group(admin_id, [InputMediaPhoto(media=p) for p in d["photos"]])
            await bot.send_message(admin_id, text, reply_markup=buttons(
                [("💬 Предложить цену", f"tio:{req_id}")], [("❌ Не принимаем", f"tino:{req_id}")]))
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
    await message.answer(f"✅ Заявка №{req_id} отправлена! Мастер оценит устройство и напишет сюда, "
                         "обычно в течение рабочего дня.", reply_markup=ReplyKeyboardRemove())
    await message.answer(welcome(cfg), reply_markup=main_menu(db, user.id, cfg))


# --- владелец: оценка ---

@admin_router.callback_query(F.data.startswith("tio:"))
async def cb_offer(call: CallbackQuery, state: FSMContext, db: DB):
    req = db.tradein(int(call.data[4:]))
    if not req:
        await call.answer()
        return
    await state.set_state(TradeOffer.text)
    await state.update_data(req_id=req["id"])
    await call.message.answer(
        f"Напишите оценку для «{html.escape(req['model'])}» — клиент получит её как есть. Например:\n"
        "<code>Готовы принять за 32 000 ₽ при проверке в сервисе, или 35 000 ₽ в зачёт покупки</code>",
        reply_markup=buttons([("✖ Отмена", "tioback")]))
    await call.answer()


@admin_router.callback_query(StateFilter(TradeOffer.text), F.data == "tioback")
async def cb_offer_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Оценка не отправлена.")
    await call.answer()


@admin_router.message(StateFilter(TradeOffer.text), F.text, ~F.text.startswith("/"))
async def on_offer(message: Message, state: FSMContext, bot: Bot, db: DB):
    req = db.tradein((await state.get_data())["req_id"])
    await state.clear()
    offer = message.text.strip()[:1000]
    db.set_tradein(req["id"], "offered", offer)
    try:
        await bot.send_message(req["user_id"],
                               f"💰 <b>Оценка вашего {html.escape(req['model'])}</b> (заявка №{req['id']})\n\n"
                               f"{html.escape(offer)}",
                               reply_markup=buttons([("✅ Согласен", f"tiyes:{req['id']}"),
                                                     ("❌ Не подходит", f"tinot:{req['id']}")]))
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.answer("⚠️ Клиент заблокировал бота — позвоните ему: " + html.escape(req["phone"]))
        return
    await message.answer(f"✅ Оценка отправлена клиенту (заявка №{req['id']}). Его ответ придёт сюда.")


@admin_router.callback_query(F.data.startswith("tino:"))
async def cb_decline(call: CallbackQuery, bot: Bot, db: DB):
    req = db.tradein(int(call.data[5:]))
    if not req:
        await call.answer()
        return
    db.set_tradein(req["id"], "declined")
    try:
        await bot.send_message(req["user_id"], f"К сожалению, сейчас не можем принять {html.escape(req['model'])} "
                                               f"(заявка №{req['id']}). Спасибо, что обратились в AVIOR!")
    except (TelegramBadRequest, TelegramForbiddenError):
        pass
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Клиенту отправлен отказ")


# --- ответ клиента на оценку ---

@router.callback_query(F.data.startswith("tiyes:") | F.data.startswith("tinot:"))
async def cb_client_answer(call: CallbackQuery, bot: Bot, db: DB, cfg: Config):
    agree = call.data.startswith("tiyes:")
    req = db.tradein(int(call.data.split(":")[1]))
    if not req or req["user_id"] != call.from_user.id:
        await call.answer()
        return
    db.set_tradein(req["id"], "agreed" if agree else "rejected")
    await call.message.edit_reply_markup(reply_markup=None)
    if agree:
        await call.message.answer("🤝 Отлично! Мы свяжемся с вами, чтобы договориться о встрече. "
                                  "Адрес сервиса: Москва, Можайское ш., 4к1.")
    else:
        await call.message.answer("Понятно, спасибо! Если передумаете — пишите, будем рады помочь.")
    verdict = "✅ <b>согласен</b> — свяжитесь с ним" if agree else "❌ не согласен"
    await notify_admins(bot, cfg, f"💰 Трейд-ин №{req['id']} ({html.escape(req['model'])}): клиент {verdict}.\n"
                                  f"📱 {html.escape(req['phone'])}")
    await call.answer()
