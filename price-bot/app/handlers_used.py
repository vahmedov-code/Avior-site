"""Б/у устройства: мастер добавления для владельца и витрина для клиентов.

Устройства приняты сервисом в трейд-ин и проверены мастерами; цена — та, что указал владелец,
без наценки. Каждое устройство в одном экземпляре: после оформления заказа оно пропадает с витрины.
"""
import asyncio
import html
import io
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import keyboards as kb
from . import watermark
from .channel import SETTING as CHANNEL_SETTING, channel_id, post_used, update_post
from .config import Config
from .db import DB, USED_LABEL
from .handlers_admin import IsAdmin
from .handlers_user import safe_edit
from .pricing import fmt_price

router = Router(name="used")
admin_router = Router(name="used_admin")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())

MAX_PHOTOS = 10  # столько Telegram показывает в одном альбоме
CONDITIONS = ("Как новый", "Отличное", "Хорошее", "Есть следы использования")
# Раздел по словам в модели; порядок важен: «Galaxy Tab» и «Galaxy Watch» — не смартфоны
KINDS = (
    ("Часы", ("watch", "часы", "band")),
    ("Наушники", ("airpods", "buds", "наушник", "headphone")),
    ("Планшеты", ("ipad", " tab", "pad ", "планшет", "matepad")),
    ("Ноутбуки", ("macbook", "ноутбук", "laptop", "thinkpad", "vivobook", "zenbook", "ideapad", "legion",
                  "rog ", "tuf ", "matebook", "magicbook", "redmibook", "omen", "pavilion", "aspire",
                  "nitro", "predator", "inspiron", "xps", "surface")),
    ("Смартфоны", ("iphone", "galaxy", "pixel", "xiaomi", "redmi", "poco", "honor", "huawei", "realme",
                   "oneplus", "nothing", "смартфон", "телефон", "vivo", "oppo", "tecno", "infinix")),
)
OTHER = "Другое"
KIND_NAMES = tuple(k for k, _ in KINDS) + (OTHER,)
_photo_lock = asyncio.Lock()  # фото из альбома приходят почти одновременно


class UsedAdd(StatesGroup):
    model = State()
    battery = State()
    condition = State()
    comment = State()
    price = State()
    photos = State()
    saving = State()
    confirm = State()
    edit = State()


class UsedPrice(StatesGroup):
    value = State()


def guess_kind(model: str) -> str:
    low = f" {model.lower()} "
    for kind, words in KINDS:
        if any(w in low for w in words):
            return kind
    return OTHER


def parse_battery(text: str) -> str | None:
    """«89», «89 %» → «89%»; «312 циклов» оставляем как есть; бессмыслица → None."""
    text = text.strip()
    m = re.fullmatch(r"(\d{1,3})\s*%?", text)
    if m:
        return f"{m.group(1)}%" if 1 <= int(m.group(1)) <= 100 else None
    return text[:40] if text else None


def parse_price(text: str) -> tuple[int, int | None] | None:
    """«45 000» → (45000, None); «45000/38000» → цена и закупка."""
    parts = [re.sub(r"\D", "", p) for p in text.split("/")]
    if not 1 <= len(parts) <= 2 or not all(parts):
        return None
    price, cost = int(parts[0]), (int(parts[1]) if len(parts) == 2 else None)
    return (price, cost) if price >= 100 else None


def card_text(cfg: Config, d) -> str:
    """d — строка used_items или словарь из мастера добавления."""
    lines = [f"♻️ <b>{html.escape(d['model'])}</b>", ""]
    if d["battery"]:
        lines.append(f"🔋 Аккумулятор: {html.escape(d['battery'])}")
    if d["condition"]:
        lines.append(f"✨ Внешний вид: {html.escape(d['condition'])}")
    if d["comment"]:
        lines.append(f"📝 {html.escape(d['comment'])}")
    lines += ["", f"💰 <b>{fmt_price(d['price'])}</b>", "", f"<i>{html.escape(cfg.used_note)}</i>"]
    return "\n".join(lines)[:1024]  # лимит подписи к фото


def cancel_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖ Отмена", callback_data="bux")]])


