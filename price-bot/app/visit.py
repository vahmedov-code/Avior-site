"""Выбор дня и интервала визита при записи на ремонт: «четверг 12 ноября с 12:00 до 16:00».

Общий для ботов Telegram, MAX и WhatsApp. Интервалы — каждый день; сегодняшний интервал
предлагается, только пока он не начался."""
from __future__ import annotations

import html
from datetime import date, datetime, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

SLOTS = (("12:00", "16:00"), ("16:00", "20:00"))
DAYS_AHEAD = 7  # сколько дней показывать на выбор
PHONE_CHOICE = "call"  # «другое время или срочно» — согласуем по телефону
PHONE_BUTTON = "📞 Другое время / срочно"
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря")


def free_slots(day: date, now: datetime) -> list[int]:
    """Номера интервалов, на которые ещё можно записаться в этот день."""
    if day < now.date():
        return []
    if day == now.date():
        return [i for i, (start, _) in enumerate(SLOTS) if now.hour < int(start[:2])]
    return list(range(len(SLOTS)))


def days(now: datetime) -> list[tuple[str, str]]:
    """Ближайшие дни со свободными интервалами: [(код YYYYMMDD, «Завтра, чт 12 ноября»)]."""
    out, d = [], now.date()
    while len(out) < DAYS_AHEAD:
        if free_slots(d, now):
            prefix = {0: "Сегодня, ", 1: "Завтра, "}.get((d - now.date()).days, "")
            out.append((d.strftime("%Y%m%d"), f"{prefix}{SHORT[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"))
        d += timedelta(days=1)
    return out


def parse(code: str) -> date | None:
    try:
        return datetime.strptime(code, "%Y%m%d").date()
    except ValueError:
        return None


def describe(code: str, slot: int | None) -> str:
    """«четверг 12 ноября с 12:00 до 16:00»; без дня — «время согласовать по телефону»."""
    d = parse(code) if code != PHONE_CHOICE else None
    if d is None or slot is None or not 0 <= slot < len(SLOTS):
        return "другое время — согласовать по телефону"
    start, end = SLOTS[slot]
    return f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]} с {start} до {end}"


def days_markup(now: datetime) -> InlineKeyboardMarkup:
    rows, row = [], []
    for code, label in days(now):
        row.append(InlineKeyboardButton(text=label, callback_data=f"rpd:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text=PHONE_BUTTON, callback_data=f"rpd:{PHONE_CHOICE}")])
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def slots_markup(code: str, now: datetime) -> InlineKeyboardMarkup | None:
    """Интервалы выбранного дня; None — день уже недоступен (пока клиент думал, интервал начался)."""
    d = parse(code)
    free = free_slots(d, now) if d else []
    if not free:
        return None
    rows = [[InlineKeyboardButton(text=f"🕐 {SLOTS[i][0]}–{SLOTS[i][1]}", callback_data=f"rps:{code}:{i}")]
            for i in free]
    rows.append([InlineKeyboardButton(text=PHONE_BUTTON, callback_data=f"rpd:{PHONE_CHOICE}")])
    rows.append([InlineKeyboardButton(text="⬅️ Другой день", callback_data="rpdays")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def day_title(code: str) -> str:
    d = parse(code)
    return f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}" if d else ""


DAYS_TEXT = ("<b>Последний шаг.</b> Когда вам удобно приехать? Выберите день.\n"
             "Не подходит или нужно срочно — «📞 Другое время / срочно».")


def confirmation(when: str, phone: str) -> str:
    """Ответ клиенту после записи — с номером для срочной связи (в мессенджерах он нажимается)."""
    lines = ["✅ <b>Заявка принята!</b>", f"🗓 {html.escape(when)}", ""]
    if when.startswith("другое время"):
        lines.append(f"Мастер перезвонит и согласует время. Если срочно — звоните сразу: <b>{html.escape(phone)}</b>")
    else:
        lines += ["Мастер перезвонит, подскажет цену и сроки.", f"📞 Если срочно: <b>{html.escape(phone)}</b>"]
    lines.append("📍 Москва, Можайское ш., 4к1")
    return "\n".join(lines)
