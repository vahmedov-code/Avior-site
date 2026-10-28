"""Рассылка владельца всем, кто заходил в бота, и отписки клиентов."""
import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .db import DB
from .handlers_admin import IsAdmin
from .handlers_used import buttons

router = Router(name="news")
admin_router = Router(name="news_admin")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())
log = logging.getLogger(__name__)

NEWS_MARKUP = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🛍 Открыть каталог", callback_data="catalog")],
    [InlineKeyboardButton(text="🔕 Не присылать новости", callback_data="newsoff")],
])
_tasks: set = set()  # ссылки на фоновые рассылки, чтобы их не собрал сборщик мусора


class Broadcast(StatesGroup):
    content = State()
    confirm = State()


@admin_router.message(Command("send"))
async def cmd_send(message: Message, state: FSMContext, db: DB):
    await state.set_state(Broadcast.content)
    await message.answer(f"📢 <b>Рассылка</b>: получат {len(db.news_recipients())} человек — все, кто заходил в бота "
                         "и не отписался.\n\nПришлите сообщение: текст или фото с подписью. "
                         "Перед отправкой покажу, как оно будет выглядеть.",
                         reply_markup=buttons([("✖ Отмена", "bcno")]))


@admin_router.message(StateFilter(Broadcast.content), ~F.text.startswith("/"))
async def on_content(message: Message, state: FSMContext, bot: Bot, db: DB):
    await state.update_data(chat_id=message.chat.id, message_id=message.message_id)
    await state.set_state(Broadcast.confirm)
    await message.answer("👇 Так увидят клиенты:")
    await bot.copy_message(message.chat.id, message.chat.id, message.message_id, reply_markup=NEWS_MARKUP)
    n = len(db.news_recipients())
    await message.answer(f"Отправить {n} получателям?",
                         reply_markup=buttons([(f"✅ Отправить ({n})", "bcgo")], [("✖ Отмена", "bcno")]))


@admin_router.callback_query(F.data == "bcno")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Рассылка отменена.")
    await call.answer()


async def deliver(bot: Bot, db: DB, admin_chat: int, chat_id: int, message_id: int) -> None:
    ok = blocked = 0
    for user_id in db.news_recipients():
        try:
            await bot.copy_message(user_id, chat_id, message_id, reply_markup=NEWS_MARKUP)
            ok += 1
        except TelegramRetryAfter as e:  # Telegram просит притормозить
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.copy_message(user_id, chat_id, message_id, reply_markup=NEWS_MARKUP)
                ok += 1
            except Exception:
                blocked += 1
        except (TelegramBadRequest, TelegramForbiddenError):
            blocked += 1
            db.set_news_off(user_id)  # заблокировал бота — больше не пытаемся
        except Exception:
            log.exception("Рассылка: не удалось отправить %s", user_id)
            blocked += 1
        await asyncio.sleep(0.05)  # не больше ~20 сообщений в секунду
    await bot.send_message(admin_chat, f"📢 Рассылка завершена: доставлено {ok}"
                                       + (f", не доставлено {blocked} (заблокировали бота)" if blocked else ""))


@admin_router.callback_query(StateFilter(Broadcast.confirm), F.data == "bcgo")
async def cb_go(call: CallbackQuery, state: FSMContext, bot: Bot, db: DB):
    d = await state.get_data()
    await state.clear()
    await call.message.edit_text("📢 Рассылка началась — по окончании пришлю отчёт.")
    task = asyncio.create_task(deliver(bot, db, call.message.chat.id, d["chat_id"], d["message_id"]))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    await call.answer()


# --- отписки клиентов ---

@router.callback_query(F.data == "newsoff")
async def cb_news_off(call: CallbackQuery, db: DB):
    db.set_news_off(call.from_user.id)
    await call.message.edit_reply_markup(reply_markup=buttons([("🛍 Открыть каталог", "catalog")]))
    await call.answer("Больше не будем присылать новости", show_alert=True)


@router.callback_query(F.data == "alertsoff")
async def cb_alerts_off(call: CallbackQuery, db: DB):
    db.set_alerts_off(call.from_user.id)
    await call.message.edit_reply_markup(reply_markup=buttons([("🛒 Открыть корзину", "cart")]))
    await call.answer("Больше не будем сообщать о ценах", show_alert=True)

