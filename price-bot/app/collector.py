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
MESSAGE_SEL = "[data-message-id]"
MIN_COVERAGE = 0.8  # доля постов из меню, которые обязательно должны прочитаться
RESULT_FORMAT = "avior-collected-1"

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


async def open_browser(playwright, profile_dir: str, headless: bool = True):
    options = {"headless": headless, "viewport": {"width": 1280, "height": 1000}, "locale": "ru-RU"}
    if os.getenv("COLLECTOR_CHROMIUM"):
        options["executable_path"] = os.getenv("COLLECTOR_CHROMIUM")
    return await playwright.chromium.launch_persistent_context(profile_dir, **options)


async def is_logged_in(page) -> bool:
    return await page.locator(LOGGED_IN_SEL).count() > 0


async def login(profile_dir: str, timeout: int = 600) -> bool:
    """Открывает обычное окно браузера: войдите по QR-коду, сессия сохранится в profile_dir."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        context = await open_browser(pw, profile_dir, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(WEB_URL)
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


async def read_channel(page, chat_id: int, max_rounds: int = 80) -> dict[int, dict]:
    await page.goto(f"{WEB_URL}#{chat_id}")
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


def chat_id_of(supplier: dict) -> int:
    kind = supplier.get("type", "")
    return int(f"-100{supplier['id']}") if "channel" in kind or "supergroup" in kind else -supplier["id"]


def failure(error: str, screenshot: bytes | None = None) -> dict:
    return {"format": RESULT_FORMAT, "ok": False, "error": error,
            "screenshot": base64.b64encode(screenshot).decode() if screenshot else None}


async def fetch(supplier: dict, profile_dir: str) -> dict:
    """Читает канал. Никогда не бросает исключение: ошибка тоже уходит на сервер бота,
    чтобы владелец узнал о ней (со скриншотом)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        context = await open_browser(pw, profile_dir)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(WEB_URL)
            await page.wait_for_timeout(5000)
            if not await is_logged_in(page):
                return failure("сборщик не вошёл в Telegram (сессия закончилась) — на Windows-сервере "
                               "запустите collector_win.py login", await page.screenshot())
            posts = await read_channel(page, chat_id_of(supplier))
            menu_ids = {msg_id for _, msg_id in supplier["menu"]}
            found = menu_ids & posts.keys()
            if len(found) < MIN_COVERAGE * len(menu_ids):
                return failure(f"прочитано {len(found)} из {len(menu_ids)} постов меню", await page.screenshot())
        except Exception as e:  # сеть, изменения сайта и т.п.
            try:
                shot = await page.screenshot()
            except Exception:
                shot = None
            return failure(f"{type(e).__name__}: {e}"[:300], shot)
        finally:
            await context.close()

    menu_post = posts.get(supplier["menu_msg_id"]) or {}
    return {"format": RESULT_FORMAT, "ok": True,
            "posts": {str(i): p["text"] for i, p in posts.items()},
            "menu_buttons": menu_post.get("buttons", [])}
