"""Команды владельца: импорт прайса пересылкой поста, наценки, категории."""
import html
import io
import json
from datetime import date

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message, MessageOriginChannel,
                           TelegramObject)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import keyboards as kb
from .alerts import send_price_alerts
from .channel import update_post
from .catalog_import import (catalog_filters, create_category, exclude_words, import_catalog, merge_categories,
                             only_words, remember_supplier, set_exclude_words, set_merge_categories, set_only_words,
                             set_skip_categories, skip_categories)
from .config import Config
from .db import DB, ImportDiff
from .handlers_user import ORDER_STATUS, render_order_for_admin
from .excel import build_xlsx
from .parser import count_items, parse_post
from .pricing import apply_markup, fmt_price
from .rules import filter_sections
from .stats import format_stats, format_users
from .telegram_export import load_any

router = Router(name="admin")

HELP = """<b>Управление ботом</b>

<b>Обновить цены:</b>
• перешлите сюда пост с прайсом из группы поставщика — бот спросит категорию (один раз) и запомнит;
• или отправьте файл экспорта группы <code>result.json</code> (или <code>catalog.json</code> из manage.py) — обновятся все категории сразу.
/excel — выгрузить текущий прайс в Excel

<b>Что не продаём</b> (чехлы, зарядки, доп. гарантия)
/exclude — список слов-исключений
/exclude add ремешок — добавить слово
/exclude del стекло — убрать слово
/skipcat — категории поставщика, которые не загружаем (сейчас: телевизоры, быт. техника)
/skipcat add Пылесосы · /skipcat del Телевизоры
/mergecat — какие категории поставщика объединяем (сейчас: игровые ноутбуки → Ноутбуки, пылесосы → Dyson)
/mergecat add Игровые Консоли = Гаджеты · /mergecat del Игровые Консоли
/onlycat — из каких категорий берём только один бренд (сейчас: пылесосы → только Dyson)
/onlycat add Пылесосы = dyson · /onlycat del Пылесосы

<b>Наценка</b>
/markup — текущая наценка
/markup 10 — +10% ко всем ценам
/markup 10 500 — +10% и ещё +500 ₽
/round 100 — округлять вверх до 100 ₽ (1 — без округления)
/catmarkup 3 15 — своя наценка для категории №3 (+15%)
/catmarkup 3 15 1000 — +15% и +1000 ₽
/catmarkup 3 off — категория №3 снова по общей наценке

<b>Б/у устройства</b> (трейд-ин, цена без наценки)
/bu — список всех устройств: ➕ добавить, ✅ продано (убрать с витрины)
Добавление — по шагам: модель → аккумулятор → состояние → комментарий → цена → фото. Логотип на фото бот ставит сам.

<b>Клиенты</b>
/send — рассылка всем, кто заходил в бота (текст или фото, с превью)
/repair — статусы ремонта по номеру квитанции: /repair 1234 → кнопки статусов
/channel @канал — публиковать б/у в Telegram-канал (/channel off — выключить)
Заявки на трейд-ин и запись на ремонт приходят сюда; на трейд-ин отвечайте кнопкой «💬 Предложить цену».

<b>Статистика</b>
/stats — сколько людей заходило, заказы, популярные категории

<b>Заказы</b>
Новые заказы приходят сюда с кнопками ✅ Подтвердить / ❌ Отменить — клиент получит уведомление.
/orders — последние 10 заказов

<b>Категории</b>
/cats — список с номерами
/newcat Название — создать пустую
/rename 3 Новое название
/move 3 1 — поставить категорию №3 первой
/delcat 3 — удалить категорию №3 с ценами"""


class IsAdmin(Filter):
    async def __call__(self, event: TelegramObject, cfg: Config) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and user.id in cfg.admin_ids


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class ImportFlow(StatesGroup):
    choose = State()
    new_title = State()


def markup_text(pct: float, fixed: int) -> str:
    parts = [f"+{pct:g}%"]
    if fixed:
        parts.append(f"+{fmt_price(fixed)}")
    return " и ".join(parts)