def buttons(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


async def show(call: CallbackQuery, text: str, markup) -> None:
    """С карточки (фото) на текстовый экран: фото убираем, текст отправляем новым сообщением."""
    if call.message.photo:
        try:
            await call.message.delete()
        except TelegramBadRequest:
            pass
        await call.message.answer(text, reply_markup=markup)
    else:
        await safe_edit(call, text, markup)


# --- витрина ---

def showcase(db: DB, cfg: Config, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    kinds = db.used_kinds()
    text = f"<b>{USED_LABEL} — проверено AVIOR</b>\n\n{html.escape(cfg.used_note)}\n\n"
    text += "Выберите раздел:" if kinds else "Сейчас устройств нет — загляните позже."
    b = InlineKeyboardBuilder()
    for kind, n in kinds:
        b.button(text=f"{kind} ({n})", callback_data=f"bk:{kind}")
    b.adjust(2)
    if user_id in cfg.admin_ids:
        b.row(InlineKeyboardButton(text="➕ Добавить", callback_data="buadd"),
              InlineKeyboardButton(text="📋 Все устройства", callback_data="bulist"))
    count = db.cart_count(user_id)
    if count:
        b.row(kb.cart_button(count))
    b.row(kb.BTN_CATALOG, kb.BTN_HOME)
    return text, b.as_markup()


@router.callback_query(F.data == "bu")
async def cb_showcase(call: CallbackQuery, state: FSMContext, db: DB, cfg: Config):
    await state.clear()
    await show(call, *showcase(db, cfg, call.from_user.id))
    await call.answer()


@router.callback_query(F.data.startswith("bk:"))
async def cb_kind(call: CallbackQuery, db: DB):
    kind = call.data[3:]
    rows = db.used_items(kind)
    if not rows:
        await call.answer("В этом разделе всё разобрали", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for r in rows:
        battery = f" · 🔋{r['battery']}" if r["battery"] else ""
        b.button(text=f"{r['model']}{battery} · {fmt_price(r['price'])}", callback_data=f"bi:{r['id']}")
    b.adjust(1)
    count = db.cart_count(call.from_user.id)
    if count:
        b.row(kb.cart_button(count))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="bu"), kb.BTN_HOME)
    await show(call, f"<b>{USED_LABEL} › {html.escape(kind)}</b>\n\nНажмите на устройство, чтобы посмотреть фото.",
               b.as_markup())
    await call.answer()


def card_markup(db: DB, cfg: Config, row, user_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🛒 В корзину", callback_data=f"bc:{row['id']}"))
    n = len(db.used_photos(row))
    if n > 1:
        b.row(InlineKeyboardButton(text=f"📷 Все фото ({n})", callback_data=f"bp:{row['id']}"))
    if user_id in cfg.admin_ids:
        b.row(InlineKeyboardButton(text="💰 Изменить цену", callback_data=f"bprice:{row['id']}"),
              InlineKeyboardButton(text="✅ Продано", callback_data=f"bsold:{row['id']}"))
    count = db.cart_count(user_id)
    if count:
        b.row(kb.cart_button(count))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"bk:{row['kind']}"), kb.BTN_HOME)
    return b.as_markup()


async def send_card(message: Message, db: DB, cfg: Config, row, user_id: int) -> None:
    photos = db.used_photos(row)
    markup = card_markup(db, cfg, row, user_id)
    if photos:
        await message.answer_photo(photos[0], caption=card_text(cfg, row), reply_markup=markup)
    else:
        await message.answer(card_text(cfg, row), reply_markup=markup)


@router.callback_query(F.data.startswith("bi:"))
async def cb_card(call: CallbackQuery, db: DB, cfg: Config):
    row = db.used(int(call.data[3:]))
    # владелец открывает и устройства из оформленных заказов — чтобы отметить продажу
    visible = ("active", "reserved") if call.from_user.id in cfg.admin_ids else ("active",)
    if not row or row["status"] not in visible:
        await call.answer("Это устройство уже продано", show_alert=True)
        return
    try:
        await call.message.delete()
    except TelegramBadRequest:
        pass
    await send_card(call.message, db, cfg, row, call.from_user.id)
    await call.answer()


@router.message(CommandStart(deep_link=True, magic=F.args.regexp(r"^bu\d+$")))
async def start_card(message: Message, command: CommandObject, state: FSMContext, db: DB, cfg: Config):
    """Кнопка «Купить в боте» из поста в канале: t.me/бот?start=bu12 — сразу карточка устройства."""
    await state.clear()
    row = db.used(int(command.args[2:]))
    if not row or row["status"] != "active":
        await message.answer("Это устройство уже продано 😔 Посмотрите другие:",
                             reply_markup=buttons([(f"{USED_LABEL} — все устройства", "bu")], [("🏠 В меню", "home")]))
        return
    await send_card(message, db, cfg, row, message.from_user.id)


