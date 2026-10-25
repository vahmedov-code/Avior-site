"""Браузерная часть сборщика (работает на Windows-сервере): читает канал поставщика
через Telegram Web (web.telegram.org/a) от имени обычного аккаунта, без api_id.

Зависит только от playwright — ни токена бота, ни базы здесь нет. Результат —
JSON с текстами постов, который collector_win.py отправляет на сервер бота,
а там его разбирает app.collected_import.
"""
from __future__ import annotations

import base64
import os
import time

WEB_URL = os.getenv("COLLECTOR_WEB_URL", "https://web.telegram.org/a/")
LOGGED_IN_SEL = "#LeftColumn, .chat-list, #MiddleColumn"
# Экран входа Telegram Web: QR-код, номер телефона, код, облачный пароль
AUTH_SEL = "#auth-qr-form, #auth-phone-number-form, #auth-code-form, #auth-password-form, .auth-form"
MESSAGE_SEL = "[data-message-id], .Message[id^='message']"
SEARCH_SEL = "#telegram-search-input, input[placeholder='Search'], input[placeholder='Поиск']"
PAGE_TIMEOUT = 60_000  # мс; веб-версия Telegram иногда грузится долго
MIN_COVERAGE = 0.8  # доля постов из меню, которые обязательно должны прочитаться
RESULT_FORMAT = "avior-collected-1"

# Текст каждого поста. Время/просмотры (MessageMeta) прячем, чтобы не попали в текст.
EXTRACT_JS = r"""() => {
  document.querySelectorAll('.MessageMeta, .message-meta').forEach(m => m.style.display = 'none');
  // Эмодзи и флаги (🇺🇸, 🔥) веб-версия рисует картинками, а премиальные эмодзи канала (➡, 📱, 🛡) —
  // отдельными блоками, из-за которых innerText рвёт строку («➡» и «Видеокарты» на разных строках).
  // Заменяем каждый такой элемент целиком на его текст из alt/data-alt.
  document.querySelectorAll('.text-content [class*="emoji" i], .text-content img[alt], .text-content [data-alt]')
    .forEach(el => {
      if (!el.isConnected) return;  // уже заменён вместе с внешним элементом
      const src = el.matches('img[alt], [data-alt]') ? el : el.querySelector('img[alt], [data-alt]');
      const alt = src ? (src.getAttribute('alt') || src.getAttribute('data-alt')) : null;
      if (alt) el.replaceWith(document.createTextNode(alt));
    });
  return [...document.querySelectorAll("[data-message-id], .Message[id^='message']")].map(el => {
    const raw = el.getAttribute('data-message-id') || (el.id.match(/\d+/) || [''])[0];
    const textEl = el.querySelector('.text-content') || el.querySelector('.message-content');
    const buttons = [...el.querySelectorAll('.InlineButtons button')].map(b => b.innerText.trim()).filter(Boolean);
    return {id: Number(raw), text: textEl ? textEl.innerText : '', buttons};
  }).filter(p => Number.isInteger(p.id) && p.id > 0);  // «0.01» — служебные элементы, не посты
}"""
SCROLL_UP_JS = """() => {
  const list = document.querySelector('.MessageList');
  if (!list) return false;
  list.scrollTop = 0;
  return true;
}"""


async def open_browser(playwright, profile_dir: str, headless: bool = True):
    options = {"headless": headless, "viewport": {"width": 1280, "height": 1000}, "locale": "ru-RU"}
    if os.getenv("COLLECTOR_CHROMIUM"):
        options["executable_path"] = os.getenv("COLLECTOR_CHROMIUM")
    return await playwright.chromium.launch_persistent_context(profile_dir, **options)


async def is_logged_in(page) -> bool:
    return await page.locator(LOGGED_IN_SEL).count() > 0


async def wait_login_state(page, timeout: int = PAGE_TIMEOUT) -> bool | None:
    """Ждём, пока Telegram Web дорисует страницу: True — список чатов, False — экран входа,
    None — не появилось ни то ни другое (страница не догрузилась)."""
    try:
        await page.wait_for_selector(f"{LOGGED_IN_SEL}, {AUTH_SEL}", state="attached", timeout=timeout)
    except Exception:
        return None
    return await is_logged_in(page)


