"""Напоминания о профилактике: через несколько месяцев после выдачи ремонта клиенту приходит
«пора почистить / заменить термопасту — запишитесь».

Бот не угадывает, что делали с устройством: когда ремонт выдан (статус из CRM или /repair), владелец
получает вопрос «Напомнить клиенту о профилактике?» и одной кнопкой выбирает срок или «Не нужно».
Клиент получает напоминание в своём мессенджере (Telegram, MAX, WhatsApp) днём, а не ночью."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import outbox
from .config import Config
from .db import DB
from .handlers_admin import IsAdmin
from .handlers_user import notify_admins, person

log = logging.getLogger(__name__)
admin_router = Router(name="reminders_admin")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())

CHECK_EVERY = 30 * 60      # секунд между проверками
SEND_HOURS = range(11, 20)  # отправляем клиентам с 11:00 до 20:00 по Москве
CLIENT_MARKUP = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📝 Записаться на ремонт", callback_data="rpnew")],
    [InlineKeyboardButton(text="🏠 В меню", callback_data="home")],
])


def client_name(db: DB, user_id: int) -> str:
    v = db.visitor(user_id)
    return person(user_id, v["full_name"] if v else str(user_id), v["username"] if v else None)


async def offer(bot: Bot, db: DB, cfg: Config, user_id: int, order_no: str, device: str) -> None:
    """Ремонт выдан — спрашиваем владельца, напомнить ли клиенту о профилактике."""
    rid = db.add_reminder_draft(user_id, order_no, device)
    what = f" · {html.escape(device)}" if device else ""
    await notify_admins(bot, cfg, f"📦 Заказ №{html.escape(order_no)}{what} выдан — клиент {client_name(db, user_id)}.\n"
                                  "Напомнить клиенту о профилактике (чистка, термопаста)?",
                        InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text=f"{m} мес", callback_data=f"rmd:{rid}:{m}") for m in (3, 6, 12)],
                            [InlineKeyboardButton(text="Не нужно", callback_data=f"rmd:{rid}:0")]]))


def reminder_text(row) -> str:
    device = f": {html.escape(row['device'])}" if row["device"] else ""
    return (f"🔧 Здравствуйте! {row['months']} мес. назад вы забирали технику из ремонта в AVIOR{device}.\n\n"
            "Чтобы техника не перегревалась и служила дольше, советуем профилактику: чистку от пыли и замену "
            "термопасты — особенно для ноутбуков и игровых приставок.\n\n"
            "Записаться можно прямо здесь 👇")


@admin_router.callback_query(F.data.startswith("rmd:"))
async def cb_plan(call: CallbackQuery, db: DB, cfg: Config):
    _, rid, months = call.data.split(":")
    row = db.reminder(int(rid))
    if not row:
        await call.answer()
        return
    if months == "0":
        db.set_reminder_status(row["id"], "skip")
        await call.message.edit_text(call.message.html_text + "\n\n— напоминание не нужно", reply_markup=None)
    else:
        due = db.plan_reminder(row["id"], int(months))
        day = datetime.fromtimestamp(due, ZoneInfo(cfg.timezone)).strftime("%d.%m.%Y")
        await call.message.edit_text(call.message.html_text + f"\n\n⏰ Напомню клиенту {day}", reply_markup=None)
    await call.answer()


@admin_router.message(Command("remind"))
async def cmd_remind(message: Message, db: DB, cfg: Config):
    rows = db.planned_reminders()
    if not rows:
        await message.answer("⏰ Запланированных напоминаний о профилактике нет.\n\n"
                             "Они появляются сами: когда ремонт выдан, бот спросит, напомнить ли клиенту.")
        return
    tz = ZoneInfo(cfg.timezone)
    lines = [f"⏰ <b>Напоминания о профилактике</b>: {len(rows)}", ""]
    buttons = []
    for r in rows[:30]:
        day = datetime.fromtimestamp(r["due_at"], tz).strftime("%d.%m.%Y")
        lines.append(f"{day} · №{html.escape(r['order_no'])} {html.escape(r['device'])} · {client_name(db, r['user_id'])}")
        buttons.append([InlineKeyboardButton(text=f"✖ Отменить {day} №{r['order_no']}"[:60],
                                             callback_data=f"rmx:{r['id']}")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@admin_router.callback_query(F.data.startswith("rmx:"))
async def cb_cancel(call: CallbackQuery, db: DB):
    db.set_reminder_status(int(call.data[4:]), "skip")
    await call.answer("Напоминание отменено")
    rows = call.message.reply_markup.inline_keyboard if call.message.reply_markup else []
    keep = [r for r in rows if r[0].callback_data != call.data]
    await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=keep) if keep else None)


async def send_due(bot: Bot, db: DB, cfg: Config) -> int:
    """Отправляет наступившие напоминания (только днём). Возвращает, сколько ушло."""
    if datetime.now(ZoneInfo(cfg.timezone)).hour not in SEND_HOURS:
        return 0
    sent = 0
    for r in db.due_reminders(int(time.time())):
        try:
            await outbox.send(bot, cfg, r["user_id"], reminder_text(r), CLIENT_MARKUP)
        except outbox.DeliveryError as e:
            db.set_reminder_status(r["id"], "failed")
            await notify_admins(bot, cfg, f"⚠️ Напоминание о профилактике не доставлено: №{html.escape(r['order_no'])} "
                                          f"· {client_name(db, r['user_id'])}\n<i>{html.escape(str(e))[:200]}</i>")
            continue
        db.set_reminder_status(r["id"], "sent")
        sent += 1
        await notify_admins(bot, cfg, f"⏰ Напоминание о профилактике отправлено: №{html.escape(r['order_no'])} "
                                      f"· {client_name(db, r['user_id'])}")
        await asyncio.sleep(0.5)
    return sent


async def reminder_loop(bot: Bot, db: DB, cfg: Config) -> None:
    while True:
        try:
            await send_due(bot, db, cfg)
        except Exception:
            log.exception("Напоминания: ошибка отправки")
        await asyncio.sleep(CHECK_EVERY)
