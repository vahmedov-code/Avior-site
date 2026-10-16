"""Команды владельца: импорт прайса пересылкой поста, наценки, категории."""
import html

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, MessageOriginChannel, TelegramObject

from . import keyboards as kb
from .config import Config
from .db import DB, ImportDiff
from .parser import count_items, parse_post
from .pricing import apply_markup, fmt_price

router = Router(name="admin")

HELP = """<b>Управление ботом</b>

<b>Обновить цены:</b> перешлите сюда пост с прайсом из группы поставщика. Бот спросит, в какую категорию его положить, и запомнит это — в следующий раз тот же пост обновится сам.

<b>Наценка</b>
/markup — текущая наценка
/markup 10 — +10% ко всем ценам
/markup 10 500 — +10% и ещё +500 ₽
/round 100 — округлять вверх до 100 ₽ (1 — без округления)
/catmarkup 3 15 — своя наценка для категории №3 (+15%)
/catmarkup 3 15 1000 — +15% и +1000 ₽
/catmarkup 3 off — категория №3 снова по общей наценке

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


def format_report(db: DB, cat_id: int, diff: ImportDiff) -> str:
    cat = db.category(cat_id)
    pct, fixed, step = db.markup(cat)
    sell = lambda p: fmt_price(apply_markup(p, pct, fixed, step))  # noqa: E731
    lines = [
        f"✅ <b>{html.escape(cat['title'])}</b> обновлена",
        f"Позиций: {diff.total} (было {diff.previous})",
        f"Наценка: {markup_text(pct, fixed)}, округление до {step} ₽",
    ]
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


# --- импорт прайса ---

@router.message(StateFilter(None, ImportFlow.choose, ImportFlow.new_title), HasPrices())
async def on_price_post(message: Message, state: FSMContext, db: DB, sections, post_text: str):
    src = source_of(message)
    if src:
        cat = db.find_by_source(*src)
        if cat:
            diff = db.replace_items(cat["id"], sections)
            await state.clear()
            await message.answer(format_report(db, cat["id"], diff))
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
    sections = parse_post(data["post_text"], cfg.block_words)
    if data.get("source"):
        db.set_source(cat_id, *data["source"])
    diff = db.replace_items(cat_id, sections)
    await message.answer(format_report(db, cat_id, diff))


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
    cat_id = db.add_category(message.text.strip()[:60])
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
async def cmd_newcat(message: Message, command: CommandObject, db: DB):
    title = (command.args or "").strip()
    if not title:
        await message.answer("Пример: /newcat iPhone 18 Pro")
        return
    cat_id = db.add_category(title[:60])
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
