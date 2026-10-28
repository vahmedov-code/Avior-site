"""Ремонт: запись клиента и статус по номеру квитанции.

Статус ставит владелец командой /repair; клиент, который хоть раз проверил статус,
получает уведомление при каждом его изменении."""
import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import keyboards as kb
from .config import Config
from .db import DB
from .handlers_admin import IsAdmin
from .handlers_used import buttons
from .handlers_user import main_menu, notify_admins, safe_edit, user_link, welcome

router = Router(name="repair")
admin_router = Router(name="repair_admin")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())

STATUSES = ("🔍 Диагностика", "⏳ Ждём запчасть", "🛠 В ремонте", "✅ Готово — можно забирать", "📦 Выдано")
NUMBER_RE = re.compile(r"[\w-]{1,20}")


class RepairBook(StatesGroup):
    device = State()
    problem = State()
    phone = State()


class RepairStatus(StatesGroup):
    number = State()


def clean_number(text: str) -> str | None:
    """«№ 1234», «#1234» → «1234»."""
    number = text.strip().lstrip("№#").strip().upper()
    return number if NUMBER_RE.fullmatch(number) else None


@router.callback_query(F.data == "repair")
async def cb_repair(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call, "🔧 <b>Ремонт в сервисе AVIOR</b>\n\n"
                          "Ноутбуки, смартфоны, планшеты, игровые приставки: диагностика, замена экранов и "
                          "аккумуляторов, ремонт плат, чистка.\n📍 Москва, Можайское ш., 4к1",
                    buttons([("📝 Записаться на ремонт", "rpnew")], [("🔎 Статус ремонта", "rpstatus")],
                            [("🏠 В меню", "home")]))
    await call.answer()


# --- запись ---

@router.callback_query(F.data == "rpnew")
async def cb_book(call: CallbackQuery, state: FSMContext):
    await state.set_state(RepairBook.device)
    await call.message.answer("<b>Шаг 1 из 3.</b> Что за устройство? Например: <code>iPhone 13</code>, "
                              "<code>ноутбук ASUS TUF</code>, <code>PS5</code>", reply_markup=kb.cancel_lead())
    await call.answer()


@router.message(StateFilter(RepairBook.device), F.text, ~F.text.startswith("/"))
async def on_device(message: Message, state: FSMContext):
    await state.update_data(device=message.text.strip()[:120])
    await state.set_state(RepairBook.problem)
    await message.answer("<b>Шаг 2 из 3.</b> Что случилось? Опишите коротко: "
                         "<code>разбит экран</code>, <code>не включается после воды</code>, <code>греется</code>",
                         reply_markup=kb.cancel_lead())


@router.message(StateFilter(RepairBook.problem), F.text, ~F.text.startswith("/"))
async def on_problem(message: Message, state: FSMContext):
    await state.update_data(problem=message.text.strip()[:1000])
    await state.set_state(RepairBook.phone)
    await message.answer("<b>Шаг 3 из 3.</b> Оставьте номер телефона — мастер перезвонит и подскажет "
                         "цену и сроки.", reply_markup=kb.phone_request())


@router.message(StateFilter(RepairBook.phone), F.text == "Отмена")
async def on_book_cancel(message: Message, state: FSMContext, db: DB, cfg: Config):
    await state.clear()
    await message.answer("Запись отменена.", reply_markup=ReplyKeyboardRemove())
    await message.answer(welcome(cfg), reply_markup=main_menu(db, message.from_user.id, cfg))


@router.message(StateFilter(RepairBook.phone), F.contact | F.text)
async def on_book_phone(message: Message, state: FSMContext, bot: Bot, db: DB, cfg: Config):
    phone = message.contact.phone_number if message.contact else message.text.strip()
    if len(re.sub(r"\D", "", phone)) < 10:
        await message.answer("Не похоже на номер телефона. Пример: +7 900 123-45-67", reply_markup=kb.phone_request())
        return
    d = await state.get_data()
    await state.clear()
    await notify_admins(bot, cfg, f"🔧 <b>Запись на ремонт</b>\n👤 {user_link(message.from_user)}\n"
                                  f"📱 {html.escape(phone)}\n\n<b>{html.escape(d['device'])}</b>\n"
                                  f"{html.escape(d['problem'])}")
    await message.answer("✅ Заявка принята! Мастер перезвонит, подскажет цену и сроки.\n"
                         "📍 Москва, Можайское ш., 4к1", reply_markup=ReplyKeyboardRemove())
    await message.answer(welcome(cfg), reply_markup=main_menu(db, message.from_user.id, cfg))


