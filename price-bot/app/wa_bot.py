"""Упрощённый бот AVIOR в WhatsApp (WhatsApp Cloud API от Meta).

Отдельная служба (avior-wa) с общей базой: каталог, б/у, корзина, заказы, статистика — те же, что в
Telegram и MAX. Управление — в Telegram: заказы и заявки приходят владельцу туда с пометкой «WhatsApp».

Отличия от Telegram из-за WhatsApp: под сообщением до 3 кнопок или список до 10 пунктов, поэтому
каталог — через поиск по модели; номер телефона клиента WhatsApp передаёт сам.
Meta присылает сообщения на наш адрес (webhook): https://<домен>/whatsapp → этот сервер (127.0.0.1:WA_PORT).

Запуск: python -m app.wa_bot (служба avior-wa)."""
from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import re
import time

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from aiohttp import web

from . import keyboards as kb
from .config import Config
from .crm import CrmUnavailable, order_status, send_lead
from .db import DB
from .handlers_repair import REPAIR_TEXT, clean_number, crm_text, status_text
from .handlers_reply import reply_markup
from .handlers_used import CONDITIONS, card_text
from .handlers_user import notify_admins, person, render_cart, render_order_for_admin, welcome
from .platforms import wa_uid
from .pricing import fmt_price
from .search import search
from .stats import today
from .wa_api import WaApi, html_to_wa, verify_signature

log = logging.getLogger("whatsapp")
TRADEIN_PHOTOS = 5
MEDIA_TTL = 25 * 86400  # Meta хранит загруженные файлы 30 дней — обновляем заранее
GREETINGS = {"меню", "start", "/start", "старт", "начать", "привет", "здравствуйте", "добрый день", "hi", "hello"}


def mk(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
                                                 for row in rows])


