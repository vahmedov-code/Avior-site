from pathlib import Path

import pytest

from app.db import DB
from app.parser import Item, count_items, parse_line, parse_post
from app.pricing import apply_markup, fmt_price

SAMPLE = (Path(__file__).parent / "fixtures" / "sample_post.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "line, name, price, note",
    [
        ("18 Pro 256Gb Black-115.500🇰🇷🇭🇰(1 sim+e sim)", "18 Pro 256Gb Black", 115500, "🇰🇷🇭🇰(1 sim+e sim)"),
        ("18 Pro Max 2Tb Silver -223.000🇺🇸e sim", "18 Pro Max 2Tb Silver", 223000, "🇺🇸e sim"),
        ("18 Pro 256Gb Glacier-116.000*🇰🇷🇭🇰(1 sim+e sim)актив 🔥", "18 Pro 256Gb Glacier", 116000, "🇰🇷🇭🇰(1 sim+e sim)актив 🔥"),
        ("iPhone 18 Pro Max TechWoven Case with MagSafe – Mulberry10.000",
         "iPhone 18 Pro Max TechWoven Case with MagSafe – Mulberry", 10000, ""),
        ("iPhone 18 Pro Silicone Case with MagSafe - Burgundy 7.500",
         "iPhone 18 Pro Silicone Case with MagSafe - Burgundy", 7500, ""),
        ("Adapter Apple 35W-2.500", "Adapter Apple 35W", 2500, ""),
        ("🇪🇺Adapter Apple 20 w -1800", "🇪🇺Adapter Apple 20 w", 1800, ""),
        ("🛡+1 месяц -1.000 НОВИНКА", "🛡+1 месяц", 1000, "НОВИНКА"),
    ],
)
def test_parse_line(line, name, price, note):
    assert parse_line(line) == Item(name, price, note)


@pytest.mark.parametrize("line", ["17/10/2026", "iPhone 18 Pro 256/512/1TB/2TB", "Гарантия 14 дней", "❤️"])
def test_parse_line_not_price(line):
    assert parse_line(line) is None


def test_parse_post_sections():
    sections = parse_post(SAMPLE, ("bigsale",))
    titles = [s.title for s in sections]
    assert titles[:4] == [
        "Получите дополнительную гарантию от 📱📱📱",
        "Оригинальные блоки",
        "iPhone 18 Pro 256/512/1TB/2TB",
        "iPhone 18 Pro Max 256/512/1TB/2TB",
    ]
    all_items = [it for s in sections for it in s.items]
    # оптовые ступени «От 10шт» не попадают в розницу
    assert not any(it.name.lower().startswith("от ") for it in all_items)
    assert count_items(sections) == 25
    assert sections[2].items[0] == Item("18 Pro 256Gb Black", 115500, "🇰🇷🇭🇰(1 sim+e sim)")


def test_markup():
    assert apply_markup(115500, 10, 0, 100) == 127100  # 127 050 → вверх до 127 100
    assert apply_markup(7500, 0, 500, 100) == 8000
    assert apply_markup(1234, 0, 0, 1) == 1234
    assert apply_markup(100000, 0, 0, 1000) == 100000
    assert fmt_price(127100) == "127 100 ₽"


def test_db_import_diff_and_markup():
    db = DB(":memory:")
    db.init_defaults({"markup_pct": 10, "markup_fixed": 0, "round_step": 100})
    cat = db.add_category("iPhone 18 Pro")

    first = db.replace_items(cat, parse_post(SAMPLE))
    assert first.total == 25 and first.previous == 0

    changed = SAMPLE.replace("Black-115.500", "Black-112.000").replace("Adapter Apple 35W-2.500\n", "")
    diff = db.replace_items(cat, parse_post(changed))
    assert diff.previous == 25 and diff.total == 24
    assert diff.changed == [("18 Pro 256Gb Black 🇰🇷🇭🇰(1 sim+e sim)", 115500, 112000)]
    assert [n for n, _ in diff.removed] == ["Adapter Apple 35W"]
    assert db.guess_category(parse_post(SAMPLE)) == cat

    db.set_category_markup(cat, 20, 1000)
    assert db.markup(db.category(cat)) == (20, 1000, 100)
    db.set_category_markup(cat, None, None)
    assert db.markup(db.category(cat)) == (10.0, 0, 100)


def test_source_link_moves_between_categories():
    db = DB(":memory:")
    a, b = db.add_category("A"), db.add_category("B")
    db.set_source(a, -100, 5)
    db.set_source(b, -100, 5)
    assert db.find_by_source(-100, 5)["id"] == b


def test_move_category():
    db = DB(":memory:")
    ids = [db.add_category(t) for t in "ABC"]
    db.move_category(ids[2], 1)
    assert [c["title"] for c in db.categories()] == ["C", "A", "B"]


def test_handlers_import():
    from app import handlers_admin, handlers_user  # noqa: F401


def test_cart_survives_price_update_and_order():
    db = DB(":memory:")
    db.init_defaults({"markup_pct": 10, "markup_fixed": 0, "round_step": 100})
    cat = db.add_category("iPhone 18 Pro")
    db.replace_items(cat, parse_post(SAMPLE))
    black = next(i for i in db.items(cat) if i["name"] == "18 Pro 256Gb Black")
    adapter = next(i for i in db.items(cat) if i["name"] == "Adapter Apple 35W")
    db.add_to_cart(7, black)
    db.add_to_cart(7, black)
    db.add_to_cart(7, adapter)
    assert db.cart_count(7) == 3

    # поставщик поднял цену и убрал адаптер — корзина это видит
    db.replace_items(cat, parse_post(SAMPLE.replace("Black-115.500", "Black-120.000")
                                     .replace("Adapter Apple 35W-2.500\n", "")))
    lines = db.cart_lines(7)
    assert [(l.qty, l.price, l.available) for l in lines] == [(2, 132000, True), (1, None, False)]

    order_id = db.create_order(7, "Иван", "ivan", "+79001234567", "СДЭК", lines)
    order = db.order(order_id)
    assert order["total"] == 264000 and order["cost"] == 240000
    assert db.order_items(order) == [
        {"name": "18 Pro 256Gb Black 🇰🇷🇭🇰(1 sim+e sim)", "qty": 2, "price": 132000, "cost": 120000}
    ]
    assert db.cart_count(7) == 0
