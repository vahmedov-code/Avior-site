"""Бот AVIOR в мессенджере MAX — для клиентов, которым неудобен или недоступен Telegram.

Работает отдельным процессом рядом с Telegram-ботом, с той же базой: каталог, цены, б/у, корзина,
заказы и статистика общие. Экраны строятся теми же функциями, что и в Telegram (handlers_*),
кнопки переводятся в формат MAX. Управление остаётся в Telegram: заказы и заявки из MAX приходят
владельцу туда, с пометкой «MAX».

Запуск: python -m app.max_bot (служба avior-max)."""
from __future__ import annotations

import asyncio
import html
import io
import logging
import re
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

from . import keyboards as kb
from . import sources, visit
from .config import Config
from .crm import CrmUnavailable, order_status, send_lead
from .db import DB
from .handlers_repair import REPAIR_TEXT, booking_text, clean_number, crm_text, now_local, status_text
from .handlers_reply import reply_markup
from .handlers_search import render_results
from .handlers_used import CONDITIONS, card_markup, card_text, kind_list, showcase
from .handlers_user import (catalog_menu, catalog_text, has_device, main_menu, notify_admins, person,
                            render_cart, render_order_for_admin, render_section, welcome)
from .max_api import (MaxApi, MaxError, buttons_from_markup, contact_button, image, phone_from_contact,
                      to_internal)
from .pricing import fmt_price
from .stats import today

log = logging.getLogger("max")
TRADEIN_PHOTOS = 5
PHONE_DIGITS = re.compile(r"\D")


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def markup(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn(t, d) for t, d in row] for row in rows])


CANCEL = markup([("✖ Отмена", "home")])


@dataclass
class Client:
    """Кто пишет боту: id в базе (отрицательный), имя и ник в MAX."""
    max_id: int
    name: str
    username: str | None

    @property
    def uid(self) -> int:
        return to_internal(self.max_id)


@dataclass
class Event:
    client: Client
    text: str = ""
    payload: str = ""                 # данные кнопки
    callback_id: str | None = None    # нажатие кнопки — ответ заменяет сообщение с кнопками
    attachments: list[dict] = field(default_factory=list)


