import os
from dataclasses import dataclass

from dotenv import load_dotenv

from .rules import DEFAULT_ACCESSORY_WORDS, DEFAULT_EXCLUDE_WORDS


DEFAULT_CONTACTS = (
    "📍 Москва, Можайское шоссе, 4к1\n"
    "📞 +7 (901) 222-81-11\n"
    "💬 Telegram: @ghost0590\n\n"
    "🚚 Доставка:\n"
    "• курьером по Москве\n"
    "• СДЭК\n"
    "• Ozon доставка"
)
DEFAULT_USED_NOTE = ("Устройства приняты сервисом AVIOR в трейд-ин и проверены нашими мастерами. "
                     "Посмотреть и проверить можно перед покупкой: Москва, Можайское ш., 4к1.\n"
                     "🛡 Гарантия 14 дней на проверку устройства.")
DEFAULT_SERVICES = ("🛡 Наклейка защитного стекла,🔄 Перенос данных со старого устройства,"
                    "⚙️ Настройка и активация")
DEFAULT_DELIVERY = "🚚 Курьер по Москве,📦 СДЭК,📦 Ozon доставка,🏬 Самовывоз (Можайское ш. 4к1)"


def _words(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return default
    return tuple(w.strip().lower() for w in raw.split(",") if w.strip())


def _text(name: str, default: str) -> str:
    # В .env переносы строк пишутся как \n
    return os.getenv(name, default).replace("\\n", "\n")


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    shop_name: str
    welcome_text: str
    contacts_text: str
    db_path: str
    default_markup_pct: float
    default_markup_fixed: int
    default_round_step: int
    block_words: tuple[str, ...]
    timezone: str
    delivery_options: tuple[str, ...] = ()
    accessory_markup_fixed: int = 5000
    accessory_words: tuple[str, ...] = DEFAULT_ACCESSORY_WORDS
    default_exclude_words: tuple[str, ...] = DEFAULT_EXCLUDE_WORDS
    default_skip_categories: tuple[str, ...] = ("Телевизоры", "Быт.Техника")
    default_merge_categories: tuple[tuple[str, str], ...] = (("Игровые Ноутбуки", "Ноутбуки"), ("Пылесосы", "Dyson"))
    default_only_words: tuple[tuple[str, str], ...] = (("Пылесосы", "dyson"),)
    used_note: str = DEFAULT_USED_NOTE
    services: tuple[str, ...] = tuple(DEFAULT_SERVICES.split(","))
    crm_api_url: str = "https://cms.avior.moscow/api"
    crm_lead_secret: str = ""

    @classmethod
    def from_env(cls, for_bot: bool = True) -> "Config":
        """for_bot=False — для скрипта manage.py, которому токен не нужен."""
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if for_bot and not token:
            raise SystemExit("BOT_TOKEN не задан: скопируйте .env.example в .env и впишите токен от @BotFather")
        admins = frozenset(int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x)
        if for_bot and not admins:
            raise SystemExit("ADMIN_IDS не задан: впишите свой Telegram ID (узнать: @userinfobot)")
        return cls(
            bot_token=token,
            admin_ids=admins,
            shop_name=os.getenv("SHOP_NAME", "AVIOR Shop"),
            welcome_text=_text("WELCOME_TEXT", "Оригинальная техника Apple и не только.\nАктуальные цены, гарантия. Доставка курьером по Москве, СДЭК и Ozon.\n\n"
                                 "🕚 Цены обновляются каждый день с 11:00 до 12:00 (МСК)."),
            contacts_text=_text("CONTACTS_TEXT", DEFAULT_CONTACTS),
            db_path=os.getenv("DB_PATH", "data/shop.db"),
            default_markup_pct=float(os.getenv("MARKUP_PCT", "0")),
            default_markup_fixed=int(os.getenv("MARKUP_FIXED", "10000")),
            default_round_step=int(os.getenv("ROUND_STEP", "100")),
            block_words=tuple(w.strip() for w in os.getenv("BLOCK_WORDS", "bigsale").split(",") if w.strip()),
            timezone=os.getenv("TZ_NAME", "Europe/Moscow"),
            delivery_options=tuple(
                o.strip() for o in os.getenv("DELIVERY_OPTIONS", DEFAULT_DELIVERY).split(",") if o.strip()
            ),
            used_note=_text("USED_NOTE", DEFAULT_USED_NOTE),
            crm_api_url=os.getenv("CRM_API_URL", "https://cms.avior.moscow/api").rstrip("/"),
            crm_lead_secret=os.getenv("CRM_LEAD_SECRET", "").strip(),
            services=tuple(x.strip() for x in os.getenv("SERVICES", DEFAULT_SERVICES).split(",") if x.strip()),
            accessory_markup_fixed=int(os.getenv("ACCESSORY_MARKUP", "5000")),
            accessory_words=_words("ACCESSORY_WORDS", DEFAULT_ACCESSORY_WORDS),
            default_exclude_words=_words("EXCLUDE_WORDS", DEFAULT_EXCLUDE_WORDS),
            default_merge_categories=tuple(
                tuple(p.split("=", 1)) for p in os.getenv("MERGE_CATEGORIES", "Игровые Ноутбуки=Ноутбуки,Пылесосы=Dyson").split(",") if "=" in p
            ),
            default_only_words=tuple(
                tuple(p.split("=", 1)) for p in os.getenv("ONLY_WORDS", "Пылесосы=dyson").split(",") if "=" in p
            ),
            default_skip_categories=tuple(
                c.strip() for c in os.getenv("SKIP_CATEGORIES", "Телевизоры,Быт.Техника").split(",") if c.strip()
            ),
        )
