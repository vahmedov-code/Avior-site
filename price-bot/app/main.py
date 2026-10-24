import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from . import handlers_admin, handlers_user
from .config import Config
from .db import DB
from .stats import VisitMiddleware


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

    await bot.set_my_commands([BotCommand(command="start", description="Каталог и цены")])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
