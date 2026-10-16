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


# --- экспорт Telegram Desktop, исключения, наценки по категориям, manage.py ---

import json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

from app.catalog_import import import_catalog  # noqa: E402
from app.config import Config  # noqa: E402
from app.rules import DEFAULT_EXCLUDE_WORDS, filter_sections  # noqa: E402
from app.telegram_export import build_catalog, catalog_to_json, load_any  # noqa: E402

AIRPODS_POST = """AirPods
AirPods Pro 3-21.500
AirPods 4 ANC-13.000
AirPods Max USB-C Midnight-45.000
Чехол для AirPods Pro 3 - 1.500
"""


def make_export():
    return {
        "name": "BigSale", "type": "public_channel", "id": 1234567890,
        "messages": [
            {"id": 10, "type": "message", "text": "Меню",
             "inline_bot_buttons": [
                 [{"type": "url", "text": "📱 iPhone 18 Pro", "data": "https://t.me/BigSaleApple/12970"}],
                 [{"type": "url", "text": "🎧 AirPods", "data": "https://t.me/BigSaleApple/12971"}],
                 [{"type": "url", "text": "VK", "data": "https://vk.com/bigsale"}],
                 [{"type": "url", "text": "Trade-In", "data": "https://t.me/BigSaleApple/5"}],
             ]},
            {"id": 12970, "type": "message", "text": [SAMPLE[:400], {"type": "bold", "text": SAMPLE[400:]}]},
            {"id": 12971, "type": "message", "text": AIRPODS_POST},
            {"id": 5, "type": "message", "text": "Сдайте старый телефон в Trade-In!"},
        ],
    }


def cfg_for_tests():
    return Config(
        bot_token="", admin_ids=frozenset(), shop_name="AVIOR Shop", welcome_text="", contacts_text="",
        db_path=":memory:", default_markup_pct=0, default_markup_fixed=10000, default_round_step=100,
        block_words=("bigsale",), timezone="Europe/Moscow",
    )


def test_exclusions_drop_cases_chargers_warranty():
    sections, skipped = filter_sections(parse_post(SAMPLE), DEFAULT_EXCLUDE_WORDS)
    names = [it.name for s in sections for it in s.items]
    assert all("Case" not in n and "Adapter" not in n and "MagSafe" not in n for n in names)
    assert not any(n.startswith("🛡") for n in names)
    assert skipped == 14 and len(names) == 11  # остались только телефоны


def test_build_catalog_from_export():
    catalog = build_catalog(make_export(), ("bigsale",), DEFAULT_EXCLUDE_WORDS)
    assert catalog.source_chat_id == -1001234567890
    assert [(c.title, c.source_msg_id) for c in catalog.categories] == [("iPhone 18 Pro", 12970), ("AirPods", 12971)]
    airpods = catalog.categories[1]
    assert [it.name for it in airpods.sections[0].items] == ["AirPods Pro 3", "AirPods 4 ANC", "AirPods Max USB-C Midnight"]
    assert airpods.skipped == 1
    # catalog.json туда-обратно
    again = load_any(json.loads(json.dumps(catalog_to_json(catalog))))
    assert again.categories[1].sections == airpods.sections


def test_import_catalog_markups_by_category():
    cfg = cfg_for_tests()
    db = DB(":memory:")
    db.init_defaults({"markup_pct": 0, "markup_fixed": 10000, "round_step": 100})
    results = import_catalog(db, cfg, build_catalog(make_export(), ("bigsale",), DEFAULT_EXCLUDE_WORDS))
    assert [r.created for r in results] == [True, True]
    phones, airpods = results
    phone = db.items(phones.cat_id)[0]
    assert db.sell_price(phone) == 115500 + 10000
    pods = db.items(airpods.cat_id)[0]
    assert db.sell_price(pods) == 21500 + 5000
    # повторный импорт находит те же категории, а пересланный пост — по источнику
    again = import_catalog(db, cfg, build_catalog(make_export(), ("bigsale",), DEFAULT_EXCLUDE_WORDS))
    assert [r.created for r in again] == [False, False]
    assert db.find_by_source(-1001234567890, 12971)["id"] == airpods.cat_id


