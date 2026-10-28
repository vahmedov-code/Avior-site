import asyncio
import html
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, ErrorEvent

from . import (handlers_admin, handlers_news, handlers_repair, handlers_search, handlers_tradein, handlers_used,
               handlers_user)
from .config import Config
from .db import DB
from .stats import VisitMiddleware


ADMIN_COMMANDS = [
    BotCommand(command=c, description=d) for c, d in (
        ("start", "Каталог и цены (как видят клиенты)"),
        ("stats", "Статистика посещений и заказов"),
        ("bu", "Б/у: добавить, список, продано"),
        ("orders", "Последние заказы"),
        ("repair", "Статус ремонта: /repair 1234"),
        ("send", "Рассылка клиентам"),
        ("channel", "Канал для б/у"),
        ("excel", "Прайс с вашими ценами в Excel"),
        ("cats", "Категории, наценки, номера"),
        ("markup", "Общая наценка: /markup 0 10000"),
        ("catmarkup", "Наценка категории: /catmarkup 3 0 5000"),
        ("round", "Округление цен: /round 100"),
        ("exclude", "Что не продаём (слова-исключения)"),
        ("skipcat", "Категории поставщика, которые не грузим"),
        ("mergecat", "Объединение категорий поставщика"),
        ("onlycat", "Из категории — только один бренд"),
        ("rename", "Переименовать категорию"),
        ("move", "Порядок категорий в меню"),
        ("newcat", "Создать категорию"),
        ("delcat", "Удалить категорию"),
        ("admin", "Справка по всем командам"),
    )
]


async def set_menus(bot: Bot, cfg: Config, attempts: int = 10) -> None:
    """Клиентам в меню — только /start, владельцу — все команды управления."""
    for attempt in range(attempts):
        try:
            await bot.set_my_commands([BotCommand(command="start", description="Каталог и цены")])
            for admin_id in cfg.admin_ids:
                try:
                    await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
                except TelegramBadRequest:
                    # владелец ещё ни разу не писал боту — меню появится после следующего перезапуска
                    logging.warning("Не удалось поставить меню команд для %s", admin_id)
            return
        except Exception as e:  # сеть или временный сбой Telegram
            logging.warning("Меню команд не поставлено (%s), попытка %s из %s", e, attempt + 1, attempts)
            await asyncio.sleep(60)


def error_reporter(bot: Bot, cfg: Config):
    async def on_error(event: ErrorEvent) -> None:
        """Сбой в обработчике: в журнал и владельцу в Telegram — иначе клиент видит, что бот «молчит»."""
        logging.error("Ошибка при обработке обновления", exc_info=event.exception)
        upd = event.update
        what = (upd.message and (upd.message.text or "фото/файл")) or (upd.callback_query and upd.callback_query.data) or ""
        text = (f"⚠️ <b>Ошибка в боте</b>\n{html.escape(type(event.exception).__name__)}: "
                f"{html.escape(str(event.exception))[:700]}\n\nНа чём: <code>{html.escape(str(what))[:100]}</code>")
        for admin_id in cfg.admin_ids:
            try:
                await bot.send_message(admin_id, text)
            except Exception:
                pass
    return on_error


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    db = DB(cfg.db_path)
    db.init_defaults(
        {
            "markup_pct": cfg.default_markup_pct,
            "markup_fixed": cfg.default_markup_fixed,
            "round_step": cfg.default_round_step,
        }
    )

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher(storage=MemoryStorage(), db=db, cfg=cfg)
    # Админский роутер первым: пересланный владельцем прайс должен уйти в импорт.
    # Мастер б/у раньше импорта: фото-файлы в нём не должны уйти в загрузку прайса.
    # Поиск последним: он отвечает на любой текст, который не забрали мастера выше.
    dp.include_routers(
        handlers_used.admin_router, handlers_tradein.admin_router, handlers_repair.admin_router,
        handlers_news.admin_router, handlers_admin.router,
        handlers_used.router, handlers_tradein.router, handlers_repair.router, handlers_news.router,
        handlers_user.router, handlers_search.router,
    )
    dp.errors.register(error_reporter(bot, cfg))
    dp.message.outer_middleware(VisitMiddleware())
    dp.callback_query.outer_middleware(VisitMiddleware())

    # Меню команд ставим в фоне: временный сбой Telegram («Bad Gateway») не должен ронять бота
    menus = asyncio.create_task(set_menus(bot, cfg))  # ссылку держим, чтобы задачу не собрал сборщик мусора
    crm = asyncio.create_task(handlers_repair.crm_status_poller(bot, db, cfg))
    await dp.start_polling(bot)
    menus.cancel()
    crm.cancel()


if __name__ == "__main__":
    asyncio.run(main())
