"""Разбор текстового прайса из Telegram-поста в разделы и позиции.

Формат строк у поставщика неоднородный, например:
    18 Pro 256Gb Black-115.500🇰🇷🇭🇰(1 sim+e sim)
    iPhone 18 Pro Silicone Case with MagSafe - Burgundy 7.500
    ...TechWoven Case with MagSafe – Mulberry10.000
    Adapter Apple 20 w -1800
Строка без цены считается заголовком раздела, строки «От 10шт 1500» — оптовые
ступени (пропускаем), ссылки и упоминания — тоже пропускаем.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 115.500 / 7,500 / 1800 / 250000. Цифры через пробел не берём: «iPhone 16 128»
# иначе превратилось бы в цену 16128.
PRICE_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[.,]\d{3})+|\d{4,7})(?![\d.,])")
WHOLESALE_RE = re.compile(r"^от\s*\d", re.IGNORECASE)
LETTER_RE = re.compile(r"[A-Za-zА-Яа-яЁё]")
INVISIBLE_RE = re.compile(r"[​-‏⁠️ ]")
TRAILING_SEP_RE = re.compile(r"[\s\-–—:]+$")
NOTE_JUNK_RE = re.compile(r"^[\s*\-–—]*(?:₽|руб\.?|р\.)?[\s*]*", re.IGNORECASE)

MIN_PRICE = 100
MAX_SECTION_TITLE = 80


@dataclass
class Item:
    name: str
    price: int
    note: str = ""


@dataclass
class Section:
    title: str
    items: list[Item] = field(default_factory=list)


def _clean(line: str) -> str:
    return re.sub(r"\s+", " ", INVISIBLE_RE.sub(" ", line)).strip()


def _letters(s: str) -> int:
    return len(LETTER_RE.findall(s))


def parse_line(line: str) -> Item | None:
    m = PRICE_RE.search(line)
    if not m:
        return None
    name = TRAILING_SEP_RE.sub("", line[: m.start()]).strip()
    price = int(re.sub(r"[.,]", "", m.group(1)))
    if _letters(name) < 2 or price < MIN_PRICE:
        return None
    note = NOTE_JUNK_RE.sub("", line[m.end():]).strip()
    return Item(name=name, price=price, note=note)


def parse_post(text: str, block_words: tuple[str, ...] = ()) -> list[Section]:
    """Возвращает непустые разделы в порядке появления в посте."""
    sections: list[Section] = [Section(title="")]
    block = tuple(w.lower() for w in block_words if w)

    for raw in text.splitlines():
        line = _clean(raw)
        if not line:
            continue
        low = line.lower()
        if "http" in low or "t.me/" in low or "@" in line:
            continue
        if block and any(w in low for w in block):
            continue
        if WHOLESALE_RE.match(line):
            continue

        item = parse_line(line)
        if item:
            sections[-1].items.append(item)
        elif _letters(line) >= 3 and len(line) <= MAX_SECTION_TITLE:
            title = TRAILING_SEP_RE.sub("", line)
            if sections[-1].items:
                sections.append(Section(title=title))
            else:
                # Подряд идущие заголовки: оставляем последний, он ближе к ценам.
                sections[-1].title = title

    return [s for s in sections if s.items]


def count_items(sections: list[Section]) -> int:
    return sum(len(s.items) for s in sections)