def test_manage_excel_and_import(tmp_path):
    export = tmp_path / "result.json"
    export.write_text(json.dumps(make_export(), ensure_ascii=False), encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "DB_PATH": str(tmp_path / "shop.db"), "HOME": str(tmp_path)}
    root = Path(__file__).parent.parent
    run = lambda *a: subprocess.run([sys.executable, str(root / "manage.py"), *a], cwd=tmp_path, env=env,  # noqa: E731
                                    capture_output=True, text=True, check=True).stdout

    out = run("excel", str(export), "-o", str(tmp_path / "p.xlsx"))
    assert "p.xlsx (14 позиций)" in out and not (tmp_path / "shop.db").exists()

    from openpyxl import load_workbook
    ws = load_workbook(tmp_path / "p.xlsx").active
    rows = list(ws.values)
    assert rows[1] == ("iPhone 18 Pro", "iPhone 18 Pro 256/512/1TB/2TB", "18 Pro 256Gb Black",
                       "🇰🇷🇭🇰(1 sim+e sim)", 115500, 10000, 125500)
    assert rows[-1][0] == "AirPods" and rows[-1][6] == 50000

    assert "Итого: 2 категорий, 14 позиций" in run("import", str(export))
    assert "Готово: catalog.json" in run("catalog", str(export))
    assert "цен изменилось 0" in run("import", str(tmp_path / "catalog.json"))


# --- правила, найденные на настоящем экспорте канала ---

@pytest.mark.parametrize(
    "line, name, price",
    [
        ("Ballu BWH/S 30 Brief НС-1699356 6300", "Ballu BWH/S 30 Brief НС-1699356", 6300),  # артикул перед ценой
        ("Firelight BFP/P-2520 Биокамин 34300", "Firelight BFP/P-2520 Биокамин", 34300),
        ("Venu 3S Soft Gold/French Gray 33 500", "Venu 3S Soft Gold/French Gray", 33500),  # тысячи через пробел
        ("Rikor NEURO 5 — 50 800", "Rikor NEURO 5", 50800),
        ("ASUS GeForce RTX 5070 Ti 16GB — 89.000", "ASUS GeForce RTX 5070 Ti 16GB", 89000),
        ("Canon I-SENSYS MF3010 ч/б F4 18ppm 2210", "Canon I-SENSYS MF3010 ч/б F4 18ppm", 2210),
        ("Гравировка - 1000₽", "Гравировка", 1000),
    ],
)
def test_parse_line_real_formats(line, name, price):
    item = parse_line(line)
    assert (item.name, item.price) == (name, price)


@pytest.mark.parametrize("line", [
    "Watch Ultra 4 2026🔥", "Год выпуска: 2023", "IPHONE 17 256", "Art: 010-02785-02",
    "➡️ До 140.000", "➡️ От 120.000 до 200.000", "Core Ultra 5 115U · 16 GB RAM · 512 GB SSD",
])
def test_parse_line_not_a_price(line):
    assert parse_line(line) is None


def test_spec_lines_become_notes_and_articles_are_skipped():
    sections = parse_post(
        "Ноутбуки\n"
        "Acer Nitro V 15 — 130 200\n"
        "Core i9-13900H · 16 GB RAM · 512 GB SSD · RTX 5060 8 GB\n"
        "Venu 3S Gray 33 500\n"
        "Art: 010-02785-02\n"
    )
    assert len(sections) == 1 and sections[0].title == "Ноутбуки"
    acer, venu = sections[0].items
    assert (acer.price, acer.note) == (130200, "Core i9-13900H · 16 GB RAM · 512 GB SSD · RTX 5060 8 GB")
    assert venu.price == 33500


def test_boilerplate_does_not_replace_section_title():
    sections = parse_post("Саундбары\nВсе саундбары только по предзаказу\nВыдача в день заказа\nHisense AX 5 — 30 000\n")
    assert sections[0].title == "Саундбары"


def _rich(*lines):
    return {"blocks": [{"type": "paragraph", "text": {"type": "concat", "text": [
        {"type": "bold", "text": {"type": "plain", "text": line}}]}} for line in lines]}