# --- статус для клиента ---

def status_text(cfg: Config, row) -> str:
    stamp = datetime.fromtimestamp(row["updated_at"], ZoneInfo(cfg.timezone)).strftime("%d.%m %H:%M")
    device = f" · {html.escape(row['device'])}" if row["device"] else ""
    return (f"🔧 <b>Квитанция №{html.escape(row['number'])}</b>{device}\n\n"
            f"Статус: <b>{html.escape(row['status'])}</b>\n<i>обновлено {stamp}</i>")


@router.callback_query(F.data == "rpstatus")
async def cb_status(call: CallbackQuery, state: FSMContext):
    await state.set_state(RepairStatus.number)
    await call.message.answer("Напишите номер квитанции, например <code>1234</code>.", reply_markup=kb.cancel_lead())
    await call.answer()


@router.message(StateFilter(RepairStatus.number), F.text, ~F.text.startswith("/"))
async def on_status_number(message: Message, state: FSMContext, db: DB, cfg: Config):
    number = clean_number(message.text)
    if not number:
        await message.answer("Номер квитанции — это цифры, например <code>1234</code>.", reply_markup=kb.cancel_lead())
        return
    await state.clear()
    row = db.repair(number)
    if not row:
        await message.answer(f"По квитанции №{html.escape(number)} пока нет информации. Проверьте номер или "
                             "напишите нам — подскажем.", reply_markup=buttons([("💬 Задать вопрос", "lead")],
                                                                              [("🏠 В меню", "home")]))
        return
    db.watch_repair(number, message.from_user.id)
    await message.answer(status_text(cfg, row) + "\n\n🔔 Сообщим сюда, когда статус изменится.",
                         reply_markup=kb.back_home())


# --- владелец: статусы ---

def statuses_markup(number: str):
    b = InlineKeyboardBuilder()
    for idx, s in enumerate(STATUSES):
        b.button(text=s, callback_data=f"rs:{idx}:{number}")
    b.adjust(2)
    return b.as_markup()


async def save_status(bot: Bot, db: DB, cfg: Config, number: str, status: str, device: str | None) -> str:
    watchers = db.set_repair(number, status, device)
    row = db.repair(number)
    sent = 0
    for user_id in watchers:
        try:
            await bot.send_message(user_id, "🔔 Обновился статус ремонта\n\n" + status_text(cfg, row),
                                   reply_markup=kb.back_home())
            sent += 1
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
    who = f"клиент уведомлён ({sent})" if sent else "клиент ещё не проверял статус в боте — увидит, когда проверит"
    return f"✅ Квитанция №{html.escape(number)}: {html.escape(status)}\n{who}"


@admin_router.message(Command("repair"))
async def cmd_repair(message: Message, command: CommandObject, bot: Bot, db: DB, cfg: Config):
    args = (command.args or "").strip()
    if not args:
        lines = ["<b>Статусы ремонта</b>",
                 "/repair 1234 — выбрать статус кнопкой",
                 "/repair 1234 Ждём дисплей до пятницы — свой текст",
                 "/repair 1234 iPhone 13 | Диагностика — с названием устройства", ""]
        for r in db.recent_repairs():
            lines.append(f"№{html.escape(r['number'])} {html.escape(r['device'])} — {html.escape(r['status'])}")
        await message.answer("\n".join(lines))
        return
    number_raw, _, rest = args.partition(" ")
    number = clean_number(number_raw)
    if not number:
        await message.answer("Номер квитанции — цифры или буквы без пробелов, например <code>1234</code>.")
        return
    if not rest.strip():
        row = db.repair(number)
        now = f"Сейчас: {html.escape(row['status'])}" if row else "Новая квитанция."
        await message.answer(f"Квитанция №{html.escape(number)}. {now}\nВыберите статус:",
                             reply_markup=statuses_markup(number))
        return
    device, _, status = rest.partition("|") if "|" in rest else ("", "", rest)
    await message.answer(await save_status(bot, db, cfg, number, status.strip()[:300], device.strip()[:120] or None))


@admin_router.callback_query(F.data.startswith("rs:"))
async def cb_set_status(call: CallbackQuery, bot: Bot, db: DB, cfg: Config):
    _, idx, number = call.data.split(":", 2)
    await call.message.edit_text(await save_status(bot, db, cfg, number, STATUSES[int(idx)], None))
    await call.answer()
