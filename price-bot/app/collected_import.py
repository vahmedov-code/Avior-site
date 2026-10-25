"""Серверная часть сборщика (Linux, рядом с ботом): принимает JSON, присланный
с Windows-сервера, обновляет цены и сообщает владельцу, что изменилось."""
from __future__ import annotations

import base64
import html

from .catalog_import import catalog_filters, import_catalog, preview_removals, supplier
from .config import Config
from .db import DB
from .pricing import apply_markup, fmt_price
from .telegram_export import TITLE_JUNK_RE, export_from_posts, load_any

RESULT_FORMAT = "avior-collected-1"
# Если сборщик выкидывает/переименовывает больше этой доли позиций — не применяем:
# скорее всего, веб-версия Telegram отдала текст не так, как обычно
MAX_REMOVED_SHARE = 0.1


def _norm(title: str) -> str:
    return TITLE_JUNK_RE.sub("", title).strip().lower()


def format_changes(db: DB, results, limit: int = 25) -> str:
    """Короткий отчёт: только изменившиеся цены, с ценой продажи."""
    total = sum(len(r.diff.changed) + len(r.diff.added) + len(r.diff.removed) for r in results)
    lines = [f"🤖 <b>Сборщик проверил прайс</b>: {sum(r.diff.total for r in results)} позиций, "
             + (f"изменений: {total}" if total else "изменений нет")]
    shown = 0
    for r in results:
        d = r.diff
        if not (d.changed or d.added or d.removed):
            continue
        pct, fixed, step = db.markup(db.category(r.cat_id))
        block = []
        for name, old, new in d.changed:
            if shown < limit:
                arrow = "🔺" if new > old else "🔻"
                block.append(f"{arrow} {html.escape(name)}: {fmt_price(old)} → {fmt_price(new)} "
                             f"(у вас {fmt_price(apply_markup(new, pct, fixed, step))})")
            shown += 1
        if d.added:
            block.append(f"➕ новых: {len(d.added)}")
        if d.removed:
            block.append(f"➖ пропало: {len(d.removed)}")
        if block:  # не оставляем пустой заголовок, когда строки цен не влезли в лимит
            lines += ["", f"<b>{html.escape(r.title)}</b>", *block]
    if shown > limit:
        lines.append(f"\n…и ещё {shown - limit} изменений цен. Весь прайс — /excel")
    return "\n".join(lines)


def handle(db: DB, cfg: Config, data: dict, report_always: bool = False) -> tuple[str | None, bytes | None]:
    """Возвращает (текст для владельца или None, если сообщать нечего; скриншот)."""
    if data.get("format") != RESULT_FORMAT:
        return "⚠️ Сборщик прислал файл непонятного формата. Цены не изменены.", None
    if not data.get("ok"):
        shot = base64.b64decode(data["screenshot"]) if data.get("screenshot") else None
        return (f"⚠️ Сборщик не смог прочитать канал: {html.escape(data.get('error', ''))}. "
                "Цены не изменены." + (" Вот что видел браузер:" if shot else "")), shot

    info = supplier(db)
    if not info:
        return "Сборщик: сначала один раз отправьте боту экспорт канала (result.json).", None
    # Номера постов — целые; «0.01» и т.п. — служебные элементы страницы Telegram Web
    posts = {int(i): t for i, t in data["posts"].items() if str(i).isdigit()}
    catalog = load_any(export_from_posts(info, posts), **catalog_filters(db, cfg))
    if not catalog.categories:
        return "⚠️ Сборщик прочитал канал, но не нашёл цен. Цены не изменены.", None
    previous, removed, added = preview_removals(db, catalog)
    if previous and len(removed) > MAX_REMOVED_SHARE * previous:
        sample_old = "\n".join("• " + html.escape(x[:70]) for x in sorted(removed)[:3])
        sample_new = "\n".join("• " + html.escape(x[:70]) for x in sorted(added)[:3])
        return (f"⚠️ Сборщик прочитал канал, но {len(removed)} из {previous} позиций выглядят иначе, чем сейчас. "
                "Похоже на сбой чтения — <b>цены не изменены</b>.\n\nБыло, например:\n"
                f"{sample_old}\nСтало:\n{sample_new}"), None
    results = import_catalog(db, cfg, catalog)

    # Новые кнопки в меню поставщика: сравниваем со всеми кнопками, включая служебные (VK, Trade-In…).
    # В меню, запомненном старой версией бота, служебных кнопок нет — тогда проверку пропускаем.
    new_buttons = []
    if "all_buttons" in info:
        known = {_norm(t) for t in info["all_buttons"]} | {_norm(t) for t, _ in info["menu"]}
        new_buttons = [b for b in data.get("menu_buttons", []) if _norm(b) and _norm(b) not in known]

    changed = sum(len(r.diff.changed) + len(r.diff.added) + len(r.diff.removed) for r in results)
    if not (changed or new_buttons or report_always):
        return None, None
    text = format_changes(db, results)
    if new_buttons:
        text += ("\n\n🆕 В меню поставщика новые кнопки: " + html.escape(", ".join(new_buttons))
                 + ". Чтобы я их подхватил, один раз отправьте боту свежий экспорт канала.")
    return text[:4000], None
