"""Ответ клиенту от имени бота: кнопка «✍️ Ответить» под вопросом или заявкой.
Клиент получает сообщение от AVIOR Shop — личный аккаунт владельца не светится."""
import html

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import outbox, sources
from .config import Config
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
    await call.message.answer("✍️ Напишите ответ — клиент получит его от имени бота AVIOR.\n"
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
async def on_answer(message: Message, state: FSMContext, bot: Bot, cfg: Config):
    user_id = (await state.get_data())["user_id"]
    await state.clear()
    try:
        if message.text:
            await outbox.send(bot, cfg, user_id, f"💬 <b>Ответ AVIOR</b>\n\n{html.escape(message.text)}",
                              CLIENT_MARKUP)
        else:  # фото, голосовое, файл — пересылаем как есть, без «переслано от»
            await outbox.copy(bot, cfg, user_id, message, CLIENT_MARKUP)
    except outbox.DeliveryError as e:
        await message.answer("⚠️ Не доставлено: клиент заблокировал бота или ещё ни разу ему не писал.\n"
                             f"<i>{html.escape(str(e))[:200]}</i>")
        return
    await message.answer("✅ Ответ отправлен. Если клиент ответит, его сообщение придёт сюда.",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                             InlineKeyboardButton(text="✍️ Написать ещё", callback_data=f"ans:{user_id}")]]))


# --- ссылки с метками ---

@admin_router.message(Command("links"))
async def cmd_links(message: Message, command: CommandObject, bot: Bot, db: DB, cfg: Config):
    main = (await bot.me()).username
    short = cfg.link_username or main  # красивый адрес — для соцсетей, где ссылку легко поправить
    custom = sources.clean((command.args or "").strip())
    if custom:  # /links ozon — своя метка
        await send_qr(message, main, short, custom)
        return
    lines = ["🔗 <b>Ссылки на бота с метками</b>",
             "Ставьте нужную ссылку в каждом месте — в /stats будет видно, откуда пришли клиенты.", ""]
    for code, title in sources.SOURCES.items():
        lines.append(f"{title}\n<code>{sources.link(short, code)}</code>")
    lines += ["", "Своя метка: <code>/links ozon</code> — ссылка и QR-код с любой меткой латиницей.",
              "Б/у из канала метятся автоматически. QR-коды для печати — кнопками ниже."]
    if short != main:
        lines.append(f"\n<i>Ссылки через @{short} работают, пока активна подписка Telegram Premium. "
                     f"QR-коды для печати поэтому ведут на постоянный адрес @{main}.</i>")
    max_name = await max_username(cfg)
    if max_name:
        lines.append(f"\n🔵 <b>Бот в MAX</b>: <code>https://max.ru/{max_name}</code>\n"
                     f"Метки те же, например <code>https://max.ru/{max_name}?start=vk</code> — "
                     "в /stats переходы из обоих мессенджеров считаются вместе.")
    rows, row = [], []
    for code, title in sources.SOURCES.items():
        row.append(InlineKeyboardButton(text=f"QR: {title.split(' ', 1)[1][:18]}", callback_data=f"qr:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


_max_name: dict[str, str] = {}


async def max_username(cfg: Config) -> str | None:
    """Ник бота в MAX (один раз спрашиваем у API); None — MAX не подключён или недоступен."""
    if not cfg.max_token:
        return None
    if cfg.max_token not in _max_name:
        try:
            _max_name[cfg.max_token] = (await outbox.max_api(cfg).me()).get("username") or ""
        except Exception:
            return None
    return _max_name[cfg.max_token] or None


async def send_qr(message: Message, main: str, short: str, code: str) -> None:
    """QR — всегда на постоянный адрес бота: напечатанное уже не поправить, если Premium-адрес отключится."""
    url = sources.link(main, code)
    extra = f"\nДля соцсетей: <code>{sources.link(short, code)}</code>" if short != main else ""
    await message.answer_photo(BufferedInputFile(sources.qr_png(url), filename=f"avior-{code}.png"),
                               caption=f"{html.escape(sources.label(code))}\n<code>{url}</code>{extra}\n\n"
                                       "Распечатайте или вставьте в макет — клиенты, пришедшие по этому коду, "
                                       "будут видны в /stats.")


@admin_router.callback_query(F.data.startswith("qr:"))
async def cb_qr(call: CallbackQuery, bot: Bot, cfg: Config):
    code = sources.clean(call.data[3:])
    if code:
        main = (await bot.me()).username
        await send_qr(call.message, main, cfg.link_username or main, code)
    await call.answer()