async def login(profile_dir: str, timeout: int = 600) -> bool:
    """Открывает обычное окно браузера: войдите по QR-коду, сессия сохранится в profile_dir."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        context = await open_browser(pw, profile_dir, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(WEB_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        print("В открывшемся окне войдите в Telegram аккаунтом сборщика:\n"
              "  на телефоне Telegram → Настройки → Устройства → «Подключить устройство» → отсканируйте QR-код.\n"
              "Если включён облачный пароль — введите его в окне.")
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if await is_logged_in(page):
                    await page.wait_for_timeout(5000)  # даём сессии сохраниться
                    print("Готово: сборщик вошёл в Telegram, окно можно закрывать.")
                    return True
                await page.wait_for_timeout(2000)
            print("Не дождался входа за 10 минут, запустите вход ещё раз.")
            return False
        finally:
            await context.close()


async def open_chat(page, chat_id: int, name: str) -> None:
    """Открывает канал: по адресу, иначе — кликом в списке чатов, иначе — через поиск."""
    await page.goto(f"{WEB_URL}#{chat_id}", wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    try:
        await page.wait_for_selector(MESSAGE_SEL, timeout=15_000)
        return
    except Exception:
        pass
    item = page.locator(".chat-list .ListItem, #LeftColumn .ListItem").filter(has_text=name).first
    if not await item.count():
        await page.locator(SEARCH_SEL).first.fill(name)
        await page.wait_for_timeout(3000)
        item = page.locator("#LeftColumn .ListItem, .search-result").filter(has_text=name).first
    await item.click(timeout=15_000)
    await page.wait_for_selector(MESSAGE_SEL, timeout=45_000)


async def read_channel(page, chat_id: int, name: str, max_rounds: int = 80) -> dict[int, dict]:
    await open_chat(page, chat_id, name)
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


def chat_id_of(supplier: dict) -> int:
    kind = supplier.get("type", "")
    return int(f"-100{supplier['id']}") if "channel" in kind or "supergroup" in kind else -supplier["id"]


def failure(error: str, screenshot: bytes | None = None, retry: bool = True) -> dict:
    """retry=False — повторять бесполезно (например, нужно заново войти в Telegram)."""
    return {"format": RESULT_FORMAT, "ok": False, "error": error, "retry": retry,
            "screenshot": base64.b64encode(screenshot).decode() if screenshot else None}


async def fetch(supplier: dict, profile_dir: str) -> dict:
    """Читает канал. Никогда не бросает исключение: ошибка тоже уходит на сервер бота,
    чтобы владелец узнал о ней (со скриншотом)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        context = await open_browser(pw, profile_dir)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(WEB_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
            state = await wait_login_state(page)
            if state is None:
                return failure("веб-версия Telegram не загрузилась за минуту", await page.screenshot())
            if state is False:
                return failure("сборщик не вошёл в Telegram (сессия закончилась) — на Windows-сервере "
                               "запустите collector_win.py login", await page.screenshot(), retry=False)
            posts = await read_channel(page, chat_id_of(supplier), supplier.get("name", ""))
            menu_ids = {msg_id for _, msg_id in supplier["menu"]}
            found = menu_ids & posts.keys()
            if len(found) < MIN_COVERAGE * len(menu_ids):
                return failure(f"прочитано {len(found)} из {len(menu_ids)} постов меню", await page.screenshot())
        except Exception as e:  # сеть, изменения сайта и т.п.
            shot = None
            try:
                shot = await page.screenshot()
                # HTML страницы — чтобы разобраться, что поменялось на сайте
                with open(os.path.join(profile_dir, "..", "debug-page.html"), "w", encoding="utf-8") as f:
                    f.write(await page.content())
            except Exception:
                pass
            return failure(f"{type(e).__name__}: {e}"[:300], shot)
        finally:
            await context.close()

    menu_post = posts.get(supplier["menu_msg_id"]) or {}
    return {"format": RESULT_FORMAT, "ok": True,
            "posts": {str(i): p["text"] for i, p in posts.items()},
            "menu_buttons": menu_post.get("buttons", [])}