class MaxBot:
    def __init__(self, api: MaxApi, tg: Bot, db: DB, cfg: Config):
        self.api, self.tg, self.db, self.cfg = api, tg, db, cfg
        self.states: dict[int, dict] = {}       # мастер (заказ, ремонт, трейд-ин) у каждого клиента
        self.locks: dict[int, asyncio.Lock] = {}  # события одного клиента — по очереди
        self.tasks: set[asyncio.Task] = set()

    # --- вывод ---
    async def show(self, ev: Event, text: str, mk: InlineKeyboardMarkup | None = None,
                   attachments: list[dict] | None = None, note: str | None = None,
                   extra_rows: list[list[dict]] | None = None) -> None:
        """Нажатие кнопки — заменяем сообщение с кнопками (как правка в Telegram); текст — новое сообщение."""
        rows = buttons_from_markup(mk) + (extra_rows or [])
        if ev.callback_id:
            await self.api.answer(ev.callback_id, note, MaxApi.body(text, rows, attachments))
            ev.callback_id = None  # на нажатие уже ответили
        else:
            await self.api.send(ev.client.max_id, text, rows, attachments)

    async def say(self, ev: Event, text: str, mk: InlineKeyboardMarkup | None = None,
                  extra_rows: list[list[dict]] | None = None, note: str | None = None) -> None:
        """Новое сообщение; нажатую кнопку при этом подтверждаем коротким уведомлением."""
        if ev.callback_id:
            await self.api.answer(ev.callback_id, note or "✓")
            ev.callback_id = None
        await self.api.send(ev.client.max_id, text, buttons_from_markup(mk) + (extra_rows or []))

    async def toast(self, ev: Event, note: str) -> None:
        if ev.callback_id:
            await self.api.answer(ev.callback_id, note)
            ev.callback_id = None

    async def home(self, ev: Event) -> None:
        self.states.pop(ev.client.uid, None)
        await self.show(ev, welcome(self.cfg), main_menu(self.db, ev.client.uid, self.cfg))

    async def ask_phone(self, ev: Event, text: str) -> None:
        await self.say(ev, text + "\n\nНажмите кнопку ниже или напишите номер сообщением.",
                       extra_rows=[[contact_button()], [{"type": "callback", "text": "✖ Отмена", "payload": "home"}]])

    def phone_of(self, ev: Event) -> str | None:
        for a in ev.attachments:
            if a.get("type") == "contact":
                return phone_from_contact(a.get("payload") or {})
        if ev.text and len(PHONE_DIGITS.sub("", ev.text)) >= 10:
            return ev.text.strip()
        return None

    async def used_photo(self, tg_file_id: str) -> dict:
        """Фото б/у хранятся как file_id Telegram: один раз скачиваем, загружаем в MAX и запоминаем."""
        cached = self.db.max_photo(tg_file_id)
        if cached:
            return cached
        buf = io.BytesIO()
        await self.tg.download(tg_file_id, destination=buf)
        payload = await self.api.upload_image(buf.getvalue())
        self.db.save_max_photo(tg_file_id, payload)
        return payload

    # --- разбор событий ---
    async def handle(self, update: dict) -> None:
        kind = update.get("update_type")
        if kind == "bot_started":
            u = update["user"]
            ev = Event(Client(u["user_id"], u.get("first_name") or u.get("name") or "", u.get("username")))
            self.track(ev.client)
            await self.start(ev, update.get("payload") or "")
        elif kind == "message_created":
            msg = update["message"]
            if (msg.get("recipient") or {}).get("chat_type") not in (None, "dialog"):
                return  # в группах и каналах бот не отвечает
            u = msg.get("sender") or {}
            if not u or u.get("is_bot"):
                return
            body = msg.get("body") or {}
            ev = Event(Client(u["user_id"], u.get("first_name") or "", u.get("username")),
                       text=(body.get("text") or "").strip(), attachments=body.get("attachments") or [])
            self.track(ev.client)
            await self.on_message(ev)
        elif kind == "message_callback":
            cb = update["callback"]
            u = cb["user"]
            ev = Event(Client(u["user_id"], u.get("first_name") or "", u.get("username")),
                       payload=cb.get("payload") or "", callback_id=cb["callback_id"])
            self.track(ev.client)
            await self.on_callback(ev)
            if ev.callback_id:  # обработчик ничего не ответил — снимаем «часики» с кнопки
                await self.api.answer(ev.callback_id, "✓")

    def track(self, c: Client) -> None:
        try:
            self.db.track_visit(c.uid, c.username, c.name, today(self.cfg).date().isoformat())
        except Exception:
            log.exception("Статистика: не удалось записать посещение")

    async def start(self, ev: Event, payload: str) -> None:
        self.states.pop(ev.client.uid, None)
        if re.fullmatch(r"bu\d+", payload):  # «Купить в боте» из поста про б/у
            self.db.set_source_if_new(ev.client.uid, "channel")
            row = self.db.used(int(payload[2:]))
            if row and row["status"] == "active":
                await self.used_card(ev, row)
                return
        source = sources.clean(payload)
        if source:
            self.db.set_source_if_new(ev.client.uid, source)
        await self.home(ev)

    # --- кнопки ---
    async def on_callback(self, ev: Event) -> None:
        p, uid, db, cfg = ev.payload, ev.client.uid, self.db, self.cfg
        state = self.states.get(uid, {})
        if p == "home":
            await self.home(ev)
        elif p == "catalog":
            self.states.pop(uid, None)
            await self.show(ev, catalog_text(cfg), catalog_menu(db, uid, cfg))
        elif p == "contacts":
            await self.show(ev, f"<b>{html.escape(cfg.shop_name)}</b>\n\n{html.escape(cfg.contacts_text)}",
                            InlineKeyboardMarkup(inline_keyboard=[[kb.BTN_QUESTION], [kb.BTN_HOME]]))
        elif p.startswith("c:"):
            await self.category(ev, int(p[2:]))
        elif p.startswith("s:"):
            _, cat_id, sec_idx, page = p.split(":")
            cat = db.category(int(cat_id))
            if not cat or int(sec_idx) >= len(db.sections(cat["id"])):
                await self.toast(ev, "Прайс обновился, откройте категорию заново")
                return
            await self.show(ev, *render_section(db, cfg, uid, cat, int(sec_idx), int(page)))
        elif p.startswith("a:"):
            _, item_id, page = p.split(":")
            item = db.item(int(item_id))
            if not item:
                await self.toast(ev, "Прайс обновился — откройте раздел заново")
                return
            db.add_to_cart(uid, item)
            cat = db.category(item["category_id"])
            sec_idx = db.sections(cat["id"]).index(item["section"])
            await self.show(ev, *render_section(db, cfg, uid, cat, sec_idx, int(page)),
                            note=f"✅ В корзине: {item['name']}"[:200])
        elif p.startswith("fa:"):
            item = db.item(int(p[3:]))
            if item:
                db.add_to_cart(uid, item)
            await self.toast(ev, f"✅ В корзине: {item['name']}"[:200] if item else "Прайс обновился — повторите поиск")
        elif p == "find":
            await self.say(ev, "🔍 Напишите, что ищете — модель, память, цвет. Например:\n"
                               "<code>15 Pro 256</code> · <code>macbook air m3</code> · <code>airpods pro</code>",
                           InlineKeyboardMarkup(inline_keyboard=[[kb.BTN_CATALOG, kb.BTN_HOME]]))
        elif p in ("cart", "cartclear") or p.startswith("rm:"):
            if p == "cartclear":
                db.clear_cart(uid)
            elif p.startswith("rm:"):
                db.remove_cart_line(uid, int(p[3:]))
            self.states.pop(uid, None)
            lines = db.cart_lines(uid)
            await self.show(ev, render_cart(lines), kb.cart_menu(lines) if lines else kb.back_home())
        elif p == "checkout":
            lines = db.cart_lines(uid)
            if not any(line.available for line in lines):
                await self.toast(ev, "В корзине нет доступных товаров")
                return
            self.states[uid] = {"state": "delivery"}
            await self.show(ev, render_cart(lines).rsplit("\n\n", 1)[0] + "\n\nКак удобно получить заказ?",
                            kb.delivery_menu(cfg.delivery_options))
        elif p.startswith("d:") and state.get("state") == "delivery":
            idx = int(p[2:])
            if idx >= len(cfg.delivery_options):
                return
            self.states[uid] = {"state": "services", "delivery": cfg.delivery_options[idx], "services": []}
            if cfg.services and has_device(db.cart_lines(uid)):
                await self.show(ev, "🔧 <b>Услуги сервиса AVIOR к покупке</b>\nОтметьте, что нужно, — мастер сделает "
                                    "при выдаче. Стоимость менеджер скажет при подтверждении заказа.",
                                kb.services_menu(cfg.services, set()))
            else:
                await self.checkout_phone_step(ev)
        elif p.startswith("sv:") and state.get("state") == "services":
            chosen = set(state["services"]) ^ {int(p[3:])}
            state["services"] = sorted(chosen)
            await self.show(ev, "🔧 <b>Услуги сервиса AVIOR к покупке</b>\nОтметьте, что нужно, — мастер сделает "
                                "при выдаче. Стоимость менеджер скажет при подтверждении заказа.",
                            kb.services_menu(cfg.services, chosen))
        elif p == "svdone" and state.get("state") == "services":
            await self.checkout_phone_step(ev)
        elif p == "lead":
            self.states[uid] = {"state": "lead"}
            await self.say(ev, "Напишите ваш вопрос — мастер ответит в ближайшее время.", CANCEL)
        elif p == "bu":
            self.states.pop(uid, None)
            await self.show(ev, *showcase(db, cfg, uid))
        elif p.startswith("bk:"):
            screen = kind_list(db, p[3:], uid)
            if screen:
                await self.show(ev, *screen)
            else:
                await self.toast(ev, "В этом разделе всё разобрали")
        elif p.startswith("bi:"):
            row = db.used(int(p[3:]))
            if not row or row["status"] != "active":
                await self.toast(ev, "Это устройство уже продано")
                return
            await self.used_card(ev, row)
        elif p.startswith("bp:"):
            row = db.used(int(p[3:]))
            if row:
                photos = [image(await self.used_photo(f)) for f in db.used_photos(row)[:10]]
                await self.toast(ev, "📷 Фото ниже")
                await self.api.send(ev.client.max_id, f"📷 {html.escape(row['model'])}", attachments=photos)
        elif p.startswith("bc:"):
            row = db.used(int(p[3:]))
            if not row or row["status"] != "active":
                await self.toast(ev, "Это устройство уже продано")
                return
            db.add_used_to_cart(uid, row)
            await self.used_card(ev, row, note=f"✅ В корзине: {row['model']}"[:200])
        elif p == "repair":
            self.states.pop(uid, None)
            await self.show(ev, REPAIR_TEXT, markup([("📝 Записаться на ремонт", "rpnew")],
                                                    [("🔎 Статус ремонта", "rpstatus")], [("🏠 В меню", "home")]))
        elif p == "rpnew":
            self.states[uid] = {"state": "rp_device"}
            await self.say(ev, "<b>Шаг 1 из 4.</b> Что за устройство? Например: <code>iPhone 13</code>, "
                               "<code>ноутбук ASUS TUF</code>, <code>PS5</code>", CANCEL)
        elif p == "rpdays" and state.get("state") == "rp_visit":
            await self.show(ev, visit.DAYS_TEXT, visit.days_markup(now_local(cfg)))
        elif p.startswith("rpd:") and state.get("state") == "rp_visit":
            code = p[4:]
            if code == visit.PHONE_CHOICE:
                await self.repair_submit(ev, state, visit.describe(code, None))
                return
            slots = visit.slots_markup(code, now_local(cfg))
            if slots is None:
                await self.show(ev, visit.DAYS_TEXT, visit.days_markup(now_local(cfg)),
                                note="На этот день запись уже закрыта — выберите другой")
            else:
                await self.show(ev, f"🗓 <b>{visit.day_title(code)}</b>. Выберите удобное время:", slots)
        elif p.startswith("rps:") and state.get("state") == "rp_visit":
            _, code, slot = p.split(":")
            day = visit.parse(code)
            if not day or int(slot) not in visit.free_slots(day, now_local(cfg)):
                await self.show(ev, visit.DAYS_TEXT, visit.days_markup(now_local(cfg)),
                                note="Это время уже прошло — выберите другое")
            else:
                await self.repair_submit(ev, state, visit.describe(code, int(slot)))
        elif p == "rpstatus":
            self.states[uid] = {"state": "rp_number"}
            await self.say(ev, "Напишите номер заказа (он есть в квитанции), например <code>1234</code>.", CANCEL)
        elif p == "ti":
            self.states[uid] = {"state": "ti_model"}
            await self.say(ev, "💰 <b>Сдать устройство в трейд-ин</b>\n\nПримем ваш смартфон, планшет, ноутбук или "
                               "часы в зачёт покупки или выкупим. Ответьте на 5 коротких вопросов — мастер оценит "
                               "и напишет сюда.\n\n<b>Шаг 1 из 5.</b> Какая модель? Например: <code>iPhone 13 128GB</code>",
                           CANCEL)
        elif p == "tibat" and state.get("state") == "ti_battery":
            state["battery"] = "не знаю"
            await self.tradein_condition(ev)
        elif p.startswith("ticond:") and state.get("state") == "ti_condition":
            state["condition"] = CONDITIONS[int(p.split(":")[1])]
            await self.tradein_photos(ev)
        elif p == "tiphotos" and state.get("state") == "ti_photos":
            state["state"] = "ti_phone"
            await self.ask_phone(ev, "<b>Шаг 5 из 5.</b> Оставьте номер телефона.")
        elif p.startswith(("tiyes:", "tinot:")):
            await self.tradein_answer(ev, p.startswith("tiyes:"), int(p.split(":")[1]))
        elif p == "newsoff":
            db.set_news_off(uid)
            await self.toast(ev, "Больше не будем присылать новости")
        elif p == "alertsoff":
            db.set_alerts_off(uid)
            await self.toast(ev, "Больше не будем сообщать о ценах")

    async def category(self, ev: Event, cat_id: int) -> None:
        cat = self.db.category(cat_id)
        sections = self.db.sections(cat["id"]) if cat else []
        if not sections:
            await self.toast(ev, "Цены скоро появятся")
            return
        self.db.track_category_view(cat["id"], today(self.cfg).date().isoformat())
        if len(sections) == 1:
            await self.show(ev, *render_section(self.db, self.cfg, ev.client.uid, cat, 0, 0))
        else:
            await self.show(ev, f"<b>{html.escape(cat['title'])}</b>\n\nВыберите раздел:",
                            kb.sections_menu(cat["id"], sections, self.db.cart_count(ev.client.uid)))

    async def used_card(self, ev: Event, row, note: str | None = None) -> None:
        photos = self.db.used_photos(row)
        attachments = [image(await self.used_photo(photos[0]))] if photos else None
        await self.show(ev, card_text(self.cfg, row), card_markup(self.db, self.cfg, row, ev.client.uid),
                        attachments, note)

    # --- текст и вложения ---
    async def on_message(self, ev: Event) -> None:
        uid = ev.client.uid
        if ev.text.startswith("/start"):
            await self.start(ev, ev.text[6:].strip())
            return
        state = self.states.get(uid, {})
        step = state.get("state")
        if step == "lead" and ev.text:
            self.states.pop(uid, None)
            await notify_admins(self.tg, self.cfg, f"💬 <b>Вопрос</b> от {self.who(ev)}\n\n{html.escape(ev.text)}",
                                reply_markup(uid))
            await self.say(ev, "✅ Спасибо! Вопрос отправлен, скоро ответим.")
            await self.home(ev)
        elif step == "phone":
            await self.checkout_finish(ev)
        elif step and step.startswith("rp_"):
            await self.repair_step(ev, state)
        elif step and step.startswith("ti_"):
            await self.tradein_step(ev, state)
        elif ev.text:  # без мастера любой текст — поиск
            text, mk = render_results(self.db, ev.text, uid)
            await self.say(ev, text, mk)
        else:
            await self.home(ev)

    def who(self, ev: Event) -> str:
        return person(ev.client.uid, ev.client.name, ev.client.username)

    # --- оформление заказа ---
    async def checkout_phone_step(self, ev: Event) -> None:
        state = self.states[ev.client.uid]
        state["state"] = "phone"
        await self.ask_phone(ev, f"Доставка: {html.escape(state['delivery'])}")

    async def checkout_finish(self, ev: Event) -> None:
        uid, db, cfg = ev.client.uid, self.db, self.cfg
        phone = self.phone_of(ev)
        if not phone:
            await self.ask_phone(ev, "Не похоже на номер телефона. Пример: +7 900 123-45-67")
            return
        state = self.states.pop(uid)
        lines = db.cart_lines(uid)
        if not any(line.available for line in lines):
            await self.say(ev, "Корзина пуста — товары закончились.")
            return
        services = [cfg.services[i] for i in state.get("services", []) if i < len(cfg.services)]
        order_id = db.create_order(uid, ev.client.name, ev.client.username, phone, state["delivery"], lines, services)
        order = db.order(order_id)
        await notify_admins(self.tg, cfg, render_order_for_admin(db, order), kb.order_actions(order_id))
        await self.say(ev, f"✅ <b>Заказ №{order_id} оформлен!</b>\n\nСумма: {fmt_price(order['total'])}\n"
                           f"Получение: {html.escape(order['delivery'])}\n"
                           + (f"Услуги: {html.escape(', '.join(services))}\n" if services else "")
                           + "\nМы скоро свяжемся с вами для подтверждения.")
        await self.home(ev)

    # --- ремонт ---
    async def repair_step(self, ev: Event, state: dict) -> None:
        uid, step = ev.client.uid, state["state"]
        if step == "rp_device" and ev.text:
            state.update(state="rp_problem", device=ev.text[:120])
            await self.say(ev, "<b>Шаг 2 из 4.</b> Что случилось? Опишите коротко: <code>разбит экран</code>, "
                               "<code>не включается после воды</code>, <code>греется</code>", CANCEL)
        elif step == "rp_problem" and ev.text:
            state.update(state="rp_phone", problem=ev.text[:1000])
            await self.ask_phone(ev, "<b>Шаг 3 из 4.</b> Оставьте номер телефона — мастер перезвонит и подскажет "
                                     "цену и сроки.")
        elif step == "rp_phone":
            phone = self.phone_of(ev)
            if not phone:
                await self.ask_phone(ev, "Не похоже на номер телефона. Пример: +7 900 123-45-67")
                return
            state.update(state="rp_visit", phone=phone)
            await self.say(ev, visit.DAYS_TEXT, visit.days_markup(now_local(self.cfg)))
        elif step == "rp_number" and ev.text:
            number = clean_number(ev.text)
            if not number:
                await self.say(ev, "Номер заказа — это цифры, например <code>1234</code>.", CANCEL)
                return
            state.update(state="rp_status_phone", number=number)
            await self.ask_phone(ev, "И номер телефона, который вы оставили при сдаче устройства.")
        elif step == "rp_status_phone":
            phone = self.phone_of(ev)
            if not phone:
                await self.ask_phone(ev, "Не похоже на номер телефона. Пример: +7 900 123-45-67")
                return
            self.states.pop(uid, None)
            await self.repair_status(ev, state["number"], phone)

    async def repair_submit(self, ev: Event, state: dict, when: str) -> None:
        """Последний шаг записи пройден: заявка — в CRM и владельцу, клиенту — подтверждение."""
        uid = ev.client.uid
        self.states.pop(uid, None)
        nick = f", @{ev.client.username}" if ev.client.username else ""
        error = await send_lead(self.cfg, ev.client.name, state["phone"],
                                f"{state['device']}: {state['problem']}\nУдобное время: {when}\n(заявка из MAX-бота{nick})")
        crm = "✅ записана в CRM" if error is None else f"⚠️ в CRM не попала: {html.escape(error)}"
        await notify_admins(self.tg, self.cfg,
                            booking_text(self.who(ev), state["phone"], state["device"], state["problem"], when, crm),
                            reply_markup(uid))
        await self.show(ev, f"✅ <b>Заявка принята!</b>\n🗓 {html.escape(when)}\n\n"
                            "Мастер перезвонит, подскажет цену и сроки.\n📍 Москва, Можайское ш., 4к1")
        await self.home(ev)

    async def repair_status(self, ev: Event, number: str, phone: str) -> None:
        uid = ev.client.uid
        try:
            found, crm_down = await order_status(self.cfg, number, phone), False
        except CrmUnavailable as e:
            log.warning("CRM: статус не получен: %s", e)
            found, crm_down = None, True
        done_rows = [[("🏠 В меню", "home")]]
        if found:
            self.db.watch_crm(found["order_no"], phone, uid, found["status"])
            await self.say(ev, crm_text(found) + "\n\n🔔 Сообщим сюда, когда статус изменится.", markup(*done_rows))
            return
        row = self.db.repair(number)
        if row:
            self.db.watch_repair(number, uid)
            await self.say(ev, status_text(self.cfg, row) + "\n\n🔔 Сообщим сюда, когда статус изменится.",
                           markup(*done_rows))
            return
        text = ("Не удалось связаться с системой заказов — попробуйте чуть позже." if crm_down else
                f"Заказ №{html.escape(number)} с этим телефоном не найден. Проверьте номер заказа и телефон.")
        await self.say(ev, text, markup([("🔎 Попробовать ещё раз", "rpstatus")], [("💬 Задать вопрос", "lead")],
                                        [("🏠 В меню", "home")]))

    # --- трейд-ин ---
    async def tradein_condition(self, ev: Event) -> None:
        state = self.states[ev.client.uid]
        state["state"] = "ti_condition"
        rows = [[(c, f"ticond:{i}") for i, c in list(enumerate(CONDITIONS))[j:j + 2]] for j in (0, 2)]
        await self.say(ev, "<b>Шаг 3 из 5.</b> Внешнее состояние? Выберите или опишите своими словами "
                           "(сколы, трещины, ремонты).", markup(*rows, [("✖ Отмена", "home")]))

    async def tradein_photos(self, ev: Event) -> None:
        state = self.states[ev.client.uid]
        state.update(state="ti_photos", photos=[])
        await self.say(ev, f"<b>Шаг 4 из 5.</b> Пришлите до {TRADEIN_PHOTOS} фото устройства со всех сторон — "
                           "так оценка будет точнее. Можно пропустить.",
                       markup([("✅ Готово", "tiphotos"), ("Пропустить", "tiphotos")], [("✖ Отмена", "home")]))

    async def tradein_step(self, ev: Event, state: dict) -> None:
        step = state["state"]
        if step == "ti_model" and ev.text:
            state.update(state="ti_battery", model=" ".join(ev.text.split())[:120])
            await self.say(ev, "<b>Шаг 2 из 5.</b> Какой процент аккумулятора? Например: <code>87</code>",
                           markup([("Не знаю", "tibat")], [("✖ Отмена", "home")]))
        elif step == "ti_battery" and ev.text:
            digits = PHONE_DIGITS.sub("", ev.text)
            state["battery"] = f"{digits}%" if digits and 1 <= int(digits) <= 100 else ev.text[:40]
            await self.tradein_condition(ev)
        elif step == "ti_condition" and ev.text:
            state["condition"] = ev.text[:300]
            await self.tradein_photos(ev)
        elif step == "ti_photos":
            urls = [(a.get("payload") or {}).get("url") for a in ev.attachments if a.get("type") == "image"]
            room = TRADEIN_PHOTOS - len(state["photos"])
            state["photos"] += [u for u in urls if u][:max(0, room)]
            await self.say(ev, f"📷 Фото: {len(state['photos'])}. Можно добавить ещё или нажать «✅ Готово».",
                           markup([("✅ Готово", "tiphotos")], [("✖ Отмена", "home")]))
        elif step == "ti_phone":
            phone = self.phone_of(ev)
            if not phone:
                await self.ask_phone(ev, "Не похоже на номер телефона. Пример: +7 900 123-45-67")
                return
            self.states.pop(ev.client.uid, None)
            await self.tradein_send(ev, state, phone)

    async def tradein_send(self, ev: Event, d: dict, phone: str) -> None:
        """Фото из MAX пересылаем владельцу в Telegram и храним уже как file_id Telegram."""
        files = []
        for no, url in enumerate(d.get("photos", []), start=1):
            try:
                files.append(BufferedInputFile(await self.api.download(url), f"tradein-{no}.jpg"))
            except Exception:
                log.exception("Трейд-ин: не скачалось фото %s", url[:80])
        c = ev.client
        tg_ids: list[str] = []
        req_id = self.db.add_tradein(c.uid, c.name, c.username, d["model"], d["battery"], d["condition"], [], phone)
        text = (f"💰 <b>Заявка на трейд-ин №{req_id}</b>\n👤 {self.who(ev)}\n📱 {html.escape(phone)}\n\n"
                f"<b>{html.escape(d['model'])}</b>\n🔋 АКБ: {html.escape(d['battery'])}\n"
                f"✨ Состояние: {html.escape(d['condition'])}")
        buttons_mk = markup([("💬 Предложить цену", f"tio:{req_id}")], [("❌ Не принимаем", f"tino:{req_id}")],
                            [("✍️ Задать вопрос клиенту", f"ans:{c.uid}")])
        for admin_id in self.cfg.admin_ids:
            try:
                if files:
                    media = [InputMediaPhoto(media=f if not tg_ids else tg_ids[i]) for i, f in enumerate(files)]
                    sent = await self.tg.send_media_group(admin_id, media)
                    tg_ids = tg_ids or [m.photo[-1].file_id for m in sent]
                await self.tg.send_message(admin_id, text, reply_markup=buttons_mk)
            except Exception:
                log.exception("Трейд-ин: не удалось отправить заявку владельцу")
        if tg_ids:
            self.db.set_tradein_photos(req_id, tg_ids)
        await self.say(ev, f"✅ Заявка №{req_id} отправлена! Мастер оценит устройство и напишет сюда, "
                           "обычно в течение рабочего дня.")
        await self.home(ev)

    async def tradein_answer(self, ev: Event, agree: bool, req_id: int) -> None:
        req = self.db.tradein(req_id)
        if not req or req["user_id"] != ev.client.uid:
            return
        self.db.set_tradein(req["id"], "agreed" if agree else "rejected")
        await self.say(ev, "🤝 Отлично! Мы свяжемся с вами, чтобы договориться о встрече. Адрес сервиса: "
                           "Москва, Можайское ш., 4к1." if agree else
                           "Понятно, спасибо! Если передумаете — пишите, будем рады помочь.")
        verdict = "✅ <b>согласен</b> — свяжитесь с ним" if agree else "❌ не согласен"
        await notify_admins(self.tg, self.cfg, f"💰 Трейд-ин №{req['id']} ({html.escape(req['model'])}): клиент "
                                               f"{verdict}.\n📱 {html.escape(req['phone'])}")

    # --- цикл ---
    async def process(self, update: dict) -> None:
        user = (update.get("user") or (update.get("callback") or {}).get("user")
                or (update.get("message") or {}).get("sender") or {})
        lock = self.locks.setdefault(user.get("user_id", 0), asyncio.Lock())
        async with lock:
            try:
                await self.handle(update)
            except Exception as e:
                log.exception("Ошибка при обработке события MAX")
                await notify_admins(self.tg, self.cfg, f"⚠️ <b>Ошибка в боте MAX</b>\n"
                                                       f"{html.escape(type(e).__name__)}: {html.escape(str(e))[:700]}")

    async def run(self) -> None:
        me = await self.api.me()
        log.info("Бот MAX запущен: %s (@%s)", me.get("first_name") or me.get("name"), me.get("username"))
        marker = None
        while True:
            try:
                data = await self.api.updates(marker)
            except (MaxError, asyncio.TimeoutError, OSError) as e:
                log.warning("MAX: обновления не получены (%s), повтор через 5 с", e)
                await asyncio.sleep(5)
                continue
            marker = data.get("marker", marker)
            for update in data.get("updates", []):
                task = asyncio.create_task(self.process(update))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    if not cfg.max_token:
        raise SystemExit("MAX_TOKEN не задан: впишите в .env токен бота MAX (business.max.ru)")
    db = DB(cfg.db_path)
    tg = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    api = MaxApi(cfg.max_token, cfg.max_api_url, cfg.max_ca_file)
    try:
        await MaxBot(api, tg, db, cfg).run()
    finally:
        await api.close()
        await tg.session.close()


if __name__ == "__main__":
    asyncio.run(main())