@router.callback_query(F.data.startswith("bp:"))
async def cb_photos(call: CallbackQuery, db: DB):
    row = db.used(int(call.data[3:]))
    if not row:
        await call.answer()
        return
    photos = db.used_photos(row)
    await call.message.answer_media_group([InputMediaPhoto(media=p) for p in photos[:MAX_PHOTOS]])
    await call.answer()


@router.callback_query(F.data.startswith("bc:"))
async def cb_to_cart(call: CallbackQuery, db: DB, cfg: Config):
    row = db.used(int(call.data[3:]))
    if not row or row["status"] != "active":
        await call.answer("Это устройство уже продано", show_alert=True)
        return
    db.add_used_to_cart(call.from_user.id, row)
    try:
        await call.message.edit_reply_markup(reply_markup=card_markup(db, cfg, row, call.from_user.id))
    except TelegramBadRequest:
        pass
    await call.answer(f"✅ В корзине: {row['model']}")


# --- добавление (только владелец) ---

def admin_list(db: DB) -> tuple[str, InlineKeyboardMarkup]:
    """Все устройства на витрине и в заказах; «✅ Продано» убирает устройство из раздела."""
    active, reserved = db.used_items(), db.used_items(status="reserved")
    lines = [f"<b>{USED_LABEL}</b>: на витрине {len(active)}, в заказах {len(reserved)}", ""]
    b = InlineKeyboardBuilder()
    for r in reserved + active:
        mark = "🕓 " if r["status"] == "reserved" else ""
        lines.append(f"{mark}№{r['id']} · {html.escape(r['model'])} · {fmt_price(r['price'])}")
        b.row(InlineKeyboardButton(text=f"{mark}№{r['id']} {r['model']}"[:40], callback_data=f"bi:{r['id']}"),
              InlineKeyboardButton(text="✅ Продано", callback_data=f"bsold:{r['id']}"))
    if reserved:
        lines.append("\n🕓 — в оформленном заказе: подтвердите или отмените заказ, или отметьте продажу здесь.")
    if reserved or active:
        lines.append("\nНажмите на устройство — откроется карточка (там же изменить цену).")
    b.row(InlineKeyboardButton(text="➕ Добавить б/у устройство", callback_data="buadd"))
    return "\n".join(lines)[:4000], b.as_markup()


@admin_router.message(Command("bu"))
async def cmd_used(message: Message, state: FSMContext, db: DB):
    await state.clear()
    text, markup = admin_list(db)
    await message.answer(text, reply_markup=markup)


@admin_router.callback_query(F.data.startswith("bsold:"))
async def cb_sold(call: CallbackQuery, bot: Bot, db: DB, cfg: Config):
    row = db.used(int(call.data.split(":")[1]))
    if not row:
        await call.answer()
        return
    db.set_used_status(row["id"], "sold")
    await update_post(bot, db, cfg, row["id"], sold=True)
    if call.message.photo:  # нажали на карточке устройства
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.answer(f"✅ Продано и убрано с витрины: {html.escape(row['model'])}",
                                  reply_markup=buttons([("📋 Все б/у устройства", "bulist")]))
    else:
        await safe_edit(call, *admin_list(db))
    await call.answer(f"Продано: {row['model']}")


@admin_router.callback_query(F.data == "bulist")
async def cb_admin_list(call: CallbackQuery, state: FSMContext, db: DB):
    await state.clear()
    await show(call, *admin_list(db))
    await call.answer()


@admin_router.callback_query(F.data == "buadd")
async def cb_add_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(UsedAdd.model)
    await call.message.answer("<b>Новое б/у устройство</b> — шаг 1 из 6\n\n"
                              "Какая модель? Напишите с памятью и цветом, например:\n"
                              "<code>iPhone 13 Pro 256GB Graphite</code>", reply_markup=cancel_button())
    await call.answer()


@admin_router.callback_query(F.data == "bux")
async def cb_add_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("Отменено.", reply_markup=buttons([("➕ Добавить б/у устройство", "buadd")],
                                                               [("📋 Все устройства", "bulist")]))
    await call.answer()