def format_report(db: DB, cat_id: int, diff: ImportDiff, skipped: int = 0) -> str:
    cat = db.category(cat_id)
    pct, fixed, step = db.markup(cat)
    sell = lambda p: fmt_price(apply_markup(p, pct, fixed, step))  # noqa: E731
    lines = [
        f"✅ <b>{html.escape(cat['title'])}</b> обновлена",
        f"Позиций: {diff.total} (было {diff.previous})",
        f"Наценка: {markup_text(pct, fixed)}, округление до {step} ₽",
    ]
    if skipped:
        lines.append(f"🚫 пропущено (чехлы, зарядки, гарантия…): {skipped}")
    if diff.previous:
        lines.append(f"➕ новых: {len(diff.added)}  ➖ убрано: {len(diff.removed)}  💱 цена изменилась: {len(diff.changed)}")
    for name, old, new in diff.changed[:15]:
        arrow = "🔺" if new > old else "🔻"
        lines.append(f"{arrow} {html.escape(name)}: {fmt_price(old)} → {fmt_price(new)} (у вас {sell(new)})")
    if len(diff.changed) > 15:
        lines.append(f"…и ещё {len(diff.changed) - 15}")
    sections = db.sections(cat_id)
    lines.append("\nРазделы: " + html.escape(", ".join(s or "Разное" for s in sections)))
    return "\n".join(lines)


def source_of(message: Message) -> tuple[int, int] | None:
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        return origin.chat.id, origin.message_id
    return None


class HasPrices(Filter):
    """Срабатывает на пересланный пост (или длинный текст), в котором нашлись цены."""

    async def __call__(self, message: Message, cfg: Config) -> bool | dict:
        text = message.text or message.caption
        if not text or text.startswith("/"):
            return False
        sections = parse_post(text, cfg.block_words)
        n = count_items(sections)
        if n == 0 or (message.forward_origin is None and n < 3):
            return False
        return {"sections": sections, "post_text": text}


def prepare(db: DB, cfg: Config, text: str):
    return filter_sections(parse_post(text, cfg.block_words), exclude_words(db, cfg))


# --- импорт прайса ---

@router.message(StateFilter(None, ImportFlow.choose, ImportFlow.new_title), HasPrices())
async def on_price_post(message: Message, state: FSMContext, db: DB, cfg: Config, post_text: str, **_):
    sections, skipped = prepare(db, cfg, post_text)
    if not sections:
        await message.answer(f"В посте только то, что мы не продаём ({skipped} поз.: чехлы, зарядки, гарантия…). "
                             "Пропускаю. Список исключений — /exclude")
        return
    src = source_of(message)
    if src:
        cat = db.find_by_source(*src)
        if cat:
            diff = db.replace_items(cat["id"], sections)
            await state.clear()
            await message.answer(format_report(db, cat["id"], diff, skipped))
            return

    await state.set_state(ImportFlow.choose)
    await state.update_data(post_text=post_text, source=src)
    titles = ", ".join(s.title or "Разное" for s in sections[:6])
    await message.answer(
        f"Нашёл {count_items(sections)} цен в {len(sections)} разделах ({html.escape(titles)}).\n"
        "В какую категорию положить?",
        reply_markup=kb.choose_category(db.categories(), db.guess_category(sections)),
    )


async def finish_import(message: Message, state: FSMContext, db: DB, cfg: Config, cat_id: int):
    data = await state.get_data()
    await state.clear()
    sections, skipped = prepare(db, cfg, data["post_text"])
    if data.get("source"):
        db.set_source(cat_id, *data["source"])
    diff = db.replace_items(cat_id, sections)
    await message.answer(format_report(db, cat_id, diff, skipped))
    await notify_watchers(message, db)


async def notify_watchers(message: Message, db: DB) -> None:
    """Клиентам — что из их корзины подешевело; владельцу — сколько таких уведомлений ушло."""
    sent = await send_price_alerts(message.bot, db)
    if sent:
        await message.answer(f"🔔 Клиентам сообщено о снижении цен в корзине: {sent}")