def test_rich_message_posts_multi_post_categories_and_skips():
    export = {
        "name": "Shop", "type": "private_channel", "id": 42,
        "messages": [
            {"id": 1, "type": "message", "text": "Маркировка: Год выпуска: 2023"},
            {"id": 10, "type": "message", "text": "Колонки\nJBL Go 4 — 4.500\nJBL Flip 7 — 12.000\nJBL Charge 6 — 18.000"},
            {"id": 11, "type": "message", "text": "Logitech Z150 - 3.300\nLogitech Z313 - 5.400\nLogitech Z333 - 7.900"},
            {"id": 12, "type": "message", "text": "Саундбары\nВсе саундбары по предзаказу\nHisense AX5 — 30 000\n"
                                                  "Hisense AX3 — 25 000\nLG S40 — 20 000"},
            {"id": 13, "type": "message", "text": "➡ Саундбары\nSamsung Q600 — 40 000\n➡ Телевизоры\nLG 55UT80 — 60 000"},
            {"id": 14, "type": "message", "rich_message": _rich("iPhone 18 Pro", "18 Pro 256 Black-115.500")},
            {"id": 15, "type": "message", "rich_message": _rich("iPhone Duo 256 Star White", "Цен пока нет")},
            {"id": 20, "type": "message", "text": "Меню", "inline_bot_buttons": [
                [{"type": "url", "text": "Колонки", "data": "https://t.me/Shop/10"}],
                [{"type": "url", "text": "Телевизоры", "data": "https://t.me/Shop/13"}],
                [{"type": "url", "text": "iPhone 18 Pro", "data": "https://t.me/Shop/14"}],
                [{"type": "url", "text": "iPhone Duo", "data": "https://t.me/Shop/15"}],
            ]},
        ],
    }
    catalog = build_catalog(export, (), DEFAULT_EXCLUDE_WORDS)
    by_title = {c.title: c for c in catalog.categories}
    # Logitech без кнопки — к предыдущим колонкам; саундбары — к следующей категории, где есть такой раздел
    assert [it.name for s in by_title["Колонки"].sections for it in s.items][-1] == "Logitech Z333"
    assert [(s.title, len(s.items)) for s in by_title["Телевизоры"].sections] == [("➡ Саундбары", 4), ("➡ Телевизоры", 1)]
    assert by_title["iPhone 18 Pro"].sections[0].items[0].price == 115500  # rich_message
    assert "iPhone Duo" not in by_title  # без цен категории нет

    skipped = build_catalog(export, (), DEFAULT_EXCLUDE_WORDS, ("телевизоры",))
    assert "Телевизоры" not in {c.title for c in skipped.categories}


def test_merge_categories():
    export = {"name": "S", "type": "private_channel", "id": 1, "messages": [
        {"id": 10, "type": "message", "text": "Ноутбуки\nAcer Aspire 3 — 66 400"},
        {"id": 11, "type": "message", "text": "Игровые\nMSI Raider 18 — 555 300"},
        {"id": 20, "type": "message", "text": "Меню", "inline_bot_buttons": [
            [{"type": "url", "text": "Ноутбуки", "data": "https://t.me/S/10"}],
            [{"type": "url", "text": "Игровые Ноутбуки", "data": "https://t.me/S/11"}]]},
    ]}
    catalog = build_catalog(export, merge={"Игровые Ноутбуки": "Ноутбуки"})
    assert [c.title for c in catalog.categories] == ["Ноутбуки"]
    assert [s.title for s in catalog.categories[0].sections] == ["Ноутбуки", "Игровые"]


def test_only_word_and_merge_into_existing_category():
    export = {"name": "S", "type": "private_channel", "id": 1, "messages": [
        {"id": 10, "type": "message", "text": "Пылесосы\n➡ Dyson Пылесосы\nV8 SV25 Absolute — 30 000\n"
                                              "Роботы\nDreame L10 — 40 000\nDyson Gen5 — 60 000"},
        {"id": 11, "type": "message", "text": "Фены\nHD16 Red Velvet — 45 000"},
        {"id": 12, "type": "message", "text": "Быт\nГазонокосилка Champion — 18 800"},
        {"id": 20, "type": "message", "text": "Меню", "inline_bot_buttons": [
            [{"type": "url", "text": "Пылесосы", "data": "https://t.me/S/10"}],
            [{"type": "url", "text": "Dyson", "data": "https://t.me/S/11"}],
            [{"type": "url", "text": "Быт.Техника", "data": "https://t.me/S/12"}]]},
    ]}
    catalog = build_catalog(export, skip_categories=("Быт.Техника",), merge={"Пылесосы": "Dyson"},
                            only={"Пылесосы": "dyson"})
    assert [c.title for c in catalog.categories] == ["Dyson"]
    names = [it.name for s in catalog.categories[0].sections for it in s.items]
    assert names == ["V8 SV25 Absolute", "Dyson Gen5", "HD16 Red Velvet"]


def test_category_icons():
    from app.keyboards import with_icon
    assert with_icon("iPhone 18 Pro") == "📱 iPhone 18 Pro"
    assert with_icon("Игровые Ноутбуки") == "🕹 Игровые Ноутбуки"
    assert with_icon("MacBook Pro 14/16") == "💻 MacBook Pro 14/16"
    assert with_icon("AW S11/Se3/Ultra") == "⌚ AW S11/Se3/Ultra"
    assert with_icon("iPad Air 11/13") == "📲 iPad Air 11/13"
    assert with_icon("📱 Своя") == "📱 Своя"
    assert with_icon("Игровые Консоли") == "🎮 Игровые Консоли"
    assert with_icon("Что-то новое") == "Что-то новое"
