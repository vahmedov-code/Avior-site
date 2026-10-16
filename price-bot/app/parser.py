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

# Кандидаты в цену: 115.500 / 7,500 / 33 500 / 1800 / 250000
PRICE_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[.,]\d{3})+|\d{1,3}(?: \d{3})+|\d{4,7})(?![\d.,])")
# «От 10шт 1500», «До 140.000», «От 120.000 до 200.000» — опт и ценовые диапазоны, не товары
RANGE_RE = re.compile(r"^(?:от|до)\s*\d", re.IGNORECASE)
# Строки-характеристики: артикулы, серийники
SPEC_RE = re.compile(r"^(?:art|арт|артикул|s/n|sku)\b", re.IGNORECASE)
LETTER_RE = re.compile(r"[A-Za-zА-Яа-яЁё]")
LEADING_JUNK_RE = re.compile(r"^[^A-Za-zА-Яа-яЁё\d]+")
INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u2060\ufe0f\u00a0]")
TRAILING_SEP_RE = re.compile(r"[\s\-–—:]+$")
NOTE_JUNK_RE = re.compile(r"^[\s*\-–—]*(?:₽|руб\.?|р\.?)?[\s*]*", re.IGNORECASE)
SEPARATORS = "-–—:"
CURRENCY_LETTERS = "рР₽"
MEMORY_SIZES = {"064", "128", "256", "512"}

# Служебные строки, которые не годятся в названия разделов
BOILERPLATE_RE = re.compile(r"предзаказ|выдача|гаранти|в наличии|доставк|оплат|фото товара|количество ограничено|оттенки на фото",
                            re.IGNORECASE)

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


def _price_candidates(line: str):
    """Числа в строке, которые могут быть ценой (модели, артикулы, годы, память отсеиваем)."""
    for m in PRICE_RE.finditer(line):
        raw = m.group(1)
        before = line[m.start() - 1] if m.start() else ""
        after = line[m.end()] if m.end() < len(line) else ""
        currency_after = bool(after) and after in CURRENCY_LETTERS
        sep_before = line[:m.start()].rstrip()[-1:] in SEPARATORS
        if raw.startswith("0"):
            continue  # 02785 — кусок артикула
        if after.isalpha() and not currency_after:
            continue  # 12450H, 5070Ti — модель, не цена
        if before.isalpha() and raw.isdigit():
            continue  # MF3010 — модель
        if before == "(" or after == ")":
            continue  # (2026)
        if raw.isdigit() and 2015 <= int(raw) <= 2035 and not currency_after:
            continue  # «Watch Ultra 4 2026», «Год выпуска: 2023»
        if " " in raw:
            # «33 500» — цена, только если стоит в конце строки, после «—» или перед «₽»;
            # иначе это «Core Ultra 5 115U · …» или «iPhone 17 256»
            at_end = not line[m.end():].strip(" *🔥🆕")
            if not (sep_before or currency_after or at_end):
                continue
            if raw.rsplit(" ", 1)[1] in MEMORY_SIZES and not (sep_before or currency_after):
                continue
        yield m


def parse_line(line: str) -> Item | None:
    plain = LEADING_JUNK_RE.sub("", line)
    if RANGE_RE.match(plain) or SPEC_RE.match(plain):
        return None
    candidates = list(_price_candidates(line))
    if not candidates:
        return None
    # Цена у поставщика всегда в конце строки, артикулы и модели — раньше
    m = candidates[-1]
    name = TRAILING_SEP_RE.sub("", line[: m.start()]).strip()
    price = int(re.sub(r"[.,\s]", "", m.group(1)))
    if _letters(name) < 3 or price < MIN_PRICE:
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
        if "🔙" in line:
            continue  # «Samsung Phone 🔙🔙🔙» — ссылка-навигация на другую категорию
        if SPEC_RE.match(LEADING_JUNK_RE.sub("", line)):
            continue  # «Art: 010-02785-02» — артикул под товаром
        if line.count("·") >= 2:
            # «Core i5 · 16GB RAM · 512GB SSD · …» — характеристики предыдущего товара
            last = sections[-1].items[-1] if sections[-1].items else None
            if last is not None and not last.note:
                last.note = line
            continue
        item = parse_line(line)
        if item:
            sections[-1].items.append(item)
        elif (_letters(line) >= 3 and len(line) <= MAX_SECTION_TITLE and not line.startswith(("•", "+"))
              and not RANGE_RE.match(LEADING_JUNK_RE.sub("", line))):
            title = TRAILING_SEP_RE.sub("", line)
            if sections[-1].items:
                sections.append(Section(title=title))
            elif not (BOILERPLATE_RE.search(title) and sections[-1].title
                      and not BOILERPLATE_RE.search(sections[-1].title)):
                # Подряд идущие заголовки: берём последний, он ближе к ценам,
                # но служебную строку («Выдача в день заказа…») поверх настоящего не ставим.
                sections[-1].title = title

    return [s for s in sections if s.items]


def count_items(sections: list[Section]) -> int:
    return sum(len(s.items) for s in sections)