@router.callback_query(StateFilter(ImportFlow.choose), F.data.startswith("imp:"))
async def cb_import_into(call: CallbackQuery, state: FSMContext, db: DB, cfg: Config):
    cat_id = int(call.data.split(":")[1])
    if not db.category(cat_id):
        await call.answer("Категория не найдена", show_alert=True)
        return
    await call.message.edit_reply_markup(reply_markup=None)
    await finish_import(call.message, state, db, cfg, cat_id)
    await call.answer()


@router.callback_query(StateFilter(ImportFlow.choose), F.data == "impnew")
async def cb_import_new(call: CallbackQuery, state: FSMContext):
    await state.set_state(ImportFlow.new_title)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("Как назвать категорию? Например: iPhone 18 Pro")
    await call.answer()


@router.message(StateFilter(ImportFlow.new_title), F.text, ~F.text.startswith("/"))
async def on_new_title(message: Message, state: FSMContext, db: DB, cfg: Config):
    cat_id = create_category(db, cfg, message.text)
    await finish_import(message, state, db, cfg, cat_id)


@router.callback_query(F.data == "impcancel")
async def cb_import_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Импорт отменён.")
    await call.answer()


# --- команды ---

@router.message(Command("admin", "help"))
async def cmd_help(message: Message, db: DB):
    pct, fixed, step = db.markup()
    await message.answer(f"{HELP}\n\nСейчас: {markup_text(pct, fixed)}, округление до {step} ₽")


@router.message(Command("markup"))
async def cmd_markup(message: Message, command: CommandObject, db: DB):
    args = (command.args or "").replace("%", "").split()
    if args:
        try:
            pct = float(args[0].replace(",", "."))
            fixed = int(args[1]) if len(args) > 1 else 0
        except ValueError:
            await message.answer("Пример: /markup 10 или /markup 10 500")
            return
        db.set_setting("markup_pct", pct)
        db.set_setting("markup_fixed", fixed)
    pct, fixed, step = db.markup()
    await message.answer(f"Общая наценка: {markup_text(pct, fixed)}, округление до {step} ₽")


@router.message(Command("round"))
async def cmd_round(message: Message, command: CommandObject, db: DB):
    try:
        step = int(command.args or "")
        if step < 1:
            raise ValueError
    except ValueError:
        await message.answer("Пример: /round 100 (или /round 1 — без округления)")
        return
    db.set_setting("round_step", step)
    await message.answer(f"Округление: вверх до {step} ₽")


def parse_cat_id(command: CommandObject, db: DB):
    parts = (command.args or "").split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        return None, parts
    return db.category(int(parts[0])), parts


@router.message(Command("cats"))
async def cmd_cats(message: Message, db: DB):
    cats = db.categories()
    if not cats:
        await message.answer("Категорий пока нет. Перешлите сюда пост с прайсом.")
        return
    lines = ["<b>Категории</b> (№ — название — позиций — наценка)"]
    for c in cats:
        own = ""
        if c["markup_pct"] is not None or c["markup_fixed"] is not None:
            pct, fixed, _ = db.markup(c)
            own = f" — своя {markup_text(pct, fixed)}"
        lines.append(f"{c['id']}. {html.escape(c['title'])} — {c['n_items']}{own}")
    await message.answer("\n".join(lines))


@router.message(Command("newcat"))
async def cmd_newcat(message: Message, command: CommandObject, db: DB, cfg: Config):
    title = (command.args or "").strip()
    if not title:
        await message.answer("Пример: /newcat iPhone 18 Pro")
        return
    cat_id = create_category(db, cfg, title)
    await message.answer(f"Создана категория №{cat_id}: {html.escape(title)}")


@router.message(Command("rename"))
async def cmd_rename(message: Message, command: CommandObject, db: DB):
    cat, parts = parse_cat_id(command, db)
    if not cat or len(parts) < 2:
        await message.answer("Пример: /rename 3 iPhone 18 Pro")
        return
    db.rename_category(cat["id"], parts[1].strip()[:60])
    await message.answer("Готово.")


@router.message(Command("move"))
async def cmd_move(message: Message, command: CommandObject, db: DB):
    cat, parts = parse_cat_id(command, db)
    if not cat or len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Пример: /move 3 1 — поставить №3 на первое место")
        return
    db.move_category(cat["id"], int(parts[1]))
    await message.answer("Готово.")


