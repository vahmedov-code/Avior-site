"""Ремонт: запись клиента и статус заказа.

Запись уходит в CRM сервиса (как заявки с сайта) и владельцу в Telegram. Статус берётся из CRM
по номеру заказа и телефону; бот периодически проверяет его и сообщает клиенту об изменениях.
Для заказов, которых нет в CRM, владелец может поставить статус вручную командой /repair."""
import asyncio
import html
import logging
import re
import time
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
from .crm import CrmUnavailable, order_status, send_lead
from .db import DB
from .handlers_admin import IsAdmin
from .handlers_reply import reply_markup
from .handlers_used import buttons
from .handlers_user import main_menu, notify_admins, safe_edit, user_link, welcome

router = Router(name="repair")
admin_router = Router(name="repair_admin")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())

log = logging.getLogger(__name__)
CRM_POLL = 20 * 60          # как часто проверять статусы в CRM, секунд
CRM_WATCH_DAYS = 60         # дольше не следим за заказом
DONE_WORDS = ("выдан", "закрыт", "отказ")  # после такого статуса следить больше не нужно
STATUSES = ("🔍 Диагностика", "⏳ Ждём запчасть", "🛠 В ремонте", "✅ Готово — можно забирать", "📦 Выдано")
NUMBER_RE = re.compile(r"[\w-]{1,20}")


# Факты — с сайта avior.moscow (услуги, этапы работы, гарантия, режим работы)
REPAIR_TEXT = """🔧 <b>Ремонт в сервисе AVIOR</b>
<i>От замены экрана до компонентного ремонта плат</i>

<b>Что ремонтируем</b>
💻 Ноутбуки и моноблоки Mac / PC
📱 Смартфоны и планшеты
🎮 Игровые приставки
🖥 Сборка и апгрейд ПК

<b>Что делаем</b>
🔬 Диагностика под микроскопом — <b>бесплатно при ремонте</b>
🧩 Ремонт материнских плат, BGA-пайка
📲 Замена и переклейка стекла дисплеев
🔋 Замена аккумуляторов
❄️ Чистка и обслуживание охлаждения
💾 Спасение данных

<b>Как работаем</b>
1️⃣ Диагностика — ищем причину, а не симптом
2️⃣ Согласование — цена и срок фиксируются до начала работ
3️⃣ Ремонт — с фотофиксацией этапов
4️⃣ Гарантия на работу и установленные запчасти

📍 Москва, Можайское ш., 4к1
🕘 Ежедневно, приём по записи"""


class RepairBook(StatesGroup):
    device = State()
    problem = State()
    phone = State()


class RepairStatus(StatesGroup):
    number = State()
    phone = State()


def contact_phone(message: Message) -> str:
    """Номер из кнопки «Отправить контакт» Telegram присылает без «+» — приводим к виду +7…"""
    if message.contact:
        phone = message.contact.phone_number
        return phone if phone.startswith("+") else f"+{phone}"
    return message.text.strip()


def clean_number(text: str) -> str | None:
    """«№ 1234», «#1234» → «1234»."""
    number = text.strip().lstrip("№#").strip().upper()
    return number if NUMBER_RE.fullmatch(number) else None


