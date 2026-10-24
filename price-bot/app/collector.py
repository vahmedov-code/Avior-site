"""Браузерный сборщик: читает канал поставщика через Telegram Web (web.telegram.org/a)
от имени обычного аккаунта, без api_id. Вход — один раз по QR-коду, который бот
присылает владельцу; сессия хранится в data/browser.

Сборщик только читает тексты постов. Какие посты к какой категории относятся, он знает
из меню, запомненного при загрузке экспорта (в веб-версии ссылки кнопок не видны).
Если канал прочитан не полностью — цены не трогаем, а владельцу уходит скриншот.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from aiogram import Bot
from aiogram.types import BufferedInputFile

from .catalog_import import catalog_filters, import_catalog, supplier
from .config import Config
from .db import DB
from .telegram_export import TITLE_JUNK_RE, export_from_posts, load_any

log = logging.getLogger("collector")

WEB_URL = os.getenv("COLLECTOR_WEB_URL", "https://web.telegram.org/a/")
LOGGED_IN_SEL = "#LeftColumn, .chat-list, #MiddleColumn"
QR_SEL = ".qr-container, #auth-qr-form"
PASSWORD_SEL = "input#sign-in-password, #auth-password-form input[type=password]"
MESSAGE_SEL = "[data-message-id]"
MIN_COVERAGE = 0.8  # доля постов из меню, которые обязательно должны прочитаться
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# Текст каждого поста. Время/просмотры (MessageMeta) прячем, чтобы не попали в текст.
EXTRACT_JS = """() => {
  document.querySelectorAll('.MessageMeta, .message-meta').forEach(m => m.style.display = 'none');
  return [...document.querySelectorAll('[data-message-id]')].map(el => {
    const textEl = el.querySelector('.text-content') || el.querySelector('.message-content');
    const buttons = [...el.querySelectorAll('.InlineButtons button')].map(b => b.innerText.trim()).filter(Boolean);
    return {id: Number(el.getAttribute('data-message-id')), text: textEl ? textEl.innerText : '', buttons};
  });
}"""
SCROLL_UP_JS = """() => {
  const list = document.querySelector('.MessageList');
  if (!list) return false;
  list.scrollTop = 0;
  return true;
}"""


class CollectorError(Exception):
    pass


def browser_dir(cfg: Config) -> str:
    return str(Path(cfg.db_path).parent / "browser")


def debug_dir(cfg: Config) -> Path:
    path = Path(cfg.db_path).parent / "collector-debug"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def open_browser(playwright, cfg: Config):
    options = {"headless": True, "viewport": {"width": 1280, "height": 1600}, "locale": "ru-RU",
               "user_agent": USER_AGENT}
    if os.getenv("COLLECTOR_CHROMIUM"):
        options["executable_path"] = os.getenv("COLLECTOR_CHROMIUM")
    return await playwright.chromium.launch_persistent_context(browser_dir(cfg), **options)


async def notify(bot: Bot, cfg: Config, text: str, photo: bytes | None = None) -> None:
    for admin_id in cfg.admin_ids:
        try:
            if photo:
                await bot.send_photo(admin_id, BufferedInputFile(photo, "screen.png"), caption=text[:1000])
            else:
                await bot.send_message(admin_id, text)
        except Exception:
            log.exception("Не удалось отправить сообщение владельцу")


async def save_debug(page, cfg: Config, tag: str) -> bytes:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    png = await page.screenshot(full_page=False)
    (debug_dir(cfg) / f"{stamp}-{tag}.png").write_bytes(png)
    (debug_dir(cfg) / f"{stamp}-{tag}.html").write_text(await page.content(), encoding="utf-8")
    return png


async def is_logged_in(page) -> bool:
    return await page.locator(LOGGED_IN_SEL).count() > 0


# --- вход по QR-коду ---

async def login(cfg: Config, bot: Bot, timeout: int = 300) -> bool:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        context = await open_browser(pw, cfg)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(WEB_URL, wait_until="domcontentloaded")
        deadline = time.monotonic() + timeout
        last_qr = 0.0
        sent_debug = False
        try:
            while time.monotonic() < deadline:
                await page.wait_for_timeout(3000)
                if await is_logged_in(page):
                    await notify(bot, cfg, "✅ Сборщик вошёл в Telegram. Теперь он сможет сам обновлять цены.")
                    return True
                if await page.locator(PASSWORD_SEL).count():
                    password = os.getenv("COLLECTOR_PASSWORD", "")
                    if not password:
                        await notify(bot, cfg, "🔐 У аккаунта сборщика включён облачный пароль. Впишите его в .env "
                                               "строкой COLLECTOR_PASSWORD=… и запустите вход ещё раз.")
                        return False
                    await page.locator(PASSWORD_SEL).first.fill(password)
                    await page.keyboard.press("Enter")
                    continue
                qr = page.locator(QR_SEL).first
                if await qr.count() and time.monotonic() - last_qr > 30:
                    await notify(bot, cfg, "📷 Вход сборщика: откройте Telegram на телефоне → Настройки → Устройства → "
                                           "«Подключить устройство» и отсканируйте этот код. Код обновляется, "
                                           "через 30 секунд пришлю новый.", await qr.screenshot())
                    last_qr = time.monotonic()
                elif not await qr.count() and not sent_debug and time.monotonic() > deadline - timeout + 25:
                    await notify(bot, cfg, "Не вижу QR-кода на странице входа, вот что показывает браузер:",
                                 await save_debug(page, cfg, "login"))
                    sent_debug = True
            await notify(bot, cfg, "⌛ Вход сборщика не выполнен за 5 минут. Запустите вход ещё раз.",
                         await save_debug(page, cfg, "login-timeout"))
            return False
        finally:
            await context.close()


# --- чтение канала ---

async def read_channel(page, chat_id: int, max_rounds: int = 80) -> dict[int, dict]:
    await page.goto(f"{WEB_URL}#{chat_id}", wait_until="domcontentloaded")
    await page.wait_for_selector(MESSAGE_SEL, timeout=60_000)
    posts: dict[int, dict] = {}
    stable = 0
    for _ in range(max_rounds):
        before = len(posts)
        for post in await page.evaluate(EXTRACT_JS):
            old = posts.get(post["id"])
            if not old or len(post["text"]) > len(old["text"]):
                posts[post["id"]] = post
        if not await page.evaluate(SCROLL_UP_JS):
            break
        await page.wait_for_timeout(1500)
        stable = stable + 1 if len(posts) == before else 0
        if stable >= 4:  # четыре прокрутки подряд ничего нового — дошли до начала канала
            break
    return posts


def _norm(title: str) -> str:
    return TITLE_JUNK_RE.sub("", title).strip().lower()


async def collect(cfg: Config, db: DB, bot: Bot, report_always: bool = False) -> bool:
    from playwright.async_api import async_playwright

    info = supplier(db)
    if not info:
        await notify(bot, cfg, "Сборщик: сначала один раз отправьте боту экспорт канала (result.json) — "
                               "по нему я узнаю меню поставщика.")
        return False

    chat_id = int(f"-100{info['id']}") if "channel" in info["type"] or "supergroup" in info["type"] else -info["id"]
    async with async_playwright() as pw:
        context = await open_browser(pw, cfg)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(WEB_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            if not await is_logged_in(page):
                await notify(bot, cfg, "🔑 Сборщик не вошёл в Telegram (сессия закончилась). Запустите вход: "
                                       "<code>./collector.sh login</code>", await save_debug(page, cfg, "not-logged-in"))
                return False
            posts = await read_channel(page, chat_id)
            menu_ids = {msg_id for _, msg_id in info["menu"]}
            found = menu_ids & posts.keys()
            if len(found) < MIN_COVERAGE * len(menu_ids):
                raise CollectorError(f"прочитано {len(found)} из {len(menu_ids)} постов меню")
        except Exception as e:
            await notify(bot, cfg, f"⚠️ Сборщик не смог прочитать канал ({html.escape(str(e))[:300]}). "
                                   "Цены не изменены. Вот что видит браузер:", await save_debug(page, cfg, "error"))
            log.exception("Сбор не удался")
            return False
        finally:
            await context.close()

    texts = {i: p["text"] for i, p in posts.items()}
    catalog = load_any(export_from_posts(info, texts), **catalog_filters(db, cfg))
    if not catalog.categories:
        await notify(bot, cfg, "⚠️ Сборщик прочитал канал, но не нашёл цен. Цены не изменены.")
        return False
    results = import_catalog(db, cfg, catalog)

    # Сравниваем со всеми кнопками меню, включая служебные (VK, Trade-In, Инструкция…)
    known = {_norm(t) for t in info.get("all_buttons", [])} | {_norm(t) for t, _ in info["menu"]}
    menu_post = posts.get(info["menu_msg_id"])
    new_buttons = [b for b in (menu_post or {}).get("buttons", []) if _norm(b) and _norm(b) not in known]

    changed = sum(len(r.diff.changed) + len(r.diff.added) + len(r.diff.removed) for r in results)
    if changed or new_buttons or report_always:
        text = format_changes(db, results)
        if new_buttons:
            text += ("\n\n🆕 В меню поставщика новые кнопки: " + html.escape(", ".join(new_buttons))
                     + ". Чтобы я их подхватил, один раз отправьте боту свежий экспорт канала.")
        await notify(bot, cfg, text[:4000])
    log.info("Сбор завершён: %s категорий, изменений %s", len(results), changed)
    return True


def format_changes(db: DB, results, limit: int = 25) -> str:
    """Короткий отчёт для владельца: только изменившиеся цены, с ценой продажи."""
    from .pricing import apply_markup, fmt_price

    total = sum(len(r.diff.changed) + len(r.diff.added) + len(r.diff.removed) for r in results)
    lines = [f"🤖 <b>Сборщик проверил прайс</b>: {sum(r.diff.total for r in results)} позиций, "
             + (f"изменений: {total}" if total else "изменений нет")]
    shown = 0
    for r in results:
        d = r.diff
        if not (d.changed or d.added or d.removed):
            continue
        pct, fixed, step = db.markup(db.category(r.cat_id))
        lines += ["", f"<b>{html.escape(r.title)}</b>"]
        for name, old, new in d.changed:
            if shown < limit:
                arrow = "🔺" if new > old else "🔻"
                lines.append(f"{arrow} {html.escape(name)}: {fmt_price(old)} → {fmt_price(new)} "
                             f"(у вас {fmt_price(apply_markup(new, pct, fixed, step))})")
            shown += 1
        if d.added:
            lines.append(f"➕ новых: {len(d.added)}")
        if d.removed:
            lines.append(f"➖ пропало: {len(d.removed)}")
    if shown > limit:
        lines.append(f"\n…и ещё {shown - limit} изменений цен. Весь прайс — /excel")
    return "\n".join(lines)


async def run(command: str, cfg: Config, db: DB, report_always: bool = False) -> int:
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        if command == "login":
            ok = await login(cfg, bot)
        else:
            ok = await collect(cfg, db, bot, report_always)
        return 0 if ok else 1
    finally:
        await bot.session.close()


def main(command: str, cfg: Config, db: DB, report_always: bool = False) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return asyncio.run(run(command, cfg, db, report_always))