@router.message(Command("delcat"))
async def cmd_delcat(message: Message, command: CommandObject, db: DB):
    cat, _ = parse_cat_id(command, db)
    if not cat:
        await message.answer("Пример: /delcat 3 (номера — в /cats)")
        return
    db.delete_category(cat["id"])
    await message.answer(f"Категория «{html.escape(cat['title'])}» удалена.")


@router.message(Command("catmarkup"))
async def cmd_catmarkup(message: Message, command: CommandObject, db: DB):
    cat, parts = parse_cat_id(command, db)
    rest = parts[1].replace("%", "").split() if len(parts) > 1 else []
    if not cat or not rest:
        await message.answer("Пример: /catmarkup 3 15, /catmarkup 3 15 1000 или /catmarkup 3 off")
        return
    if rest[0].lower() == "off":
        db.set_category_markup(cat["id"], None, None)
    else:
        try:
            pct = float(rest[0].replace(",", "."))
            fixed = int(rest[1]) if len(rest) > 1 else 0
        except ValueError:
            await message.answer("Пример: /catmarkup 3 15 1000")
            return
        db.set_category_markup(cat["id"], pct, fixed)
    pct, fixed, step = db.markup(db.category(cat["id"]))
    await message.answer(f"«{html.escape(cat['title'])}»: {markup_text(pct, fixed)}, округление до {step} ₽")


# --- заказы ---

@router.callback_query(F.data.startswith("o:"))
async def cb_order_status(call: CallbackQuery, bot: Bot, db: DB, cfg: Config):
    _, status, order_id = call.data.split(":")
    order = db.order(int(order_id))
    if not order:
        await call.answer("Заказ не найден", show_alert=True)
        return
    db.set_order_status(order["id"], status)
    if status == "ok":  # б/у из подтверждённого заказа — в канале помечаем «ПРОДАНО»
        for it in db.order_items(order):
            if "used_id" in it:
                await update_post(bot, db, cfg, it["used_id"], sold=True)
    await call.message.edit_text(render_order_for_admin(db, db.order(order["id"])), reply_markup=None)
    if status == "ok":
        text = (f"✅ Ваш заказ №{order['id']} подтверждён!\n"
                "Менеджер свяжется с вами, чтобы согласовать оплату и доставку.")
    else:
        text = (f"❌ Заказ №{order['id']} отменён.\n"
                "Если это ошибка или остались вопросы — напишите нам, контакты в меню бота.")
    try:
        await bot.send_message(order["user_id"], text)
    except (TelegramBadRequest, TelegramForbiddenError):
        await call.answer("Статус сохранён, но клиент заблокировал бота", show_alert=True)
        return
    await call.answer("Клиент уведомлён")


@router.message(Command("orders"))
async def cmd_orders(message: Message, db: DB):
    rows = db.recent_orders(10)
    if not rows:
        await message.answer("Заказов пока нет.")
        return
    lines = ["<b>Последние заказы</b>"]
    for o in rows:
        who = f"@{o['username']}" if o["username"] else html.escape(o["user_name"])
        lines.append(
            f"№{o['id']} · {ORDER_STATUS.get(o['status'], o['status'])} · {fmt_price(o['total'])} · "
            f"{who} · {html.escape(o['phone'])}"
        )
    await message.answer("\n".join(lines))


# --- исключения ---

@router.message(Command("exclude"))
async def cmd_exclude(message: Message, command: CommandObject, db: DB, cfg: Config):
    words = set(exclude_words(db, cfg))
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) == 2 and parts[0] in ("add", "del"):
        word = parts[1].strip().lower()
        if parts[0] == "add":
            words.add(word)
        else:
            words.discard(word)
        set_exclude_words(db, words)
    elif parts:
        await message.answer("Пример: /exclude add ремешок или /exclude del стекло")
        return
    await message.answer(
        "🚫 Не продаём позиции, в названии которых есть:\n"
        + html.escape(", ".join(sorted(words)))
        + "\n\nа также строки доп. гарантии «+1 месяц …». Применяется при следующем обновлении прайса."
    )


