"""Ответ клиенту от имени бота: кнопка «✍️ Ответить» под вопросом или заявкой.
Клиент получает сообщение от AVIOR Shop — личный аккаунт владельца не светится."""
import html

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import sources
from .db import DB
from .handlers_admin import IsAdmin

admin_router = Router(name="reply_admin")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())

CLIENT_MARKUP = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="💬 Ответить", callback_data="lead")],
    [InlineKeyboardButton(text="🏠 В меню", callback_data="home")],
])


class Answer(StatesGroup):
    text = State()


def reply_button(user_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(text="✍️ Ответить клиенту", callback_data=f"ans:{user_id}")


def reply_markup(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[reply_button(user_id)]])


@admin_router.callback_query(F.data.startswith("ans:"))
async def cb_answer(call: CallbackQuery, state: FSMContext):
    await state.set_state(Answer.text)
    await state.update_data(user_id=int(call.data[4:]))
    await call.message.answer("✍️ Напишите ответ — клиент получит его от имени бота AVIOR Shop.\n"
                              "Можно текст или фото с подписью.",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                  InlineKeyboardButton(text="✖ Отмена", callback_data="ansx")]]))
    await call.answer()


@admin_router.callback_query(F.data == "ansx")
async def cb_answer_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Ответ не отправлен.")
    await call.answer()


@admin_router.message(StateFilter(Answer.text), ~F.text.startswith("/"))
async def on_answer(message: Message, state: FSMContext, bot: Bot):
    user_id = (await state.get_data())["user_id"]
    await state.clear()
    try:
        if message.text:
            await bot.send_message(user_id, f"💬 <b>Ответ AVIOR</b>\n\n{html.escape(message.text)}",
                                   reply_markup=CLIENT_MARKUP)
        else:  # фото, голосовое, файл — пересылаем как есть, без «переслано от»
            await bot.copy_message(user_id, message.chat.id, message.message_id, reply_markup=CLIENT_MARKUP)
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.answer("⚠️ Не доставлено: клиент заблокировал бота или ещё ни разу ему не писал.")
        return
    await message.answer("✅ Ответ отправлен. Если клиент ответит, его сообщение придёт сюда.",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                             InlineKeyboardButton(text="✍️ Написать ещё", callback_data=f"ans:{user_id}")]]))


# --- ссылки с метками ---

@admin_router.message(Command("links"))
async def cmd_links(message: Message, command: CommandObject, bot: Bot, db: DB):
    username = (await bot.me()).username
    custom = sources.clean((command.args or "").strip())
    if custom:  # /links instagram — своя метка
        await send_qr(message, username, custom)
        return
    lines = ["🔗 <b>Ссылки на бота с метками</b>",
             "Ставьте нужную ссылку в каждом месте — в /stats будет видно, откуда пришли клиенты.", ""]
    for code, title in sources.SOURCES.items():
        lines.append(f"{title}\n<code>{sources.link(username, code)}</code>")
    lines += ["", "Своя метка: <code>/links instagram</code> — ссылка и QR-код с меткой «instagram».",
              "Б/у из канала метятся автоматически. QR-коды для печати — кнопками ниже."]
    rows, row = [], []
    for code, title in sources.SOURCES.items():
        row.append(InlineKeyboardButton(text=f"QR: {title.split(' ', 1)[1][:18]}", callback_data=f"qr:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def send_qr(message: Message, username: str, code: str) -> None:
    url = sources.link(username, code)
    await message.answer_photo(BufferedInputFile(sources.qr_png(url), filename=f"avior-{code}.png"),
                               caption=f"{html.escape(sources.label(code))}\n<code>{url}</code>\n\n"
                                       "Распечатайте или вставьте в макет — клиенты, пришедшие по этому коду, "
                                       "будут видны в /stats.")


@admin_router.callback_query(F.data.startswith("qr:"))
async def cb_qr(call: CallbackQuery, bot: Bot):
    code = sources.clean(call.data[3:])
    if code:
        await send_qr(call.message, (await bot.me()).username, code)
    await call.answer()
