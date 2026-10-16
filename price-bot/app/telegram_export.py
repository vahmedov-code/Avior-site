"""Сбор каталога из экспорта Telegram Desktop (result.json).

В группе поставщика есть пост-меню с кнопками-ссылками на посты с ценами.
Находим этот пост, по кнопкам берём тексты постов и разбираем их.
Если кнопок в экспорте нет — берём каждый пост, где нашлось много цен.
"""
from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass, field

from .parser import BOILERPLATE_RE, LEADING_JUNK_RE, Section, count_items, parse_post
from .rules import filter_sections

LINK_ID_RE = re.compile(r"t\.me/(?:c/)?[^/\s]+/(\d+)")
TITLE_JUNK_RE = re.compile(r"^[^\wА-Яа-яЁё]+")
MIN_ITEMS_FALLBACK = 5
MIN_ITEMS_EXTRA_POST = 3
# Раздел без внятного названия длиннее этого делим по брендам
SPLIT_BY_BRAND_OVER = 40
GENERIC_WORDS = {"ноутбук", "игровой", "моноблок", "смартфон", "планшет", "новый", "new"}
WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё][\w\-]*")


@dataclass
class CatalogCategory:
    title: str
    sections: list[Section]
    skipped: int = 0
    source_msg_id: int | None = None


@dataclass
class Catalog:
    source_chat_id: int | None
    categories: list[CatalogCategory] = field(default_factory=list)