# --- файл экспорта и Excel ---

@router.message(F.document)
async def on_document(message: Message, bot: Bot, db: DB, cfg: Config):
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".json"):
        await message.answer("Пришлите файл <code>result.json</code> из экспорта Telegram Desktop "
                             "или <code>catalog.json</code> из manage.py.")
        return
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await message.answer("Файл больше 20 МБ — Telegram не даст боту его скачать. "
                             "Запустите на компьютере <code>python manage.py catalog result.json</code> "
                             "и пришлите получившийся маленький <code>catalog.json</code>.")
        return
    buf = io.BytesIO()
    await bot.download(doc, destination=buf)
    try:
        data = json.loads(buf.getvalue().decode("utf-8"))
        catalog = load_any(data, **catalog_filters(db, cfg))
    except (ValueError, UnicodeDecodeError) as e:
        await message.answer(f"Не получилось прочитать файл: {html.escape(str(e))}")
        return
    if not catalog.categories:
        await message.answer("В файле не нашёл постов с ценами.")
        return
    remember_supplier(db, data)
    results = import_catalog(db, cfg, catalog)
    stale = [c for c in db.categories() if c["id"] not in {r.cat_id for r in results} and c["n_items"]]
    await message.answer(format_bulk_report(results, stale), reply_markup=stale_keyboard(stale))
    await notify_watchers(message, db)


def format_bulk_report(results, stale=()) -> str:
    lines = [f"✅ <b>Прайс обновлён</b>: {len(results)} категорий, "
             f"{sum(r.diff.total for r in results)} позиций", ""]
    for r in results:
        mark = "🆕 " if r.created else ""
        extra = []
        if r.diff.changed:
            extra.append(f"💱 {len(r.diff.changed)}")
        if r.diff.previous and r.diff.added:
            extra.append(f"➕ {len(r.diff.added)}")
        if r.diff.removed:
            extra.append(f"➖ {len(r.diff.removed)}")
        if r.skipped:
            extra.append(f"🚫 {r.skipped}")
        lines.append(f"{mark}{r.cat_id}. {html.escape(r.title)} — {r.diff.total}" + (f" ({' '.join(extra)})" if extra else ""))
    if stale:
        lines += ["", "⚠️ <b>Не обновлялись</b> — их нет в файле (или они теперь пропускаются/объединены). "
                      "Удалить — кнопками ниже:"]
        lines += [f"{c['id']}. {html.escape(c['title'])} — {c['n_items']} поз." for c in stale]
    lines += ["", "💱 цена изменилась · ➕ новые · ➖ пропали · 🚫 пропущены (чехлы, зарядки…)",
              "Наценки категорий — /cats, прайс в Excel — /excel, статистика посещений — /stats"]
    return "\n".join(lines)


@router.message(Command("excel"))
async def cmd_excel(message: Message, db: DB):
    buf = io.BytesIO()
    rows = build_xlsx(db, buf)
    if not rows:
        await message.answer("Прайс пуст.")
        return
    await message.answer_document(
        BufferedInputFile(buf.getvalue(), filename=f"AVIOR_price_{date.today():%Y-%m-%d}.xlsx"),
        caption=f"Прайс AVIOR: {rows} позиций",
    )


@router.message(Command("skipcat"))
async def cmd_skipcat(message: Message, command: CommandObject, db: DB, cfg: Config):
    titles = set(skip_categories(db, cfg))
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) == 2 and parts[0] in ("add", "del"):
        title = parts[1].strip()
        if parts[0] == "add":
            titles.add(title)
        else:
            titles = {t for t in titles if t.lower() != title.lower()}
        set_skip_categories(db, titles)
    elif parts:
        await message.answer("Пример: /skipcat add Пылесосы или /skipcat del Телевизоры")
        return
    await message.answer(
        "🚫 Не загружаем категории поставщика: " + (html.escape(", ".join(sorted(titles))) or "—")
        + "\n\nНазвания — как на кнопках в меню поставщика. Уже загруженную категорию удалите через /cats и /delcat."
    )


