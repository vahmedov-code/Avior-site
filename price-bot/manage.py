"""Скрипт управления прайсом AVIOR Shop.

  python manage.py excel result.json         Excel с вашими ценами прямо из экспорта группы
  python manage.py import result.json        обновить цены в боте (база data/shop.db)
  python manage.py excel                     Excel из текущей базы бота
  python manage.py catalog result.json       сжать большой экспорт в catalog.json (его можно отправить боту)

result.json — экспорт группы поставщика из Telegram Desktop
(⋮ в группе → Экспорт истории чата → формат JSON, без фото и файлов).
"""
import argparse
import json
import os
import sys
from datetime import date

from app.catalog_import import exclude_words, import_catalog
from app.config import Config
from app.db import DB
from app.excel import build_xlsx
from app.telegram_export import catalog_to_json, load_any


def load_catalog(path: str, db: DB, cfg: Config):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    catalog = load_any(data, cfg.block_words, exclude_words(db, cfg))
    if not catalog.categories:
        sys.exit("В файле не нашлось постов с ценами.")
    return catalog


def bot_db(cfg: Config) -> DB:
    db = DB(cfg.db_path)
    db.init_defaults({
        "markup_pct": cfg.default_markup_pct,
        "markup_fixed": cfg.default_markup_fixed,
        "round_step": cfg.default_round_step,
    })
    return db


def print_results(results) -> None:
    for r in results:
        changes = f"цен изменилось {len(r.diff.changed)}" if r.diff.previous else "новая" if r.created else ""
        print(f"  {r.cat_id:>3}. {r.title:<30} {r.diff.total:>4} поз.  {changes}"
              + (f"  (пропущено {r.skipped})" if r.skipped else ""))
        for name, old, new in r.diff.changed[:5]:
            print(f"        {name}: {old} → {new}")
    print(f"Итого: {len(results)} категорий, {sum(r.diff.total for r in results)} позиций")


def cmd_import(args, cfg: Config) -> None:
    db = bot_db(cfg)
    results = import_catalog(db, cfg, load_catalog(args.file, db, cfg))
    print(f"Обновлена база бота {cfg.db_path}:")
    print_results(results)


def cmd_excel(args, cfg: Config) -> None:
    if args.file:
        # Считаем в памяти, базу бота не трогаем; наценки — из базы бота, если она есть
        db = DB(":memory:")
        source = bot_db(cfg) if os.path.exists(cfg.db_path) else None
        db.init_defaults({
            "markup_pct": source.get_setting("markup_pct") if source else cfg.default_markup_pct,
            "markup_fixed": source.get_setting("markup_fixed") if source else cfg.default_markup_fixed,
            "round_step": source.get_setting("round_step") if source else cfg.default_round_step,
        })
        if source and source.get_setting("exclude_words") is not None:
            db.set_setting("exclude_words", source.get_setting("exclude_words"))
        results = import_catalog(db, cfg, load_catalog(args.file, db, cfg))
        if source:
            for r in results:
                own = source.find_category_by_title(r.title)
                if own and (own["markup_pct"] is not None or own["markup_fixed"] is not None):
                    db.set_category_markup(r.cat_id, own["markup_pct"], own["markup_fixed"])
    else:
        if not os.path.exists(cfg.db_path):
            sys.exit(f"База бота {cfg.db_path} не найдена. Укажите файл: python manage.py excel result.json")
        db = bot_db(cfg)
    out = args.output or f"AVIOR_price_{date.today():%Y-%m-%d}.xlsx"
    rows = build_xlsx(db, out)
    print(f"Готово: {out} ({rows} позиций)")


def cmd_catalog(args, cfg: Config) -> None:
    db = DB(":memory:")
    catalog = load_catalog(args.file, db, cfg)
    out = args.output or "catalog.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(catalog_to_json(catalog), f, ensure_ascii=False)
    n = sum(len(s.items) for c in catalog.categories for s in c.sections)
    print(f"Готово: {out} — {len(catalog.categories)} категорий, {n} позиций. Отправьте этот файл боту.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("import", help="обновить цены в боте из result.json или catalog.json")
    p.add_argument("file")
    p.set_defaults(func=cmd_import)
    p = sub.add_parser("excel", help="сделать Excel-прайс с вашими ценами")
    p.add_argument("file", nargs="?", help="result.json/catalog.json; без него — из базы бота")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_excel)
    p = sub.add_parser("catalog", help="сжать экспорт в маленький catalog.json для отправки боту")
    p.add_argument("file")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_catalog)
    args = parser.parse_args()
    args.func(args, Config.from_env(for_bot=False))


if __name__ == "__main__":
    main()