def _rich_text(node) -> str:
    """Текст узла rich_message: plain/bold/concat/... вложены друг в друга."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_rich_text(n) for n in node)
    if isinstance(node, dict):
        if node.get("type") in ("photo", "video"):
            return ""
        return _rich_text(node.get("text", ""))
    return ""


def message_text(msg: dict) -> str:
    rich = msg.get("rich_message")
    if isinstance(rich, dict) and rich.get("blocks"):
        # Новый формат постов: каждый блок-абзац — отдельная строка
        lines = []
        for block in rich["blocks"]:
            if block.get("type") == "paragraph":
                lines.append(_rich_text(block.get("text")))
            elif block.get("caption"):
                lines.append(_rich_text(block["caption"]))
        return "\n".join(lines)
    text = msg.get("text", "")
    if isinstance(text, list):
        return "".join(p if isinstance(p, str) else p.get("text", "") for p in text)
    return text or ""


def _buttons(msg: dict) -> list[tuple[str, int]]:
    found = []
    for row in msg.get("inline_bot_buttons") or []:
        for btn in row:
            url = str(btn.get("data") or btn.get("url") or "")
            m = LINK_ID_RE.search(url)
            if m:
                title = TITLE_JUNK_RE.sub("", str(btn.get("text", ""))).strip()
                found.append((title, int(m.group(1))))
    return found


def find_menu(messages: list[dict]) -> list[tuple[str, int]]:
    """Кнопки самого «кнопочного» поста — это и есть меню категорий."""
    best: list[tuple[str, int]] = []
    for msg in messages:
        buttons = _buttons(msg)
        if len(buttons) > len(best):
            best = buttons
    return best


def bot_api_chat_id(export: dict) -> int | None:
    """В экспорте id канала без префикса -100, а Bot API присылает его с префиксом."""
    raw = export.get("id")
    if not isinstance(raw, int):
        return None
    if "channel" in str(export.get("type", "")) or "supergroup" in str(export.get("type", "")):
        return int(f"-100{raw}")
    return -raw


def _heading_key(title: str) -> str:
    return LEADING_JUNK_RE.sub("", title).strip().lower()


def _attach_extra_posts(messages: list[dict], target_ids: list[int], block_words) -> dict[int, list[dict]]:
    """Категория у поставщика часто занимает несколько постов подряд, а кнопка меню
    ведёт только на первый. Пост без кнопки относим к предыдущей категории. К следующей —
    только если его первый раздел так же назван в следующей категории и не назван
    в предыдущей (саундбары стоят перед телевизорами)."""
    parsed = {m["id"]: parse_post(message_text(m), block_words) for m in messages}

    def headings(msg_id: int) -> set[str]:
        return {_heading_key(s.title) for s in parsed[msg_id] if s.title}

    ordered = sorted(target_ids)
    extra: dict[int, list[dict]] = {t: [] for t in ordered}
    for msg in sorted(messages, key=lambda m: m["id"]):
        mid = msg["id"]
        if mid in extra or count_items(parsed[mid]) < MIN_ITEMS_EXTRA_POST:
            continue
        pos = bisect_left(ordered, mid)
        prev_t = ordered[pos - 1] if pos > 0 else None
        next_t = ordered[pos] if pos < len(ordered) else None
        if prev_t is None:
            continue  # до первой категории — служебные посты
        owner = prev_t
        first = _heading_key(parsed[mid][0].title)
        if next_t is not None and first and first in headings(next_t) and first not in headings(prev_t):
            owner = next_t
        extra[owner].append(msg)
    return extra


def build_catalog(export: dict, block_words=(), exclude_words=(), skip_categories=(), merge=None,
                  only=None) -> Catalog:
    messages = [m for m in export.get("messages", []) if m.get("type", "message") == "message"]
    by_id = {m["id"]: m for m in messages if "id" in m}
    catalog = Catalog(source_chat_id=bot_api_chat_id(export))

    targets = []
    seen = set()
    for title, msg_id in find_menu(messages):
        if msg_id in by_id and msg_id not in seen:
            seen.add(msg_id)
            targets.append((title, by_id[msg_id]))

    if targets:
        extra = _attach_extra_posts(messages, [m["id"] for _, m in targets], block_words)
        groups = [(title, [msg] + extra[msg["id"]]) for title, msg in targets]
    else:
        groups = []
        for msg in messages:
            sections = parse_post(message_text(msg), block_words)
            if count_items(sections) >= MIN_ITEMS_FALLBACK:
                groups.append((sections[0].title or f"Пост №{msg['id']}", [msg]))

    skip = {c.strip().lower() for c in skip_categories}
    # merge: {«игровые ноутбуки»: «Ноутбуки»} — несколько категорий поставщика в одну нашу
    rename = {k.strip().lower(): v.strip() for k, v in (merge or {}).items()}
    # only: {«пылесосы»: «dyson»} — из категории берём только позиции с этим словом
    only_word = {k.strip().lower(): v.strip().lower() for k, v in (only or {}).items()}
    combined: dict[str, CatalogCategory] = {}
    for title, msgs in groups:
        title = title or f"Пост №{msgs[0]['id']}"
        if title.strip().lower() in skip:
            continue
        keep_word = only_word.get(title.strip().lower())
        title = rename.get(title.strip().lower(), title)
        sections = []
        for msg in msgs:
            sections += parse_post(message_text(msg), block_words)
        if keep_word:
            sections = _keep_only(sections, keep_word)
        sections, skipped = filter_sections(_merge_sections(sections), exclude_words)
        if not sections:
            continue
        sections = _split_big_sections(sections)
        key = title.lower()
        if key in combined:
            cat = combined[key]
            cat.sections = _merge_sections(cat.sections + sections)
            cat.skipped += skipped
        else:
            combined[key] = CatalogCategory(title, sections, skipped, msgs[0].get("id"))
            catalog.categories.append(combined[key])
    return catalog


def _keep_only(sections: list[Section], word: str) -> list[Section]:
    """Позиции, где слово есть в названии товара или раздела (Dyson V8 лежит в разделе «Dyson…»)."""
    out = []
    for s in sections:
        if word in s.title.lower():
            out.append(s)
        else:
            items = [it for it in s.items if word in it.name.lower()]
            if items:
                out.append(Section(title=s.title, items=items))
    return out


def _brand(name: str) -> str:
    for word in WORD_RE.findall(name):
        if word.lower() not in GENERIC_WORDS:
            return word
    return ""


def _split_big_sections(sections: list[Section]) -> list[Section]:
    """Длинный раздел без названия («», «Ноутбуки по предзаказу») режем по брендам:
    листать 267 ноутбуков подряд в боте неудобно."""
    out: list[Section] = []
    for s in sections:
        if len(s.items) <= SPLIT_BY_BRAND_OVER or (s.title and not BOILERPLATE_RE.search(s.title)):
            out.append(s)
            continue
        by_brand: dict[str, Section] = {}
        for it in s.items:
            brand = _brand(it.name)
            key = brand.lower()
            if key not in by_brand:
                by_brand[key] = Section(title=brand or s.title)
            by_brand[key].items.append(it)
        out += by_brand.values()
    return _merge_sections(out)


def _merge_sections(sections: list[Section]) -> list[Section]:
    """Одноимённые разделы из разных постов склеиваем в один."""
    merged: dict[str, Section] = {}
    for s in sections:
        key = _heading_key(s.title)  # «Саундбары» и «➡ Саундбары» — один раздел
        if key in merged:
            merged[key].items.extend(s.items)
        else:
            merged[key] = Section(title=s.title, items=list(s.items))
    return list(merged.values())


def catalog_to_json(catalog: Catalog) -> dict:
    return {
        "format": "avior-catalog-1",
        "source_chat_id": catalog.source_chat_id,
        "categories": [
            {
                "title": c.title,
                "source_msg_id": c.source_msg_id,
                "sections": [
                    {"title": s.title, "items": [[it.name, it.price, it.note] for it in s.items]} for s in c.sections
                ],
            }
            for c in catalog.categories
        ],
    }


def catalog_from_json(data: dict) -> Catalog:
    from .parser import Item

    catalog = Catalog(source_chat_id=data.get("source_chat_id"))
    for c in data.get("categories", []):
        sections = [
            Section(title=s["title"], items=[Item(name, int(price), note) for name, price, note in s["items"]])
            for s in c["sections"]
        ]
        catalog.categories.append(CatalogCategory(c["title"], sections, 0, c.get("source_msg_id")))
    return catalog


def load_any(data: dict, block_words=(), exclude_words=(), skip_categories=(), merge=None, only=None) -> Catalog:
    """Принимает и сырой экспорт Telegram, и уже собранный catalog.json."""
    if data.get("format") == "avior-catalog-1":
        return catalog_from_json(data)
    if "messages" in data:
        return build_catalog(data, block_words, exclude_words, skip_categories, merge, only)
    raise ValueError("Это не экспорт Telegram (result.json) и не catalog.json")
