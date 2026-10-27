import json
import os
import sqlite3
import time
from dataclasses import dataclass

from .parser import Section
from .pricing import apply_markup

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id             INTEGER PRIMARY KEY,
    title          TEXT NOT NULL,
    sort           INTEGER NOT NULL DEFAULT 0,
    markup_pct     REAL,
    markup_fixed   INTEGER,
    source_chat_id INTEGER,
    source_msg_id  INTEGER,
    updated_at     INTEGER
);
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    section     TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    base_price  INTEGER NOT NULL,
    sort        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS items_category ON items(category_id, sort);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Корзина ссылается на позицию по названию, а не по id: при обновлении прайса
-- позиции пересоздаются, а товар в корзине должен остаться.
CREATE TABLE IF NOT EXISTS cart (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    category_id INTEGER NOT NULL,
    section     TEXT NOT NULL,
    name        TEXT NOT NULL,
    note        TEXT NOT NULL,
    qty         INTEGER NOT NULL DEFAULT 1,
    UNIQUE (user_id, category_id, section, name, note)
);
-- Статистика посещений: кто заходил и в какие дни (день — по Москве, «2026-10-24»)
CREATE TABLE IF NOT EXISTS visitors (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    full_name  TEXT NOT NULL DEFAULT '',
    first_day  TEXT NOT NULL,
    last_seen  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS visits (
    user_id INTEGER NOT NULL,
    day     TEXT NOT NULL,
    actions INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS category_views (
    category_id INTEGER NOT NULL,
    day         TEXT NOT NULL,
    views       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (category_id, day)
);
CREATE TABLE IF NOT EXISTS orders (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    user_name  TEXT NOT NULL,
    username   TEXT,
    phone      TEXT NOT NULL,
    delivery   TEXT NOT NULL,
    items      TEXT NOT NULL,
    total      INTEGER NOT NULL,
    cost       INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'new',
    created_at INTEGER NOT NULL
);
-- Б/у устройства, принятые в трейд-ин и проверенные мастерами; цена — без наценки.
-- status: active — на витрине, reserved — в оформленном заказе, sold — продано, hidden — снято
CREATE TABLE IF NOT EXISTS used_items (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,
    model      TEXT NOT NULL,
    battery    TEXT NOT NULL DEFAULT '',
    condition  TEXT NOT NULL DEFAULT '',
    comment    TEXT NOT NULL DEFAULT '',
    price      INTEGER NOT NULL,
    cost       INTEGER,
    photos     TEXT NOT NULL DEFAULT '[]',
    status     TEXT NOT NULL DEFAULT 'active',
    created_at INTEGER NOT NULL
);
"""

USED_LABEL = "♻️ Б/у"


@dataclass
class CartLine:
    cart_id: int
    label: str
    category: str
    section: str
    qty: int
    price: int | None  # цена продажи сейчас; None — позиции больше нет в прайсе
    cost: int | None   # закупка
    used_id: int | None = None  # б/у устройство — единственный экземпляр

    @property
    def path(self) -> str:
        """«Очки › Wayfarer» — откуда товар, чтобы по короткому названию было понятно, что это."""
        parts = [self.category]
        if self.section and self.section.lower() != self.category.lower():
            parts.append(self.section)
        return " › ".join(p for p in parts if p)

    @property
    def available(self) -> bool:
        return self.price is not None


@dataclass
class ImportDiff:
    total: int
    previous: int
    added: list[tuple[str, int]]
    removed: list[tuple[str, int]]
    changed: list[tuple[str, int, int]]  # имя, было, стало


def _item_label(section: str, name: str, note: str) -> str:
    # section в подписи не нужен: название позиции и так содержит модель
    return " ".join(p for p in (name, note) if p)


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        # корзина из версии без б/у: добавляем ссылку на устройство
        if "used_id" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(cart)")}:
            self.conn.execute("ALTER TABLE cart ADD COLUMN used_id INTEGER")

    # --- настройки ---
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def init_defaults(self, defaults: dict) -> None:
        for key, value in defaults.items():
            if self.get_setting(key) is None:
                self.set_setting(key, value)

    def markup(self, category: sqlite3.Row | None = None) -> tuple[float, int, int]:
        pct = float(self.get_setting("markup_pct", "0"))
        fixed = int(self.get_setting("markup_fixed", "0"))
        step = int(self.get_setting("round_step", "100"))
        if category is not None:
            if category["markup_pct"] is not None:
                pct = category["markup_pct"]
            if category["markup_fixed"] is not None:
                fixed = category["markup_fixed"]
        return pct, fixed, step

    # --- категории ---
    def categories(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM items i WHERE i.category_id = c.id) AS n_items "
            "FROM categories c ORDER BY c.sort, c.id"
        ).fetchall()

    def category(self, cat_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM categories WHERE id = ?", (cat_id,)).fetchone()

    def add_category(self, title: str) -> int:
        with self.conn:
            sort = self.conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM categories").fetchone()[0]
            cur = self.conn.execute("INSERT INTO categories(title, sort) VALUES(?, ?)", (title, sort))
        return cur.lastrowid

    def find_by_source(self, chat_id: int, msg_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM categories WHERE source_chat_id = ? AND source_msg_id = ?", (chat_id, msg_id)
        ).fetchone()

    def set_source(self, cat_id: int, chat_id: int, msg_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE categories SET source_chat_id = NULL, source_msg_id = NULL "
                "WHERE source_chat_id = ? AND source_msg_id = ?",
                (chat_id, msg_id),
            )
            self.conn.execute(
                "UPDATE categories SET source_chat_id = ?, source_msg_id = ? WHERE id = ?", (chat_id, msg_id, cat_id)
            )

    def rename_category(self, cat_id: int, title: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE categories SET title = ? WHERE id = ?", (title, cat_id))

    def delete_category(self, cat_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))

    def set_category_markup(self, cat_id: int, pct: float | None, fixed: int | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE categories SET markup_pct = ?, markup_fixed = ? WHERE id = ?", (pct, fixed, cat_id)
            )

    def move_category(self, cat_id: int, position: int) -> None:
        ids = [r["id"] for r in self.categories() if r["id"] != cat_id]
        position = max(1, min(position, len(ids) + 1))
        ids.insert(position - 1, cat_id)
        with self.conn:
            for sort, cid in enumerate(ids, start=1):
                self.conn.execute("UPDATE categories SET sort = ? WHERE id = ?", (sort, cid))

    # --- позиции ---
    def sections(self, cat_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT section FROM items WHERE category_id = ? GROUP BY section ORDER BY MIN(sort)", (cat_id,)
        ).fetchall()
        return [r["section"] for r in rows]

    def items(self, cat_id: int, section: str | None = None) -> list[sqlite3.Row]:
        if section is None:
            return self.conn.execute(
                "SELECT * FROM items WHERE category_id = ? ORDER BY sort", (cat_id,)
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM items WHERE category_id = ? AND section = ? ORDER BY sort", (cat_id, section)
        ).fetchall()

    def replace_items(self, cat_id: int, sections: list[Section]) -> ImportDiff:
        """Полностью заменяет прайс категории и возвращает, что изменилось."""
        # Сравниваем по названию и примечанию, без раздела: если поставщик переименовал
        # заголовок раздела, товар не «пропал и появился», а остался тем же.
        old = {}
        for r in self.items(cat_id):
            old[(r["name"], r["note"])] = r["base_price"]

        new = {}
        rows = []
        for s in sections:
            for it in s.items:
                new[(it.name, it.note)] = it.price
                rows.append((cat_id, s.title, it.name, it.note, it.price, len(rows)))

        with self.conn:
            self.conn.execute("DELETE FROM items WHERE category_id = ?", (cat_id,))
            self.conn.executemany(
                "INSERT INTO items(category_id, section, name, note, base_price, sort) VALUES(?, ?, ?, ?, ?, ?)", rows
            )
            self.conn.execute("UPDATE categories SET updated_at = ? WHERE id = ?", (int(time.time()), cat_id))

        return ImportDiff(
            total=len(rows),
            previous=len(old),
            added=[(_item_label("", *k), p) for k, p in new.items() if k not in old],
            removed=[(_item_label("", *k), p) for k, p in old.items() if k not in new],
            changed=[(_item_label("", *k), old[k], p) for k, p in new.items() if k in old and old[k] != p],
        )

    def guess_category(self, sections: list[Section]) -> int | None:
        """Ищет категорию, в которой уже лежат позиции с такими же названиями."""
        names = {it.name for s in sections for it in s.items}
        if not names:
            return None
        best_id, best_score = None, 0.0
        for cat in self.categories():
            existing = {r["name"] for r in self.items(cat["id"])}
            if not existing:
                continue
            score = len(names & existing) / len(names)
            if score > best_score:
                best_id, best_score = cat["id"], score
        return best_id if best_score >= 0.3 else None

    def item(self, item_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()

    def sell_price(self, item: sqlite3.Row, category: sqlite3.Row | None = None) -> int:
        category = category or self.category(item["category_id"])
        return apply_markup(item["base_price"], *self.markup(category))

    # --- корзина ---
    def add_to_cart(self, user_id: int, item: sqlite3.Row) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO cart(user_id, category_id, section, name, note) VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, category_id, section, name, note) DO UPDATE SET qty = qty + 1",
                (user_id, item["category_id"], item["section"], item["name"], item["note"]),
            )

    def cart_count(self, user_id: int) -> int:
        return self.conn.execute("SELECT COALESCE(SUM(qty), 0) FROM cart WHERE user_id = ?", (user_id,)).fetchone()[0]

    def cart_lines(self, user_id: int) -> list[CartLine]:
        rows = self.conn.execute(
            "SELECT c.*, i.base_price, cat.title AS cat_title, cat.markup_pct, cat.markup_fixed, "
            "u.price AS used_price, u.cost AS used_cost "
            "FROM cart c "
            "LEFT JOIN used_items u ON u.id = c.used_id AND u.status = 'active' "
            "LEFT JOIN categories cat ON cat.id = c.category_id "
            # без раздела: переименованный поставщиком раздел не должен «терять» товар в корзине
            "LEFT JOIN items i ON i.category_id = c.category_id "
            "  AND i.name = c.name AND i.note = c.note "
            "WHERE c.user_id = ? GROUP BY c.id ORDER BY c.id",
            (user_id,),
        ).fetchall()
        lines = []
        for r in rows:
            price = cost = None
            if r["used_id"] is not None:
                if r["used_price"] is not None:
                    price, cost = r["used_price"], r["used_cost"]
            elif r["base_price"] is not None:
                cost = r["base_price"]
                price = apply_markup(cost, *self.markup(r))
            lines.append(
                CartLine(
                    cart_id=r["id"],
                    label=_item_label(r["section"], r["name"], r["note"]),
                    category=USED_LABEL if r["used_id"] is not None else (r["cat_title"] or ""),
                    section=r["section"],
                    qty=r["qty"],
                    price=price,
                    cost=cost,
                    used_id=r["used_id"],
                )
            )
        return lines

    def remove_cart_line(self, user_id: int, cart_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM cart WHERE id = ? AND user_id = ?", (cart_id, user_id))

    def clear_cart(self, user_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM cart WHERE user_id = ?", (user_id,))

    # --- заказы ---
    def create_order(self, user_id: int, user_name: str, username: str | None, phone: str, delivery: str,
                     lines: list[CartLine]) -> int:
        items = [
            {"name": l.label, "qty": l.qty, "price": l.price, "cost": l.cost, "path": l.path}
            | ({"used_id": l.used_id} if l.used_id is not None else {})
            for l in lines if l.available
        ]
        total = sum(i["qty"] * i["price"] for i in items)
        # закупка б/у может быть не указана — тогда считаем её равной цене, чтобы не завышать наценку
        cost = sum(i["qty"] * (i["price"] if i["cost"] is None else i["cost"]) for i in items)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO orders(user_id, user_name, username, phone, delivery, items, total, cost, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, user_name, username, phone, delivery, json.dumps(items, ensure_ascii=False), total, cost,
                 int(time.time())),
            )
            self.conn.execute("DELETE FROM cart WHERE user_id = ?", (user_id,))
            # б/у устройство в одном экземпляре: пока заказ не подтверждён или не отменён, его не видно на витрине
            for i in items:
                if "used_id" in i:
                    self.conn.execute("UPDATE used_items SET status = 'reserved' WHERE id = ? AND status = 'active'",
                                      (i["used_id"],))
        return cur.lastrowid

    def order(self, order_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()

    def order_items(self, order: sqlite3.Row) -> list[dict]:
        return json.loads(order["items"])

    def set_order_status(self, order_id: int, status: str) -> None:
        order = self.order(order_id)
        with self.conn:
            self.conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
            # подтверждён — б/у продано; отменён — снова на витрине
            used_status = {"ok": "sold", "no": "active"}.get(status)
            for i in self.order_items(order) if order and used_status else ():
                if i.get("used_id") is not None:
                    self.conn.execute("UPDATE used_items SET status = ? WHERE id = ? AND status IN ('reserved', 'sold')",
                                      (used_status, i["used_id"]))

    def recent_orders(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def find_category_by_title(self, title: str) -> sqlite3.Row | None:
        for c in self.categories():
            if c["title"].strip().lower() == title.strip().lower():
                return c
        return None

    # --- б/у ---
    def add_used(self, kind: str, model: str, battery: str, condition: str, comment: str, price: int,
                 cost: int | None, photos: list[str]) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO used_items(kind, model, battery, condition, comment, price, cost, photos, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, model, battery, condition, comment, price, cost, json.dumps(photos), int(time.time())),
            )
        return cur.lastrowid

    def used(self, used_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM used_items WHERE id = ?", (used_id,)).fetchone()

    def used_items(self, kind: str | None = None, status: str = "active") -> list[sqlite3.Row]:
        if kind is None:
            return self.conn.execute(
                "SELECT * FROM used_items WHERE status = ? ORDER BY id DESC", (status,)).fetchall()
        return self.conn.execute(
            "SELECT * FROM used_items WHERE status = ? AND kind = ? ORDER BY id DESC", (status, kind)).fetchall()

    def used_kinds(self) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            "SELECT kind, COUNT(*) AS n FROM used_items WHERE status = 'active' GROUP BY kind ORDER BY MIN(id)"
        ).fetchall()
        return [(r["kind"], r["n"]) for r in rows]

    def used_photos(self, row: sqlite3.Row) -> list[str]:
        return json.loads(row["photos"])

    def set_used_status(self, used_id: int, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE used_items SET status = ? WHERE id = ?", (status, used_id))

    def set_used_price(self, used_id: int, price: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE used_items SET price = ? WHERE id = ?", (price, used_id))

    def add_used_to_cart(self, user_id: int, row: sqlite3.Row) -> None:
        """Устройство одно — повторное нажатие не увеличивает количество."""
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO cart(user_id, category_id, section, name, note, used_id) VALUES(?, 0, ?, ?, ?, ?)",
                (user_id, row["kind"], row["model"], f"б/у №{row['id']}", row["id"]),
            )

    # --- статистика ---
    def track_visit(self, user_id: int, username: str | None, full_name: str, day: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO visitors(user_id, username, full_name, first_day, last_seen) VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, full_name = excluded.full_name, "
                "last_seen = excluded.last_seen",
                (user_id, username, full_name, day, int(time.time())),
            )
            self.conn.execute(
                "INSERT INTO visits(user_id, day) VALUES(?, ?) "
                "ON CONFLICT(user_id, day) DO UPDATE SET actions = actions + 1",
                (user_id, day),
            )

    def track_category_view(self, cat_id: int, day: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO category_views(category_id, day) VALUES(?, ?) "
                "ON CONFLICT(category_id, day) DO UPDATE SET views = views + 1",
                (cat_id, day),
            )

    def visitor_stats(self, since_day: str) -> tuple[int, int, int]:
        """(новых, активных, действий) с указанного дня включительно."""
        new = self.conn.execute("SELECT COUNT(*) FROM visitors WHERE first_day >= ?", (since_day,)).fetchone()[0]
        active, actions = self.conn.execute(
            "SELECT COUNT(DISTINCT user_id), COALESCE(SUM(actions), 0) FROM visits WHERE day >= ?", (since_day,)
        ).fetchone()
        return new, active, actions

    def total_visitors(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM visitors").fetchone()[0]

    def order_stats(self, since_ts: int) -> tuple[int, int, int]:
        """(заказов, подтверждено, сумма подтверждённых) с момента since_ts."""
        return tuple(self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(status = 'ok'), 0), COALESCE(SUM(CASE WHEN status = 'ok' THEN total END), 0) "
            "FROM orders WHERE created_at >= ?",
            (since_ts,),
        ).fetchone())

    def top_categories(self, since_day: str, limit: int = 5) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            "SELECT c.title, SUM(v.views) AS n FROM category_views v JOIN categories c ON c.id = v.category_id "
            "WHERE v.day >= ? GROUP BY v.category_id ORDER BY n DESC LIMIT ?",
            (since_day, limit),
        ).fetchall()
        return [(r["title"], r["n"]) for r in rows]

    def recent_visitors(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM visitors ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