class WaBot:
    def __init__(self, api: WaApi, tg: Bot, db: DB, cfg: Config):
        self.api, self.tg, self.db, self.cfg = api, tg, db, cfg
        self.states: dict[int, dict] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.tasks: set[asyncio.Task] = set()

    # --- вывод ---
    async def say(self, phone: str, html_text: str, buttons: list[tuple[str, str]] | None = None,
                  image_id: str | None = None) -> None:
        text = html_to_wa(html_text)
        if buttons:
            await self.api.buttons(phone, text, buttons, image_id=image_id)
        elif image_id:
            await self.api.image(phone, image_id, text)
        else:
            await self.api.text(phone, text)

    async def home(self, phone: str, uid: int, note: str = "") -> None:
        self.states.pop(uid, None)
        count = self.db.cart_count(uid)
        rows = [("repair", "🔧 Ремонт", "запись, цены, сроки"),
                ("rpstatus", "🔎 Статус ремонта", "по номеру заказа"),
                ("catalog", "🛍 Каталог", "поиск по модели, проверенные б/у"),
                ("ti", "💰 Трейд-ин", "сдать устройство в зачёт или за деньги")]
        if count:
            rows.append(("cart", f"🛒 Корзина ({count})", "оформить заказ"))
        rows += [("lead", "💬 Вопрос мастеру", ""), ("contacts", "📞 Контакты и доставка", "")]
        text = (note + "\n\n" if note else "") + html_to_wa(welcome(self.cfg)).replace(
            "Выберите, что вас интересует:", "Нажмите «Меню» и выберите, что нужно 👇")
        await self.api.menu(phone, text, "Меню", rows)

    async def wa_photo(self, tg_file_id: str) -> str:
        """Фото б/у хранятся как file_id Telegram: скачиваем, загружаем в WhatsApp, запоминаем на 25 дней."""
        key = f"wa:{tg_file_id}"
        cached = self.db.max_photo(key)  # общая таблица загруженных картинок
        if cached and time.time() - cached["ts"] < MEDIA_TTL:
            return cached["id"]
        buf = io.BytesIO()
        await self.tg.download(tg_file_id, destination=buf)
        media_id = await self.api.upload_image(buf.getvalue())
        self.db.save_max_photo(key, {"id": media_id, "ts": int(time.time())})
        return media_id

    # --- входящие ---
    async def handle(self, phone: str, name: str, msg: dict) -> None:
        uid = wa_uid(phone)
        first_visit = self.db.conn.execute("SELECT 1 FROM visitors WHERE user_id = ?", (uid,)).fetchone() is None
        try:
            self.db.track_visit(uid, None, name, today(self.cfg).date().isoformat())
        except Exception:
            log.exception("Статистика: не удалось записать посещение")
        await self.api.mark_read(msg.get("id", ""))
        kind = msg.get("type")
        if kind == "interactive":
            reply = msg["interactive"].get("button_reply") or msg["interactive"].get("list_reply") or {}
            await self.on_action(phone, uid, name, reply.get("id", ""))
        elif kind == "button":  # быстрый ответ на шаблон
            await self.on_action(phone, uid, name, (msg.get("button") or {}).get("payload", ""))
        elif kind == "image":
            await self.on_image(phone, uid, msg["image"]["id"])
        elif kind == "text":
            text = msg["text"]["body"].strip()
            if first_visit or text.lower().strip("!.") in GREETINGS:
                await self.home(phone, uid)
            else:
                await self.on_text(phone, uid, name, text)
        else:
            await self.home(phone, uid, "Я понимаю текст, фото и кнопки 🙂")

    async def on_action(self, phone: str, uid: int, name: str, p: str) -> None:
        db, cfg = self.db, self.cfg
        state = self.states.get(uid, {})
        if p in ("home", "catalog_home"):
            await self.home(phone, uid)
        elif p == "contacts":
            await self.say(phone, f"<b>{html.escape(cfg.shop_name)}</b>\n\n{html.escape(cfg.contacts_text)}",
                           [("lead", "💬 Задать вопрос"), ("home", "🏠 Меню")])
        elif p == "catalog":
            self.states.pop(uid, None)
            kinds = db.used_kinds()
            rows = [("find", "🔍 Поиск по модели", "напишите, что ищете")]
            if kinds:
                rows.append(("bu", f"♻️ Б/у ({sum(n for _, n in kinds)})", "проверены мастерами AVIOR"))
            if db.cart_count(uid):
                rows.append(("cart", f"🛒 Корзина ({db.cart_count(uid)})", ""))
            rows.append(("home", "🏠 Меню", ""))
            await self.api.menu(phone, html_to_wa(
                f"🛍 <b>Каталог</b>\n\n{html.escape(cfg.welcome_text)}\n\n"
                "🔍 Просто напишите модель, например: <b>15 Pro 256</b>"), "Каталог", rows)
        elif p == "find":
            await self.say(phone, "🔍 Напишите модель, память или цвет. Например: <b>15 Pro 256</b>, "
                                  "<b>macbook air m3</b>, <b>airpods pro</b>")
        elif p.startswith("fa:"):
            item = db.item(int(p[3:]))
            if not item:
                await self.say(phone, "Прайс обновился — повторите поиск.")
                return
            db.add_to_cart(uid, item)
            await self.say(phone, f"✅ В корзине: <b>{html.escape(item['name'])}</b> — {fmt_price(db.sell_price(item))}",
                           [("checkout", "✅ Оформить заказ"), ("cart", "🛒 Корзина"), ("home", "🏠 Меню")])
        elif p == "bu":
            rows = [(f"bi:{r['id']}", r["model"], f"{fmt_price(r['price'])}" + (f" · 🔋{r['battery']}" if r["battery"] else ""))
                    for r in db.used_items()[:10]]
            if not rows:
                await self.say(phone, "Сейчас б/у устройств нет — загляните позже.", [("home", "🏠 Меню")])
                return
            await self.api.menu(phone, html_to_wa(f"♻️ <b>Б/у — проверено AVIOR</b>\n\n{html.escape(cfg.used_note)}"),
                                "Устройства", rows)
        elif p.startswith("bi:"):
            row = db.used(int(p[3:]))
            if not row or row["status"] != "active":
                await self.say(phone, "Это устройство уже продано 😔", [("bu", "♻️ Другие б/у"), ("home", "🏠 Меню")])
                return
            photos = db.used_photos(row)
            image_id = await self.wa_photo(photos[0]) if photos else None
            await self.say(phone, card_text(cfg, row), [(f"bc:{row['id']}", "🛒 В корзину"), ("bu", "♻️ Другие б/у"),
                                                        ("home", "🏠 Меню")], image_id=image_id)
        elif p.startswith("bc:"):
            row = db.used(int(p[3:]))
            if not row or row["status"] != "active":
                await self.say(phone, "Это устройство уже продано 😔", [("bu", "♻️ Другие б/у"), ("home", "🏠 Меню")])
                return
            db.add_used_to_cart(uid, row)
            await self.say(phone, f"✅ В корзине: <b>{html.escape(row['model'])}</b> — {fmt_price(row['price'])}",
                           [("checkout", "✅ Оформить заказ"), ("cart", "🛒 Корзина"), ("home", "🏠 Меню")])
        elif p in ("cart", "cartclear"):
            if p == "cartclear":
                db.clear_cart(uid)
            lines = db.cart_lines(uid)
            text = render_cart(lines).replace("\n\n❌ с номером — убрать товар.", "")
            buttons = ([("checkout", "✅ Оформить заказ"), ("cartclear", "🗑 Очистить"), ("home", "🏠 Меню")]
                       if lines else [("catalog", "🛍 Каталог"), ("home", "🏠 Меню")])
            await self.say(phone, text, buttons)
        elif p == "checkout":
            lines = db.cart_lines(uid)
            if not any(line.available for line in lines):
                await self.say(phone, "В корзине нет доступных товаров.", [("catalog", "🛍 Каталог"), ("home", "🏠 Меню")])
                return
            self.states[uid] = {"state": "delivery"}
            await self.api.menu(phone, html_to_wa(render_cart(lines).rsplit("\n\n", 1)[0]) + "\n\nКак удобно получить заказ?",
                                "Получение", [(f"d:{i}", o, "") for i, o in enumerate(cfg.delivery_options)])
        elif p.startswith("d:") and state.get("state") == "delivery":
            await self.checkout(phone, uid, name, cfg.delivery_options[int(p[2:])])
        elif p == "lead":
            self.states[uid] = {"state": "lead"}
            await self.say(phone, "Напишите ваш вопрос — мастер ответит в ближайшее время.")
        elif p == "repair":
            self.states.pop(uid, None)
            await self.say(phone, REPAIR_TEXT, [("rpnew", "📝 Записаться"), ("rpstatus", "🔎 Статус ремонта"),
                                                ("home", "🏠 Меню")])
        elif p == "rpnew":
            self.states[uid] = {"state": "rp_device"}
            await self.say(phone, "<b>Шаг 1 из 2.</b> Что за устройство? Например: <b>iPhone 13</b>, "
                                  "<b>ноутбук ASUS TUF</b>, <b>PS5</b>")
        elif p == "rpstatus":
            self.states[uid] = {"state": "rp_number"}
            await self.say(phone, "Напишите номер заказа (он есть в квитанции), например <b>1234</b>.")
        elif p == "ti":
            self.states[uid] = {"state": "ti_model"}
            await self.say(phone, "💰 <b>Сдать устройство в трейд-ин</b>\n\nПримем смартфон, планшет, ноутбук или часы "
                                  "в зачёт покупки или выкупим. Мастер оценит и напишет сюда.\n\n"
                                  "<b>Шаг 1 из 4.</b> Какая модель? Например: <b>iPhone 13 128GB</b>")
        elif p == "tibat" and state.get("state") == "ti_battery":
            state["battery"] = "не знаю"
            await self.tradein_condition(phone, state)
        elif p.startswith("ticond:") and state.get("state") == "ti_condition":
            state["condition"] = CONDITIONS[int(p.split(":")[1])]
            await self.tradein_photos(phone, state)
        elif p in ("tiphotos", "tiskip") and state.get("state") == "ti_photos":
            self.states.pop(uid, None)
            await self.tradein_send(phone, uid, name, state)
        elif p.startswith(("tiyes:", "tinot:")):
            await self.tradein_answer(phone, uid, p.startswith("tiyes:"), int(p.split(":")[1]))
        elif p == "newsoff":
            db.set_news_off(uid)
            await self.say(phone, "Хорошо, больше не будем присылать новости.")
        elif p == "alertsoff":
            db.set_alerts_off(uid)
            await self.say(phone, "Хорошо, больше не будем сообщать о ценах.")
        else:
            await self.home(phone, uid)

    async def on_text(self, phone: str, uid: int, name: str, text: str) -> None:
        state = self.states.get(uid, {})
        step = state.get("state")
        if step == "lead":
            self.states.pop(uid, None)
            await notify_admins(self.tg, self.cfg, f"💬 <b>Вопрос</b> от {person(uid, name)}\n\n{html.escape(text)}",
                                reply_markup(uid))
            await self.home(phone, uid, "✅ Спасибо! Вопрос отправлен, скоро ответим.")
        elif step == "rp_device":
            state.update(state="rp_problem", device=text[:120])
            await self.say(phone, "<b>Шаг 2 из 2.</b> Что случилось? Опишите коротко: <b>разбит экран</b>, "
                                  "<b>не включается после воды</b>, <b>греется</b>")
        elif step == "rp_problem":
            self.states.pop(uid, None)
            await self.repair_request(phone, uid, name, state["device"], text[:1000])
        elif step == "rp_number":
            number = clean_number(text)
            if not number:
                await self.say(phone, "Номер заказа — это цифры, например <b>1234</b>.")
                return
            self.states.pop(uid, None)
            await self.repair_status(phone, uid, number, f"+{phone}")
        elif step == "rp_status_phone":
            self.states.pop(uid, None)
            await self.repair_status(phone, uid, state["number"], text, retry=False)
        elif step == "ti_model":
            state.update(state="ti_battery", model=" ".join(text.split())[:120])
            await self.say(phone, "<b>Шаг 2 из 4.</b> Какой процент аккумулятора? Например: <b>87</b>",
                           [("tibat", "Не знаю"), ("home", "✖ Отмена")])
        elif step == "ti_battery":
            digits = re.sub(r"\D", "", text)
            state["battery"] = f"{digits}%" if digits and 1 <= int(digits) <= 100 else text[:40]
            await self.tradein_condition(phone, state)
        elif step == "ti_condition":
            state["condition"] = text[:300]
            await self.tradein_photos(phone, state)
        else:  # без мастера любой текст — поиск по каталогу
            await self.search(phone, uid, text)

    async def search(self, phone: str, uid: int, query: str) -> None:
        hits, total = search(self.db, query)
        if not hits:
            await self.say(phone, f"🔍 По запросу «{html.escape(query[:60])}» ничего не нашлось. Попробуйте короче, "
                                  "например <b>15 Pro</b>, или спросите мастера — привезём под заказ.",
                           [("lead", "💬 Спросить"), ("catalog", "🛍 Каталог"), ("home", "🏠 Меню")])
            return
        rows = [(f"bi:{h.id}" if h.kind == "used" else f"fa:{h.id}", h.label, f"{fmt_price(h.price)} · {h.path}")
                for h in hits[:10]]
        more = " Показаны 10 самых подходящих — уточните запрос." if total > 10 else ""
        await self.api.menu(phone, f"🔍 Найдено: {total}.{more}\nОткройте список и выберите товар — "
                                   "он добавится в корзину.", "Товары", rows)

    async def on_image(self, phone: str, uid: int, media_id: str) -> None:
        state = self.states.get(uid, {})
        if state.get("state") != "ti_photos":
            await self.home(phone, uid, "Фото получили 🙂 Чтобы оценить устройство, выберите «Трейд-ин» в меню.")
            return
        if len(state["photos"]) < TRADEIN_PHOTOS:
            state["photos"].append(media_id)
        await self.say(phone, f"📷 Фото: {len(state['photos'])}. Пришлите ещё или нажмите «Готово».",
                       [("tiphotos", "✅ Готово"), ("home", "✖ Отмена")])

    # --- заказ ---
    async def checkout(self, phone: str, uid: int, name: str, delivery: str) -> None:
        self.states.pop(uid, None)
        lines = self.db.cart_lines(uid)
        if not any(line.available for line in lines):
            await self.say(phone, "Корзина пуста — товары закончились.", [("home", "🏠 Меню")])
            return
        order_id = self.db.create_order(uid, name, None, f"+{phone}", delivery, lines)
        order = self.db.order(order_id)
        await notify_admins(self.tg, self.cfg, render_order_for_admin(self.db, order), kb.order_actions(order_id))
        await self.home(phone, uid, f"✅ *Заказ №{order_id} оформлен!*\nСумма: {fmt_price(order['total'])}\n"
                                    f"Получение: {delivery}\nМы скоро свяжемся с вами для подтверждения.")

    # --- ремонт ---
    async def repair_request(self, phone: str, uid: int, name: str, device: str, problem: str) -> None:
        error = await send_lead(self.cfg, name, f"+{phone}", f"{device}: {problem}\n(заявка из WhatsApp-бота)")
        crm = "✅ записана в CRM" if error is None else f"⚠️ в CRM не попала: {html.escape(error)}"
        await notify_admins(self.tg, self.cfg, f"🔧 <b>Запись на ремонт</b>\n👤 {person(uid, name)}\n\n"
                                               f"<b>{html.escape(device)}</b>\n{html.escape(problem)}\n\n{crm}",
                            reply_markup(uid))
        await self.home(phone, uid, "✅ Заявка принята! Мастер перезвонит, подскажет цену и сроки.\n"
                                    "📍 Москва, Можайское ш., 4к1")

    async def repair_status(self, phone: str, uid: int, number: str, order_phone: str, retry: bool = True) -> None:
        try:
            found, crm_down = await order_status(self.cfg, number, order_phone), False
        except CrmUnavailable as e:
            log.warning("CRM: статус не получен: %s", e)
            found, crm_down = None, True
        if found:
            self.db.watch_crm(found["order_no"], order_phone, uid, found["status"])
            await self.say(phone, crm_text(found) + "\n\n🔔 Сообщим сюда, когда статус изменится.", [("home", "🏠 Меню")])
            return
        row = self.db.repair(number)
        if row:
            self.db.watch_repair(number, uid)
            await self.say(phone, status_text(self.cfg, row) + "\n\n🔔 Сообщим сюда, когда статус изменится.",
                           [("home", "🏠 Меню")])
            return
        if crm_down:
            await self.say(phone, "Не удалось связаться с системой заказов — попробуйте чуть позже.", [("home", "🏠 Меню")])
        elif retry:  # заказ мог быть оформлен на другой номер
            self.states[uid] = {"state": "rp_status_phone", "number": number}
            await self.say(phone, f"Заказ №{html.escape(number)} на этот номер WhatsApp не найден. Напишите телефон, "
                                  "который вы оставили при сдаче устройства.")
        else:
            await self.say(phone, f"Заказ №{html.escape(number)} с этим телефоном не найден. Проверьте номер заказа.",
                           [("rpstatus", "🔎 Ещё раз"), ("lead", "💬 Спросить"), ("home", "🏠 Меню")])

    # --- трейд-ин ---
    async def tradein_condition(self, phone: str, state: dict) -> None:
        state["state"] = "ti_condition"
        await self.api.menu(phone, html_to_wa("<b>Шаг 3 из 4.</b> Внешнее состояние? Выберите в списке или опишите "
                                              "своими словами (сколы, трещины, ремонты)."),
                            "Состояние", [(f"ticond:{i}", c, "") for i, c in enumerate(CONDITIONS)])

    async def tradein_photos(self, phone: str, state: dict) -> None:
        state.update(state="ti_photos", photos=[])
        await self.say(phone, f"<b>Шаг 4 из 4.</b> Пришлите до {TRADEIN_PHOTOS} фото устройства со всех сторон — "
                              "так оценка будет точнее. Можно пропустить.",
                       [("tiphotos", "✅ Готово"), ("tiskip", "Пропустить"), ("home", "✖ Отмена")])

    async def tradein_send(self, phone: str, uid: int, name: str, d: dict) -> None:
        files = []
        for no, media_id in enumerate(d.get("photos", []), start=1):
            try:
                files.append(BufferedInputFile(await self.api.download(media_id), f"tradein-{no}.jpg"))
            except Exception:
                log.exception("Трейд-ин: не скачалось фото из WhatsApp")
        req_id = self.db.add_tradein(uid, name, None, d["model"], d["battery"], d["condition"], [], f"+{phone}")
        text = (f"💰 <b>Заявка на трейд-ин №{req_id}</b>\n👤 {person(uid, name)}\n\n"
                f"<b>{html.escape(d['model'])}</b>\n🔋 АКБ: {html.escape(d['battery'])}\n"
                f"✨ Состояние: {html.escape(d['condition'])}")
        buttons = mk([("💬 Предложить цену", f"tio:{req_id}")], [("❌ Не принимаем", f"tino:{req_id}")],
                     [("✍️ Задать вопрос клиенту", f"ans:{uid}")])
        tg_ids: list[str] = []
        for admin_id in self.cfg.admin_ids:
            try:
                if files:
                    media = [InputMediaPhoto(media=tg_ids[i] if tg_ids else f) for i, f in enumerate(files)]
                    sent = await self.tg.send_media_group(admin_id, media)
                    tg_ids = tg_ids or [m.photo[-1].file_id for m in sent]
                await self.tg.send_message(admin_id, text, reply_markup=buttons)
            except Exception:
                log.exception("Трейд-ин: не удалось отправить заявку владельцу")
        if tg_ids:
            self.db.set_tradein_photos(req_id, tg_ids)
        await self.home(phone, uid, f"✅ Заявка №{req_id} отправлена! Мастер оценит устройство и напишет сюда, "
                                    "обычно в течение рабочего дня.")

    async def tradein_answer(self, phone: str, uid: int, agree: bool, req_id: int) -> None:
        req = self.db.tradein(req_id)
        if not req or req["user_id"] != uid:
            return
        self.db.set_tradein(req["id"], "agreed" if agree else "rejected")
        await self.say(phone, "🤝 Отлично! Мы свяжемся с вами, чтобы договориться о встрече. Адрес сервиса: "
                              "Москва, Можайское ш., 4к1." if agree else
                              "Понятно, спасибо! Если передумаете — пишите, будем рады помочь.")
        verdict = "✅ <b>согласен</b> — свяжитесь с ним" if agree else "❌ не согласен"
        await notify_admins(self.tg, self.cfg, f"💰 Трейд-ин №{req['id']} ({html.escape(req['model'])}): клиент "
                                               f"{verdict}.\n📱 {html.escape(req['phone'])}")

    # --- webhook ---
    async def process(self, phone: str, name: str, msg: dict) -> None:
        async with self.locks.setdefault(phone, asyncio.Lock()):
            try:
                await self.handle(phone, name, msg)
            except Exception as e:
                log.exception("Ошибка при обработке сообщения WhatsApp")
                await notify_admins(self.tg, self.cfg, f"⚠️ <b>Ошибка в боте WhatsApp</b>\n"
                                                       f"{html.escape(type(e).__name__)}: {html.escape(str(e))[:700]}")

    async def webhook_verify(self, request: web.Request) -> web.Response:
        """Meta проверяет адрес при подключении: возвращаем hub.challenge, если совпал наш токен."""
        q = request.query
        if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == self.cfg.wa_verify_token:
            return web.Response(text=q.get("hub.challenge", ""))
        return web.Response(status=403, text="wrong verify token")

    async def webhook(self, request: web.Request) -> web.Response:
        body = await request.read()
        if not verify_signature(self.cfg.wa_app_secret, body, request.headers.get("X-Hub-Signature-256")):
            log.warning("WhatsApp: запрос с неверной подписью отклонён")
            return web.Response(status=403)
        try:
            data = json.loads(body)
        except ValueError:
            return web.Response(status=400)
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value") or {}
                names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in value.get("contacts", [])}
                for msg in value.get("messages", []):
                    phone = msg.get("from", "")
                    if not phone.isdigit():
                        continue
                    task = asyncio.create_task(self.process(phone, names.get(phone) or f"+{phone}", msg))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
        return web.Response(text="ok")  # Meta ждёт быстрый ответ 200, обработка — в фоне

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/whatsapp", self.webhook_verify)
        app.router.add_post("/whatsapp", self.webhook)
        return app


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    if not (cfg.wa_token and cfg.wa_phone_id and cfg.wa_verify_token):
        raise SystemExit("Не заданы WA_TOKEN, WA_PHONE_ID и WA_VERIFY_TOKEN в .env")
    db = DB(cfg.db_path)
    tg = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    api = WaApi(cfg.wa_token, cfg.wa_phone_id, cfg.wa_api_version)
    runner = web.AppRunner(WaBot(api, tg, db, cfg).app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", cfg.wa_port).start()
    log.info("Бот WhatsApp ждёт сообщения на 127.0.0.1:%s/whatsapp", cfg.wa_port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await api.close()
        await tg.session.close()


if __name__ == "__main__":
    asyncio.run(main())
