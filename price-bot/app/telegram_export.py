"""Сбор каталога из экспорта Telegram Desktop (result.json).

В группе поставщика есть пост-меню с кнопками-ссылками на посты с ценами.
Находим этот пост, по кнопкам берём тексты постов и разбираем их.
Если кнопок в экспорте нет — берём каждый пост, где нашлось много цен.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .parser import Section, count_items, parse_post
from .rules import filter_sections

LINK_ID_RE = re.compile(r"t\.me/(?:c/)?[^/\s]+/(\d+)")
TITLE_JUNK_RE = re.compile(r"^[^\wА-Яа-яЁё]+")
MIN_ITEMS_FALLBACK = 5


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


def message_text(msg: dict) -> str:
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


def build_catalog(export: dict, block_words=(), exclude_words=()) -> Catalog:
    messages = [m for m in export.get("messages", []) if m.get("type", "message") == "message"]
    by_id = {m["id"]: m for m in messages if "id" in m}
    catalog = Catalog(source_chat_id=bot_api_chat_id(export))

    targets = []
    seen = set()
    for title, msg_id in find_menu(messages):
        if msg_id in by_id and msg_id not in seen:
            seen.add(msg_id)
            targets.append((title, by_id[msg_id]))

    if not targets:
        for msg in messages:
            sections = parse_post(message_text(msg), block_words)
            if count_items(sections) >= MIN_ITEMS_FALLBACK:
                targets.append((sections[0].title or f"Пост №{msg['id']}", msg))

    for title, msg in targets:
        sections = parse_post(message_text(msg), block_words)
        if not sections:
            continue
        sections, skipped = filter_sections(sections, exclude_words)
        if sections:
            catalog.categories.append(CatalogCategory(title or f"Пост №{msg['id']}", sections, skipped, msg.get("id")))
    return catalog


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


def load_any(data: dict, block_words=(), exclude_words=()) -> Catalog:
    """Принимает и сырой экспорт Telegram, и уже собранный catalog.json."""
    if data.get("format") == "avior-catalog-1":
        return catalog_from_json(data)
    if "messages" in data:
        return build_catalog(data, block_words, exclude_words)
    raise ValueError("Это не экспорт Telegram (result.json) и не catalog.json")