@admin_router.message(StateFilter(UsedAdd.model), F.text, ~F.text.startswith("/"))
async def on_model(message: Message, state: FSMContext):
    model = " ".join(message.text.split())[:120]
    await state.update_data(model=model, kind=guess_kind(model))
    await state.set_state(UsedAdd.battery)
    await message.answer("Шаг 2 из 6. Какой процент аккумулятора? Например: <code>89</code>\n"
                         "Можно написать и по-другому (<code>312 циклов</code>).",
                         reply_markup=buttons([("Без аккумулятора", "bubat")], [("✖ Отмена", "bux")]))


async def ask_condition(message: Message, state: FSMContext) -> None:
    await state.set_state(UsedAdd.condition)
    b = InlineKeyboardBuilder()
    for idx, c in enumerate(CONDITIONS):
        b.button(text=c, callback_data=f"bucond:{idx}")
    b.adjust(2)
    b.row(InlineKeyboardButton(text="✖ Отмена", callback_data="bux"))
    await message.answer("Шаг 3 из 6. Внешнее состояние? Выберите или напишите своё "
                         "(например: <code>царапина на задней крышке</code>).", reply_markup=b.as_markup())


@admin_router.message(StateFilter(UsedAdd.battery), F.text, ~F.text.startswith("/"))
async def on_battery(message: Message, state: FSMContext):
    battery = parse_battery(message.text)
    if battery is None:
        await message.answer("Напишите число от 1 до 100, например <code>89</code>.", reply_markup=cancel_button())
        return
    await state.update_data(battery=battery)
    await ask_condition(message, state)


@admin_router.callback_query(StateFilter(UsedAdd.battery), F.data == "bubat")
async def cb_no_battery(call: CallbackQuery, state: FSMContext):
    await state.update_data(battery="")
    await call.message.edit_reply_markup(reply_markup=None)
    await ask_condition(call.message, state)
    await call.answer()


async def ask_comment(message: Message, state: FSMContext) -> None:
    await state.set_state(UsedAdd.comment)
    await message.answer("Шаг 4 из 6. Комплект и комментарий: коробка, кабель, что меняли, что проверили.\n"
                         "Например: <code>Полный комплект, Face ID работает, ремонтов не было</code>",
                         reply_markup=buttons([("Пропустить", "buskip")], [("✖ Отмена", "bux")]))


@admin_router.callback_query(StateFilter(UsedAdd.condition), F.data.startswith("bucond:"))
async def cb_condition(call: CallbackQuery, state: FSMContext):
    await state.update_data(condition=CONDITIONS[int(call.data.split(":")[1])])
    await call.message.edit_reply_markup(reply_markup=None)
    await ask_comment(call.message, state)
    await call.answer()


@admin_router.message(StateFilter(UsedAdd.condition), F.text, ~F.text.startswith("/"))
async def on_condition(message: Message, state: FSMContext):
    await state.update_data(condition=message.text.strip()[:200])
    await ask_comment(message, state)


async def ask_price(message: Message, state: FSMContext) -> None:
    await state.set_state(UsedAdd.price)
    await message.answer("Шаг 5 из 6. Цена продажи, ₽ — например <code>45000</code>.\n"
                         "Если хотите видеть прибыль в заказах, через дробь укажите, за сколько приняли: "
                         "<code>45000/38000</code>", reply_markup=cancel_button())


@admin_router.message(StateFilter(UsedAdd.comment), F.text, ~F.text.startswith("/"))
async def on_comment(message: Message, state: FSMContext):
    await state.update_data(comment=message.text.strip()[:500])
    await ask_price(message, state)


@admin_router.callback_query(StateFilter(UsedAdd.comment), F.data == "buskip")
async def cb_skip_comment(call: CallbackQuery, state: FSMContext):
    await state.update_data(comment="")
    await call.message.edit_reply_markup(reply_markup=None)
    await ask_price(call.message, state)
    await call.answer()


