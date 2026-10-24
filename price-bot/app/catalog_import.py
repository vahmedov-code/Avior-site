"""Общая логика импорта для бота и для manage.py."""
from __future__ import annotations

import json
from dataclasses import dataclass

from .config import Config
from .db import DB, ImportDiff
from .rules import is_accessory
from .telegram_export import Catalog, supplier_info


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


def skip_categories(db: DB, cfg: Config) -> tuple[str, ...]:
    raw = db.get_setting("skip_categories")
    if raw is None:
        return cfg.default_skip_categories
    return tuple(c for c in raw.split("|") if c)


def set_skip_categories(db: DB, titles) -> None:
    db.set_setting("skip_categories", "|".join(sorted({t.strip() for t in titles if t.strip()})))


def merge_categories(db: DB, cfg: Config) -> dict[str, str]:
    raw = db.get_setting("merge_categories")
    if raw is None:
        return dict(cfg.default_merge_categories)
    return dict(pair.split("=", 1) for pair in raw.split("|") if "=" in pair)


def only_words(db: DB, cfg: Config) -> dict[str, str]:
    raw = db.get_setting("only_words")
    if raw is None:
        return dict(cfg.default_only_words)
    return dict(pair.split("=", 1) for pair in raw.split("|") if "=" in pair)


def set_only_words(db: DB, mapping: dict[str, str]) -> None:
    db.set_setting("only_words", "|".join(f"{k}={v}" for k, v in sorted(mapping.items())))


def catalog_filters(db: DB, cfg: Config) -> dict:
    """Все настройки отбора для load_any/build_catalog."""
    return {
        "block_words": cfg.block_words,
        "exclude_words": exclude_words(db, cfg),
        "skip_categories": skip_categories(db, cfg),
        "merge": merge_categories(db, cfg),
        "only": only_words(db, cfg),
    }


def set_merge_categories(db: DB, mapping: dict[str, str]) -> None:
    db.set_setting("merge_categories", "|".join(f"{k}={v}" for k, v in sorted(mapping.items())))


def set_exclude_words(db: DB, words) -> None:
    db.set_setting("exclude_words", "|".join(sorted({w.strip().lower() for w in words if w.strip()})))


def remember_supplier(db: DB, export: dict) -> None:
    """Запоминаем канал и его меню из экспорта: по ним работает браузерный сборщик."""
    info = supplier_info(export) if "messages" in export else None
    if info:
        db.set_setting("supplier", json.dumps(info, ensure_ascii=False))


def supplier(db: DB) -> dict | None:
    raw = db.get_setting("supplier")
    return json.loads(raw) if raw else None


def create_category(db: DB, cfg: Config, title: str) -> int:
    """Новая категория; аксессуарам сразу ставим их наценку."""
    cat_id = db.add_category(title.strip()[:60])
    if is_accessory(title, cfg.accessory_words):
        db.set_category_markup(cat_id, None, cfg.accessory_markup_fixed)
    return cat_id


def import_catalog(db: DB, cfg: Config, catalog: Catalog) -> list[ImportResult]:
    results = []
    for c in catalog.categories:
        # Сначала по названию: при объединении («Пылесосы» → «Dyson») пост-источник
        # совпадает со старой категорией «Пылесосы», а писать надо в «Dyson».
        # По посту — запасной путь, если категорию переименовали через /rename.
        cat = db.find_category_by_title(c.title)
        if cat is None and catalog.source_chat_id and c.source_msg_id:
            cat = db.find_by_source(catalog.source_chat_id, c.source_msg_id)
        created = cat is None
        cat_id = create_category(db, cfg, c.title) if created else cat["id"]
        if catalog.source_chat_id and c.source_msg_id:
            db.set_source(cat_id, catalog.source_chat_id, c.source_msg_id)
        diff = db.replace_items(cat_id, c.sections)
        results.append(ImportResult(cat_id, db.category(cat_id)["title"], diff, c.skipped, created))
    return results
