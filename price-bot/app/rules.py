"""Что не продаём и какая наценка у категории."""
from __future__ import annotations

from .parser import Section

# Позиции с этими словами в названии в бот не попадают (чехлы, зарядки, доп. гарантия).
# Меняется командой /exclude прямо в боте.
DEFAULT_EXCLUDE_WORDS = (
    "чехол", "silicone case", "clear case", "leather case", "techwoven", "finewoven", "case with magsafe",
    "adapter", "адаптер", "зарядк", "charger", "кабель", "cable",
    "гарант", "🛡", "стекло", "пленк", "плёнк", "ремеш",
)

# Категории-аксессуары (по названию категории) получают «аксессуарную» наценку.
DEFAULT_ACCESSORY_WORDS = (
    "airpods", "наушник", "очки", "ray-ban", "рейбан", "часы", "watch", "aw ", "колонк", "гаджет", "аксессуар",
)


def is_excluded(name: str, words) -> bool:
    low = name.lower()
    # «+1 месяц -1.000» — платная доп. гарантия
    if low.lstrip("🛡 ").startswith("+"):
        return True
    return any(w and w in low for w in words)


def filter_sections(sections: list[Section], words) -> tuple[list[Section], int]:
    """Убирает ненужные позиции; возвращает оставшиеся разделы и сколько позиций выкинуто."""
    kept, skipped = [], 0
    for s in sections:
        items = [it for it in s.items if not is_excluded(it.name, words)]
        skipped += len(s.items) - len(items)
        if items:
            kept.append(Section(title=s.title, items=items))
    return kept, skipped


def is_accessory(category_title: str, words) -> bool:
    low = category_title.lower() + " "
    return any(w and w in low for w in words)