@admin_router.message(StateFilter(UsedAdd.price), F.text, ~F.text.startswith("/"))
async def on_price(message: Message, state: FSMContext):
    parsed = parse_price(message.text)
    if not parsed:
        await message.answer("Не понял цену. Пример: <code>45000</code> или <code>45000/38000</code>",
                             reply_markup=cancel_button())
        return
    await state.update_data(price=parsed[0], cost=parsed[1], photos=[])
    await state.set_state(UsedAdd.photos)
    await message.answer(f"Шаг 6 из 6. Загрузите фото (до {MAX_PHOTOS}) — можно альбомом. "
                         "Логотип сайта я поставлю сам.\nКогда всё загрузите, нажмите «✅ Готово».",
                         reply_markup=done_button())


def done_button() -> InlineKeyboardMarkup:
    return buttons([("✅ Готово", "budone")], [("✖ Отмена", "bux")])


@admin_router.message(StateFilter(UsedAdd.photos), F.photo | F.document.mime_type.startswith("image/"))
async def on_photo(message: Message, state: FSMContext):
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    async with _photo_lock:
        photos = (await state.get_data()).get("photos", [])
        if len(photos) >= MAX_PHOTOS:
            full = True
        else:
            full = False
            photos = photos + [file_id]
            await state.update_data(photos=photos)
    if full:
        await message.answer(f"Больше {MAX_PHOTOS} фото не поместится — нажмите «✅ Готово».",
                             reply_markup=done_button())
        return
    # альбом приходит пачкой сообщений — отвечаем один раз, когда пачка закончилась
    count = len(photos)
    await asyncio.sleep(1.5)
    if len((await state.get_data()).get("photos", [])) == count:
        await message.answer(f"📷 Фото: {count}. Можно добавить ещё или нажать «✅ Готово».",
                             reply_markup=done_button())


@admin_router.message(StateFilter(UsedAdd.photos), ~F.text.startswith("/"))
async def on_not_photo(message: Message):
    await message.answer("Жду фото устройства. Когда всё загрузите — «✅ Готово».", reply_markup=done_button())


# Что можно исправить в карточке перед публикацией: поле → (кнопка, вопрос)
EDITABLE = {
    "model": ("Модель", "Новое название модели:"),
    "battery": ("АКБ", "Процент аккумулятора (например <code>89</code>) или <code>нет</code>:"),
    "condition": ("Состояние", "Внешнее состояние:"),
    "comment": ("Комментарий", "Комплект и комментарий (<code>-</code> — убрать):"),
    "price": ("Цена", "Цена, ₽ (можно с закупкой: <code>45000/38000</code>):"),
}