@router.callback_query(F.data == "repair")
async def cb_repair(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call, REPAIR_TEXT,
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
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
    await message.answer(welcome(cfg), reply_markup=main_menu(db, message.from_user.id, cfg))


@router.message(StateFilter(RepairBook.phone), F.contact | F.text)
async def on_book_phone(message: Message, state: FSMContext, bot: Bot, db: DB, cfg: Config):
    phone = contact_phone(message)
    if len(re.sub(r"\D", "", phone)) < 10:
        await message.answer("Не похоже на номер телефона. Пример: +7 900 123-45-67", reply_markup=kb.phone_request())
        return
    d = await state.get_data()
    await state.clear()
    user = message.from_user
    tg = f", @{user.username}" if user.username else ""
    error = await send_lead(cfg, user.full_name, phone,
                            f"{d['device']}: {d['problem']}\n(заявка из Telegram-бота{tg})")
    crm = "✅ записана в CRM" if error is None else f"⚠️ в CRM не попала: {html.escape(error)}"
    await notify_admins(bot, cfg, f"🔧 <b>Запись на ремонт</b>\n👤 {user_link(user)}\n"
                                  f"📱 {html.escape(phone)}\n\n<b>{html.escape(d['device'])}</b>\n"
                                  f"{html.escape(d['problem'])}\n\n{crm}", reply_markup(user.id))
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
    await call.message.answer("Напишите номер заказа (он есть в квитанции), например <code>1234</code>.",
                              reply_markup=kb.cancel_lead())
    await call.answer()


@router.message(StateFilter(RepairStatus.number), F.text, ~F.text.startswith("/"))
async def on_status_number(message: Message, state: FSMContext):
    number = clean_number(message.text)
    if not number:
        await message.answer("Номер заказа — это цифры, например <code>1234</code>.", reply_markup=kb.cancel_lead())
        return
    await state.update_data(number=number)
    await state.set_state(RepairStatus.phone)
    await message.answer("И номер телефона, который вы оставили при сдаче устройства — кнопкой ниже или сообщением.",
                         reply_markup=kb.phone_request())


def crm_text(found: dict) -> str:
    device = f" · {html.escape(found['device'])}" if found["device"] else ""
    return f"🔧 <b>Заказ №{html.escape(found['order_no'])}</b>{device}\n\nСтатус: <b>{html.escape(found['status'])}</b>"


@router.message(StateFilter(RepairStatus.phone), F.text == "Отмена")
async def on_status_cancel(message: Message, state: FSMContext, db: DB, cfg: Config):
    await on_book_cancel(message, state, db, cfg)


@router.message(StateFilter(RepairStatus.phone), F.contact | F.text)
async def on_status_phone(message: Message, state: FSMContext, db: DB, cfg: Config):
    phone = contact_phone(message)
    if len(re.sub(r"\D", "", phone)) < 10:
        await message.answer("Не похоже на номер телефона. Пример: +7 900 123-45-67", reply_markup=kb.phone_request())
        return
    number = (await state.get_data())["number"]
    await state.clear()
    await message.answer("Проверяю…", reply_markup=ReplyKeyboardRemove())
    try:
        found = await order_status(cfg, number, phone)
    except CrmUnavailable as e:
        log.warning("CRM: статус не получен: %s", e)
        found, crm_down = None, True
    else:
        crm_down = False
    if found:
        db.watch_crm(found["order_no"], phone, message.from_user.id, found["status"])
        await message.answer(crm_text(found) + "\n\n🔔 Сообщим сюда, когда статус изменится.",
                             reply_markup=kb.back_home())
        return
    row = db.repair(number)  # статус, поставленный вручную командой /repair
    if row:
        db.watch_repair(number, message.from_user.id)
        await message.answer(status_text(cfg, row) + "\n\n🔔 Сообщим сюда, когда статус изменится.",
                             reply_markup=kb.back_home())
        return
    text = ("Не удалось связаться с системой заказов — попробуйте чуть позже." if crm_down else
            f"Заказ №{html.escape(number)} с этим телефоном не найден. Проверьте номер заказа и телефон.")
    await message.answer(text, reply_markup=buttons([("🔎 Попробовать ещё раз", "rpstatus")],
                                                    [("💬 Задать вопрос", "lead")], [("🏠 В меню", "home")]))


async def crm_status_poller(bot: Bot, db: DB, cfg: Config) -> None:
    """Фоновая задача бота: раз в 20 минут сверяет статусы заказов, за которыми следят клиенты."""
    while True:
        await asyncio.sleep(CRM_POLL)
        for w in db.crm_watches():
            if time.time() - w["created_at"] > CRM_WATCH_DAYS * 86400:
                db.update_crm_watch(w["order_no"], w["user_id"], None)
                continue
            try:
                found = await order_status(cfg, w["order_no"], w["phone"])
            except CrmUnavailable:
                break  # CRM недоступна — попробуем в следующий раз
            except Exception:
                log.exception("CRM: ошибка проверки статуса")
                break
            if not found or found["status"] == w["last_status"]:
                continue
            done = any(word in found["status"].lower() for word in DONE_WORDS)
            try:
                await bot.send_message(w["user_id"], "🔔 Обновился статус ремонта\n\n" + crm_text(found),
                                       reply_markup=kb.back_home())
            except (TelegramBadRequest, TelegramForbiddenError):
                done = True  # клиент заблокировал бота
            db.update_crm_watch(w["order_no"], w["user_id"], None if done else found["status"])
            await asyncio.sleep(1)  # не нагружаем CRM


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
                 "/repair 1234 iPhone 13 | Диагностика — с названием устройства",
                 "<i>Заказы из CRM клиенты видят сами (номер заказа + телефон) — /repair нужен только "
                 "для того, чего нет в CRM.</i>", ""]
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
