import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat

from . import handlers_admin, handlers_user
from .config import Config
from .db import DB
from .stats import VisitMiddleware


ADMIN_COMMANDS = [
    BotCommand(command=c, description=d) for c, d in (
        ("start", "Каталог и цены (как видят клиенты)"),
        ("stats", "Статистика посещений и заказов"),
        ("orders", "Последние заказы"),
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
    dp.include_routers(handlers_admin.router, handlers_user.router)
    dp.message.outer_middleware(VisitMiddleware())
    dp.callback_query.outer_middleware(VisitMiddleware())

    # Клиентам в меню — только /start, владельцу — все команды управления
    await bot.set_my_commands([BotCommand(command="start", description="Каталог и цены")])
    for admin_id in cfg.admin_ids:
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            # владелец ещё ни разу не писал боту — меню появится после следующего перезапуска
            logging.warning("Не удалось поставить меню команд для %s", admin_id)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
