"""Общая логика импорта для бота и для manage.py."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .db import DB, ImportDiff
from .rules import is_accessory
from .telegram_export import Catalog


@dataclass
class ImportResult:
    cat_id: int
    title: str
    diff: ImportDiff
    skipped: int
    created: bool


def exclude_words(db: DB, cfg: Config) -> tuple[str, ...]:
    raw = db.get_setting("exclude_words")
    if raw is None:
        return cfg.default_exclude_words
    return tuple(w for w in raw.split("|") if w)


def set_exclude_words(db: DB, words) -> None:
    db.set_setting("exclude_words", "|".join(sorted({w.strip().lower() for w in words if w.strip()})))


def create_category(db: DB, cfg: Config, title: str) -> int:
    """Новая категория; аксессуарам сразу ставим их наценку."""
    cat_id = db.add_category(title.strip()[:60])
    if is_accessory(title, cfg.accessory_words):
        db.set_category_markup(cat_id, None, cfg.accessory_markup_fixed)
    return cat_id


def import_catalog(db: DB, cfg: Config, catalog: Catalog) -> list[ImportResult]:
    results = []
    for c in catalog.categories:
        cat = None
        if catalog.source_chat_id and c.source_msg_id:
            cat = db.find_by_source(catalog.source_chat_id, c.source_msg_id)
        cat = cat or db.find_category_by_title(c.title)
        created = cat is None
        cat_id = create_category(db, cfg, c.title) if created else cat["id"]
        if catalog.source_chat_id and c.source_msg_id:
            db.set_source(cat_id, catalog.source_chat_id, c.source_msg_id)
        diff = db.replace_items(cat_id, c.sections)
        results.append(ImportResult(cat_id, db.category(cat_id)["title"], diff, c.skipped, created))
    return results
