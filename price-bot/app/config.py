import os
from dataclasses import dataclass

from dotenv import load_dotenv


DEFAULT_CONTACTS = (
    "📍 Москва, Можайское шоссе, 4к1\n"
    "📞 +7 (901) 222-81-11\n"
    "💬 Telegram: @ghost0590\n\n"
    "🚚 Доставка:\n"
    "• курьером по Москве\n"
    "• СДЭК\n"
    "• Ozon доставка"
)
DEFAULT_DELIVERY = "🚚 Курьер по Москве,📦 СДЭК,📦 Ozon доставка,🏬 Самовывоз (Можайское ш. 4к1)"


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

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("BOT_TOKEN не задан: скопируйте .env.example в .env и впишите токен от @BotFather")
        admins = frozenset(int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x)
        if not admins:
            raise SystemExit("ADMIN_IDS не задан: впишите свой Telegram ID (узнать: @userinfobot)")
        return cls(
            bot_token=token,
            admin_ids=admins,
            shop_name=os.getenv("SHOP_NAME", "AVIOR Shop"),
            welcome_text=_text("WELCOME_TEXT", "Оригинальная техника Apple и не только. Актуальные цены, гарантия. Доставка курьером по Москве, СДЭК и Ozon."),
            contacts_text=_text("CONTACTS_TEXT", DEFAULT_CONTACTS),
            db_path=os.getenv("DB_PATH", "data/shop.db"),
            default_markup_pct=float(os.getenv("MARKUP_PCT", "0")),
            default_markup_fixed=int(os.getenv("MARKUP_FIXED", "0")),
            default_round_step=int(os.getenv("ROUND_STEP", "100")),
            block_words=tuple(w.strip() for w in os.getenv("BLOCK_WORDS", "bigsale").split(",") if w.strip()),
            timezone=os.getenv("TZ_NAME", "Europe/Moscow"),
            delivery_options=tuple(
                o.strip() for o in os.getenv("DELIVERY_OPTIONS", DEFAULT_DELIVERY).split(",") if o.strip()
            ),
        )