def preview_markup(kind: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✅ Опубликовать", callback_data="bupub"))
    b.row(*[InlineKeyboardButton(text=f"✏️ {label}", callback_data=f"buedit:{field}")
            for field, (label, _) in list(EDITABLE.items())[:3]])
    b.row(*[InlineKeyboardButton(text=f"✏️ {label}", callback_data=f"buedit:{field}")
            for field, (label, _) in list(EDITABLE.items())[3:]],
          InlineKeyboardButton(text="📷 Фото заново", callback_data="buphotos"))
    kinds = InlineKeyboardBuilder()
    for k in KIND_NAMES:
        kinds.button(text=("📂 " if k == kind else "") + k, callback_data=f"bukind:{k}")
    kinds.adjust(3)
    b.attach(kinds)
    b.row(InlineKeyboardButton(text="✖ Отмена", callback_data="bux"))
    return b.as_markup()


def preview_caption(cfg: Config, data: dict) -> str:
    note = (f"\n\n👆 <b>Проверьте карточку.</b> Раздел: {USED_LABEL} › {html.escape(data['kind'])}, "
            f"фото: {len(data['marked'])}.\nЕсли всё верно — «✅ Опубликовать», до этого клиенты устройство не видят.")
    card = card_text(cfg, data)
    return card[:1024 - len(note)] + note  # лимит подписи к фото


async def send_preview(message: Message, state: FSMContext, cfg: Config) -> None:
    """Карточка целиком, как её увидит клиент, и кнопки: опубликовать или исправить."""
    await state.set_state(UsedAdd.confirm)
    data = await state.get_data()
    await message.answer_photo(data["marked"][0], caption=preview_caption(cfg, data),
                               reply_markup=preview_markup(data["kind"]))


@admin_router.callback_query(StateFilter(UsedAdd.photos), F.data == "budone")
async def cb_photos_done(call: CallbackQuery, state: FSMContext, bot: Bot, cfg: Config):
    data = await state.get_data()
    if not data.get("photos"):
        await call.answer("Нужно хотя бы одно фото", show_alert=True)
        return
    await state.set_state(UsedAdd.saving)  # повторное нажатие «Готово» не запустит обработку дважды
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Ставлю логотип…")
    ready = []
    for no, file_id in enumerate(data["photos"], start=1):
        try:
            buf = io.BytesIO()
            await bot.download(file_id, destination=buf)
            image = await asyncio.to_thread(watermark.apply, buf.getvalue())
        except Exception as e:  # слишком большой файл, не картинка и т.п.
            await call.message.answer(f"⚠️ Фото {no} пропущено: {html.escape(str(e))[:200]}")
            continue
        ready.append(BufferedInputFile(image, filename=f"avior-{no}.jpg"))
    if not ready:
        await state.set_state(UsedAdd.photos)
        await state.update_data(photos=[])
        await call.message.answer("Не получилось обработать фото — пришлите их ещё раз.", reply_markup=done_button())
        return

    # Фото с логотипом отправляем себе: так у Telegram появляются их file_id для витрины
    if len(ready) == 1:
        sent = [await call.message.answer_photo(ready[0], caption="Фото с логотипом")]
    else:
        media = [InputMediaPhoto(media=f, caption="Фото с логотипом" if i == 0 else None) for i, f in enumerate(ready)]
        sent = await call.message.answer_media_group(media)
    await state.update_data(marked=[m.photo[-1].file_id for m in sent])
    await send_preview(call.message, state, cfg)


@admin_router.callback_query(StateFilter(UsedAdd.confirm), F.data.startswith("bukind:"))
async def cb_set_kind(call: CallbackQuery, state: FSMContext, cfg: Config):
    kind = call.data.split(":", 1)[1]
    if kind not in KIND_NAMES:
        await call.answer()
        return
    await state.update_data(kind=kind)
    data = await state.get_data()
    try:
        await call.message.edit_caption(caption=preview_caption(cfg, data), reply_markup=preview_markup(kind))
    except TelegramBadRequest:
        pass
    await call.answer(f"Раздел: {kind}")


@admin_router.callback_query(StateFilter(UsedAdd.confirm), F.data.startswith("buedit:"))
async def cb_edit(call: CallbackQuery, state: FSMContext):
    field = call.data.split(":", 1)[1]
    if field not in EDITABLE:
        await call.answer()
        return
    await state.update_data(editing=field)
    await state.set_state(UsedAdd.edit)
    await call.message.answer(EDITABLE[field][1], reply_markup=buttons([("⬅️ Не менять", "buback")]))
    await call.answer()


@admin_router.callback_query(StateFilter(UsedAdd.edit), F.data == "buback")
async def cb_edit_back(call: CallbackQuery, state: FSMContext, cfg: Config):
    await call.message.edit_reply_markup(reply_markup=None)
    await send_preview(call.message, state, cfg)
    await call.answer()


@admin_router.message(StateFilter(UsedAdd.edit), F.text, ~F.text.startswith("/"))
async def on_edit(message: Message, state: FSMContext, cfg: Config):
    field = (await state.get_data())["editing"]
    text = message.text.strip()
    if field == "model":
        model = " ".join(text.split())[:120]
        await state.update_data(model=model, kind=guess_kind(model))
    elif field == "battery":
        battery = "" if text.lower() in ("нет", "-") else parse_battery(text)
        if battery is None:
            await message.answer("Напишите число от 1 до 100 или <code>нет</code>.", reply_markup=buttons([("⬅️ Не менять", "buback")]))
            return
        await state.update_data(battery=battery)
    elif field == "condition":
        await state.update_data(condition=text[:200])
    elif field == "comment":
        await state.update_data(comment="" if text == "-" else text[:500])
    else:
        parsed = parse_price(text)
        if not parsed:
            await message.answer("Не понял цену. Пример: <code>45000</code> или <code>45000/38000</code>",
                                 reply_markup=buttons([("⬅️ Не менять", "buback")]))
            return
        await state.update_data(price=parsed[0], cost=parsed[1])
    await send_preview(message, state, cfg)


@admin_router.callback_query(StateFilter(UsedAdd.confirm), F.data == "buphotos")
async def cb_redo_photos(call: CallbackQuery, state: FSMContext):
    await state.update_data(photos=[], marked=[])
    await state.set_state(UsedAdd.photos)
    await call.message.answer(f"Пришлите фото заново (до {MAX_PHOTOS}), потом «✅ Готово».", reply_markup=done_button())
    await call.answer()


@admin_router.callback_query(StateFilter(UsedAdd.confirm), F.data == "bupub")
async def cb_publish(call: CallbackQuery, state: FSMContext, bot: Bot, db: DB, cfg: Config):
    data = await state.get_data()
    used_id = db.add_used(data["kind"], data["model"], data["battery"], data["condition"], data["comment"],
                          data["price"], data["cost"], data["marked"])
    await state.clear()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await call.message.answer(
        f"✅ <b>Опубликовано</b>: {html.escape(data['model'])} — {fmt_price(data['price'])}\n"
        f"Раздел {USED_LABEL} › {html.escape(data['kind'])}, №{used_id}. Клиенты уже видят устройство.",
        reply_markup=buttons([("👀 Посмотреть", f"bi:{used_id}"), ("📋 Все устройства", "bulist")],
                             [("➕ Добавить ещё", "buadd")]))
    await call.answer("Опубликовано")
    error = await post_used(bot, db, cfg, used_id)
    if error:
        await call.message.answer("⚠️ " + error)


# --- управление с карточки ---

@admin_router.callback_query(F.data.startswith("bprice:"))
async def cb_price(call: CallbackQuery, state: FSMContext, db: DB):
    row = db.used(int(call.data.split(":")[1]))
    if not row:
        await call.answer()
        return
    await state.set_state(UsedPrice.value)
    await state.update_data(used_id=row["id"])
    await call.message.answer(f"Новая цена для {html.escape(row['model'])} (сейчас {fmt_price(row['price'])}):",
                              reply_markup=cancel_button())
    await call.answer()


@admin_router.message(StateFilter(UsedPrice.value), F.text, ~F.text.startswith("/"))
async def on_new_price(message: Message, state: FSMContext, bot: Bot, db: DB, cfg: Config):
    parsed = parse_price(message.text)
    if not parsed:
        await message.answer("Не понял цену. Пример: <code>43000</code>", reply_markup=cancel_button())
        return
    used_id = (await state.get_data())["used_id"]
    db.set_used_price(used_id, parsed[0])
    await state.clear()
    await update_post(bot, db, cfg, used_id)
    await message.answer(f"✅ Новая цена: {fmt_price(parsed[0])}", reply_markup=buttons([("👀 Посмотреть", f"bi:{used_id}")]))


# --- канал ---

@admin_router.message(Command("channel"))
async def cmd_channel(message: Message, command: CommandObject, bot: Bot, db: DB, cfg: Config):
    arg = (command.args or "").strip()
    if not arg:
        current = channel_id(db)
        await message.answer(
            (f"Б/у публикуются в канал <code>{current}</code>." if current else "Канал для б/у не подключён.")
            + "\n\nПодключить: добавьте бота администратором канала (право «Публикация сообщений») и напишите\n"
              "<code>/channel @имя_канала</code>\n/channel all — опубликовать всё, что сейчас на витрине\n"
              "/channel off — не публиковать")
        return
    if arg == "off":
        db.set_setting(CHANNEL_SETTING, "")
        await message.answer("Публикация б/у в канал выключена.")
        return
    if arg == "all":
        if not channel_id(db):
            await message.answer("Сначала подключите канал: <code>/channel @имя_канала</code>")
            return
        errors = [e for r in reversed(db.used_items()) if (e := await post_used(bot, db, cfg, r["id"]))]
        await message.answer("⚠️ " + errors[0] if errors else "✅ Все устройства с витрины опубликованы в канале.")
        return
    try:
        chat = await bot.get_chat(arg if arg.startswith(("@", "-")) else f"@{arg}")
        member = await bot.get_chat_member(chat.id, (await bot.me()).id)
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.answer("Не нашёл канал. Проверьте имя и что бот добавлен в канал администратором.")
        return
    if member.status not in ("administrator", "creator"):
        await message.answer("Бот есть в канале, но не администратор — дайте ему право «Публикация сообщений».")
        return
    db.set_setting(CHANNEL_SETTING, chat.id)
    await message.answer(f"✅ Канал «{html.escape(chat.title or arg)}» подключён: новые б/у будут публиковаться туда "
                         "автоматически, проданные — помечаться «ПРОДАНО».\n"
                         "Опубликовать то, что уже на витрине: /channel all")
