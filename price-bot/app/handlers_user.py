import html
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from . import keyboards as kb
from .config import Config
from .db import DB
from .pricing import apply_markup, fmt_price

router = Router(name="user")

MAX_MESSAGE = 3800  # лимит Telegram 4096, оставляем запас под заголовок


class Lead(StatesGroup):
    text = State()


def welcome(cfg: Config) -> str:
    return (
        f"👋 Добро пожаловать в <b>{html.escape(cfg.shop_name)}</b>!\n\n"
        f"{html.escape(cfg.welcome_text)}\n\nВыберите категорию:"
    )


def format_items(db: DB, cfg: Config, cat, section: str) -> list[str]:
    pct, fixed, step = db.markup(cat)
    header = f"<b>{html.escape(cat['title'])}</b>"
    if section:
        header += f"\n<b>{html.escape(section)}</b>"
    if cat["updated_at"]:
        stamp = datetime.fromtimestamp(cat["updated_at"], ZoneInfo(cfg.timezone)).strftime("%d.%m %H:%M")
        header += f"\n<i>Цены обновлены {stamp}</i>"

    lines = []
    for it in db.items(cat["id"], section):
        price = fmt_price(apply_markup(it["base_price"], pct, fixed, step))
        line = f"• {html.escape(it['name'])} — <b>{price}</b>"
        if it["note"]:
            line += f" {html.escape(it['note'])}"
        lines.append(line)

    chunks, current = [], header + "\n"
    for line in lines:
        if len(current) + len(line) + 1 > MAX_MESSAGE:
            chunks.append(current)
            current = ""
        current += "\n" + line
    chunks.append(current)
    return chunks


async def safe_edit(call: CallbackQuery, text: str, markup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            await call.message.answer(text, reply_markup=markup)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db: DB, cfg: Config):
    await state.clear()
    await message.answer(welcome(cfg), reply_markup=kb.main_menu(db.categories()))


@router.callback_query(F.data == "home")
async def cb_home(call: CallbackQuery, state: FSMContext, db: DB, cfg: Config):
    await state.clear()
    await safe_edit(call, welcome(cfg), kb.main_menu(db.categories()))
    await call.answer()


@router.callback_query(F.data == "contacts")
async def cb_contacts(call: CallbackQuery, cfg: Config):
    await safe_edit(call, f"<b>{html.escape(cfg.shop_name)}</b>\n\n{html.escape(cfg.contacts_text)}", kb.back_home())
    await call.answer()


async def show_section(call: CallbackQuery, db: DB, cfg: Config, cat, section: str, back_data: str):
    chunks = format_items(db, cfg, cat, section)
    footer = kb.items_footer(back_data)
    if len(chunks) == 1:
        await safe_edit(call, chunks[0], footer)
        return
    await safe_edit(call, chunks[0], None)
    for i, chunk in enumerate(chunks[1:], start=1):
        await call.message.answer(chunk, reply_markup=footer if i == len(chunks) - 1 else None)


@router.callback_query(F.data.startswith("c:"))
async def cb_category(call: CallbackQuery, db: DB, cfg: Config):
    cat = db.category(int(call.data.split(":")[1]))
    if not cat:
        await call.answer("Категория не найдена", show_alert=True)
        return
    sections = db.sections(cat["id"])
    if not sections:
        await call.answer("Цены скоро появятся", show_alert=True)
        return
    if len(sections) == 1:
        await show_section(call, db, cfg, cat, sections[0], "home")
    else:
        await safe_edit(call, f"<b>{html.escape(cat['title'])}</b>\n\nВыберите раздел:", kb.sections_menu(cat["id"], sections))
    await call.answer()


@router.callback_query(F.data.startswith("s:"))
async def cb_section(call: CallbackQuery, db: DB, cfg: Config):
    _, cat_id, idx = call.data.split(":")
    cat = db.category(int(cat_id))
    sections = db.sections(int(cat_id)) if cat else []
    if not cat or int(idx) >= len(sections):
        await call.answer("Прайс обновился, откройте категорию заново", show_alert=True)
        return
    await show_section(call, db, cfg, cat, sections[int(idx)], f"c:{cat_id}")
    await call.answer()


@router.callback_query(F.data == "lead")
async def cb_lead(call: CallbackQuery, state: FSMContext):
    await state.set_state(Lead.text)
    await call.message.answer(
        "Напишите, что хотите купить (модель, память, цвет) и как с вами связаться — "
        "мы ответим в ближайшее время.",
        reply_markup=kb.cancel_lead(),
    )
    await call.answer()


@router.message(StateFilter(Lead.text), F.text)
async def lead_text(message: Message, state: FSMContext, bot: Bot, db: DB, cfg: Config):
    user = message.from_user
    who = html.escape(user.full_name)
    if user.username:
        who += f" (@{user.username})"
    report = (
        f"📝 <b>Новая заявка</b>\nОт: <a href=\"tg://user?id={user.id}\">{who}</a>\n\n{html.escape(message.text)}"
    )
    for admin_id in cfg.admin_ids:
        try:
            await bot.send_message(admin_id, report)
        except TelegramBadRequest:
            pass
    await state.clear()
    await message.answer("✅ Спасибо! Заявка отправлена, скоро с вами свяжемся.")
    await message.answer(welcome(cfg), reply_markup=kb.main_menu(db.categories()))