@router.message(Command("mergecat"))
async def cmd_mergecat(message: Message, command: CommandObject, db: DB, cfg: Config):
    mapping = merge_categories(db, cfg)
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) == 2 and parts[0] == "add" and "=" in parts[1]:
        src, dst = (x.strip() for x in parts[1].split("=", 1))
        mapping = {k: v for k, v in mapping.items() if k.lower() != src.lower()}
        mapping[src] = dst
        set_merge_categories(db, mapping)
    elif len(parts) == 2 and parts[0] == "del":
        mapping = {k: v for k, v in mapping.items() if k.lower() != parts[1].strip().lower()}
        set_merge_categories(db, mapping)
    elif parts:
        await message.answer("Пример: /mergecat add Игровые Ноутбуки = Ноутбуки или /mergecat del Игровые Ноутбуки")
        return
    lines = [f"• {html.escape(k)} → {html.escape(v)}" for k, v in sorted(mapping.items())] or ["—"]
    await message.answer("🔗 Объединяем категории поставщика:\n" + "\n".join(lines)
                         + "\n\nПрименится при следующей загрузке прайса. Старую категорию удалите через /cats и /delcat.")


@router.message(Command("onlycat"))
async def cmd_onlycat(message: Message, command: CommandObject, db: DB, cfg: Config):
    mapping = only_words(db, cfg)
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) == 2 and parts[0] == "add" and "=" in parts[1]:
        src, word = (x.strip() for x in parts[1].split("=", 1))
        mapping = {k: v for k, v in mapping.items() if k.lower() != src.lower()}
        mapping[src] = word.lower()
        set_only_words(db, mapping)
    elif len(parts) == 2 and parts[0] == "del":
        mapping = {k: v for k, v in mapping.items() if k.lower() != parts[1].strip().lower()}
        set_only_words(db, mapping)
    elif parts:
        await message.answer("Пример: /onlycat add Пылесосы = dyson или /onlycat del Пылесосы")
        return
    lines = [f"• {html.escape(k)}: только «{html.escape(v)}»" for k, v in sorted(mapping.items())] or ["—"]
    await message.answer("🎯 Из этих категорий поставщика берём только позиции со словом:\n" + "\n".join(lines)
                         + "\n\nПрименится при следующей загрузке прайса.")


def stale_keyboard(stale):
    if not stale:
        return None
    kb = InlineKeyboardBuilder()
    for c in stale:
        kb.button(text=f"🗑 Удалить «{c['title']}»", callback_data=f"delcat:{c['id']}")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data.startswith("delcat:"))
async def cb_delcat(call: CallbackQuery, db: DB):
    cat = db.category(int(call.data.split(":")[1]))
    if cat:
        db.delete_category(cat["id"])
    # убираем нажатую кнопку, остальные оставляем
    rows = [row for row in (call.message.reply_markup.inline_keyboard if call.message.reply_markup else [])
            if row[0].callback_data != call.data]
    await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
    await call.answer(f"Удалено: {cat['title']}" if cat else "Уже удалено")


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: DB, cfg: Config):
    await message.answer(format_stats(db, cfg), reply_markup=stats_markup(db))


def stats_markup(db: DB) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"👥 Все пользователи ({db.total_visitors()})", callback_data="users:0")
    return b.as_markup()


@router.callback_query(F.data == "stats")
async def cb_stats(call: CallbackQuery, db: DB, cfg: Config):
    await call.message.edit_text(format_stats(db, cfg), reply_markup=stats_markup(db))
    await call.answer()


@router.callback_query(F.data.startswith("users:"))
async def cb_users(call: CallbackQuery, db: DB, cfg: Config):
    text, page, pages = format_users(db, cfg, int(call.data.split(":")[1]))
    b = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(("◀️", f"users:{page - 1}"))
    if page < pages - 1:
        nav.append(("▶️", f"users:{page + 1}"))
    for t, d in nav:
        b.button(text=t, callback_data=d)
    b.button(text="📊 К статистике", callback_data="stats")
    b.adjust(len(nav) or 1, 1)
    try:
        await call.message.edit_text(text, reply_markup=b.as_markup())
    except TelegramBadRequest:
        pass
    await call.answer()
