from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

BTN_CONTACTS = InlineKeyboardButton(text="📞 Контакты", callback_data="contacts")
BTN_LEAD = InlineKeyboardButton(text="📝 Оставить заявку", callback_data="lead")
BTN_HOME = InlineKeyboardButton(text="🏠 В меню", callback_data="home")


def main_menu(categories) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in categories:
        if c["n_items"]:
            kb.button(text=c["title"], callback_data=f"c:{c['id']}")
    kb.adjust(2)
    kb.row(BTN_LEAD)
    kb.row(BTN_CONTACTS)
    return kb.as_markup()


def sections_menu(cat_id: int, sections: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for idx, title in enumerate(sections):
        kb.button(text=title or "Разное", callback_data=f"s:{cat_id}:{idx}")
    kb.adjust(1)
    kb.row(BTN_HOME)
    return kb.as_markup()


def items_footer(back_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [BTN_LEAD],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_data), BTN_HOME],
        ]
    )


def back_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[BTN_HOME]])


def cancel_lead() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖ Отмена", callback_data="home")]])


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
