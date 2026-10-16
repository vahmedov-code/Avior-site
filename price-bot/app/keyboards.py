from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

BTN_CONTACTS = InlineKeyboardButton(text="📞 Контакты и доставка", callback_data="contacts")
BTN_QUESTION = InlineKeyboardButton(text="💬 Задать вопрос", callback_data="lead")
BTN_HOME = InlineKeyboardButton(text="🏠 В меню", callback_data="home")


# Иконка категории по словам в названии; первое совпадение выигрывает
CATEGORY_ICONS = (
    ("консол", "🎮"), ("игров", "🕹"), ("iphone", "📱"), ("ipad", "📲"), ("планшет", "📲"),
    ("samsung", "🤖"), ("xiaomi", "🤖"), ("huawei", "🤖"), ("honor", "🤖"), ("pixel", "🤖"),
    ("macbook", "💻"), ("ноутбук", "💻"), ("imac", "🖥"), ("mac mini", "🖥"), ("компьютер", "🖥"),
    ("airpods", "🎧"), ("наушник", "🎧"), ("колонк", "🔊"), ("aw ", "⌚"), ("watch", "⌚"), ("часы", "⌚"),
    ("очки", "🕶"), ("dyson", "🌀"), ("пылесос", "🧹"), ("dji", "🚁"),
    ("гаджет", "🔌"), ("телевизор", "📺"), ("быт", "🏠"),
)


def with_icon(title: str) -> str:
    if title[:1] and not title[0].isalnum():
        return title  # иконка уже есть в названии
    low = title.lower() + " "
    for word, icon in CATEGORY_ICONS:
        if word in low:
            return f"{icon} {title}"
    return title


def cart_button(count: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=f"🛒 Корзина ({count})", callback_data="cart")


def main_menu(categories, cart_count: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in categories:
        if c["n_items"]:
            kb.button(text=with_icon(c["title"]), callback_data=f"c:{c['id']}")
    kb.adjust(2)
    if cart_count:
        kb.row(cart_button(cart_count))
    kb.row(BTN_QUESTION)
    kb.row(BTN_CONTACTS)
    return kb.as_markup()


def sections_menu(cat_id: int, sections: list[str], cart_count: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for idx, title in enumerate(sections):
        kb.button(text=title or "Разное", callback_data=f"s:{cat_id}:{idx}:0")
    kb.adjust(1)
    if cart_count:
        kb.row(cart_button(cart_count))
    kb.row(BTN_HOME)
    return kb.as_markup()


def section_page(cat_id: int, sec_idx: int, page: int, numbered: list[tuple[int, int]], has_prev: bool,
                 has_next: bool, back_data: str, cart_count: int) -> InlineKeyboardMarkup:
    """numbered — пары (номер в списке, id позиции) для кнопок «добавить в корзину»."""
    kb = InlineKeyboardBuilder()
    for no, item_id in numbered:
        kb.button(text=f"➕{no}", callback_data=f"a:{item_id}:{page}")
    kb.adjust(5)
    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"s:{cat_id}:{sec_idx}:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"s:{cat_id}:{sec_idx}:{page + 1}"))
    if nav:
        kb.row(*nav)
    if cart_count:
        kb.row(cart_button(cart_count))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_data), BTN_HOME)
    return kb.as_markup()


def cart_menu(lines) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for no, line in enumerate(lines, start=1):
        kb.button(text=f"❌{no}", callback_data=f"rm:{line.cart_id}")
    kb.adjust(5)
    if any(line.available for line in lines):
        kb.row(InlineKeyboardButton(text="✅ Оформить заказ", callback_data="checkout"))
    kb.row(InlineKeyboardButton(text="🗑 Очистить", callback_data="cartclear"), BTN_HOME)
    return kb.as_markup()


def delivery_menu(options: tuple[str, ...]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for idx, title in enumerate(options):
        kb.button(text=title, callback_data=f"d:{idx}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ В корзину", callback_data="cart"))
    return kb.as_markup()


def phone_request() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)], [KeyboardButton(text="Отмена")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def back_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[BTN_HOME]])


def cancel_lead() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖ Отмена", callback_data="home")]])


def order_actions(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"o:ok:{order_id}"),
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"o:no:{order_id}"),
        ]]
    )


def choose_category(categories, guess_id: int | None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    ordered = sorted(categories, key=lambda c: c["id"] != guess_id)
    for c in ordered:
        mark = "✅ " if c["id"] == guess_id else ""
        kb.button(text=f"{mark}{c['title']}", callback_data=f"imp:{c['id']}")
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="➕ Новая категория", callback_data="impnew"))
    kb.row(InlineKeyboardButton(text="✖ Отмена", callback_data="impcancel"))
    return kb.as_markup()
