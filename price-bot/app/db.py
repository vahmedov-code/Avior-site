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
"""


@dataclass
class CartLine:
    cart_id: int
    label: str
    category: str
    section: str
    qty: int
    price: int | None  # цена продажи сейчас; None — позиции больше нет в прайсе
    cost: int | None   # закупка

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
        old = {}
        for r in self.items(cat_id):
            old[(r["section"], r["name"], r["note"])] = r["base_price"]

        new = {}
        rows = []
        for s in sections:
            for it in s.items:
                new[(s.title, it.name, it.note)] = it.price
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
            added=[(_item_label(*k), p) for k, p in new.items() if k not in old],
            removed=[(_item_label(*k), p) for k, p in old.items() if k not in new],
            changed=[(_item_label(*k), old[k], p) for k, p in new.items() if k in old and old[k] != p],
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
            "SELECT c.*, i.base_price, cat.title AS cat_title, cat.markup_pct, cat.markup_fixed "
            "FROM cart c "
            "LEFT JOIN categories cat ON cat.id = c.category_id "
            "LEFT JOIN items i ON i.category_id = c.category_id AND i.section = c.section "
            "  AND i.name = c.name AND i.note = c.note "
            "WHERE c.user_id = ? GROUP BY c.id ORDER BY c.id",
            (user_id,),
        ).fetchall()
        lines = []
        for r in rows:
            price = cost = None
            if r["base_price"] is not None:
                cost = r["base_price"]
                price = apply_markup(cost, *self.markup(r))
            lines.append(
                CartLine(
                    cart_id=r["id"],
                    label=_item_label(r["section"], r["name"], r["note"]),
                    category=r["cat_title"] or "",
                    section=r["section"],
                    qty=r["qty"],
                    price=price,
                    cost=cost,
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
            for l in lines if l.available
        ]
        total = sum(i["qty"] * i["price"] for i in items)
        cost = sum(i["qty"] * i["cost"] for i in items)
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO orders(user_id, user_name, username, phone, delivery, items, total, cost, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, user_name, username, phone, delivery, json.dumps(items, ensure_ascii=False), total, cost,
                 int(time.time())),
            )
            self.conn.execute("DELETE FROM cart WHERE user_id = ?", (user_id,))
        return cur.lastrowid

    def order(self, order_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()

    def order_items(self, order: sqlite3.Row) -> list[dict]:
        return json.loads(order["items"])

    def set_order_status(self, order_id: int, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))

    def recent_orders(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def find_category_by_title(self, title: str) -> sqlite3.Row | None:
        for c in self.categories():
            if c["title"].strip().lower() == title.strip().lower():
                return c
        return None
