"""Клиент WhatsApp Cloud API (Meta) для бота AVIOR в WhatsApp.

Форматы — по официальной документации Meta (сверено с библиотекой pywa): текст до 4096 символов,
кнопки-ответы — до 3 (надпись до 20 символов), список — до 10 строк (название до 24, описание до 72),
текст над кнопками или списком — до 1024 символов."""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import re

import aiohttp
from aiogram.types import InlineKeyboardMarkup

log = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=40)
GRAPH = "https://graph.facebook.com"

BUTTON_TITLE, ROW_TITLE, ROW_DESC, BODY, TEXT = 20, 24, 72, 1024, 4096


class WaError(Exception):
    def __init__(self, status: int, code: int | str, message: str):
        super().__init__(f"WhatsApp {status} ({code}): {message}")
        self.status, self.code = status, code

    @property
    def window_closed(self) -> bool:
        """Клиент не писал больше 24 часов — без шаблона Meta писать ему первым нельзя."""
        return str(self.code) == "131047"


def html_to_wa(text: str) -> str:
    """Наше HTML-оформление (<b>, <i>, <s>, <code>, <a>) → разметка WhatsApp (*жирный*, _курсив_…)."""
    text = re.sub(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", r"\1", text)
    text = re.sub(r'<a href="(?!tg://)([^"]+)">(.*?)</a>', r"\2 (\1)", text)
    for tags, mark in ((("b", "strong"), "*"), (("i", "em"), "_"), (("s", "del"), "~"), (("code",), "`")):
        for tag in tags:
            # разметка WhatsApp не терпит пробелов у краёв: «* текст*» не станет жирным
            text = re.sub(rf"<{tag}>(\s*)(.*?)(\s*)</{tag}>", lambda m, k=mark: f"{m.group(1)}{k}{m.group(2)}{k}{m.group(3)}"
                          if m.group(2) else m.group(0), text, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def flat_buttons(markup: InlineKeyboardMarkup | None) -> list[tuple[str, str]]:
    """Кнопки клавиатуры aiogram списком (id, надпись) — только кнопки-действия, без ссылок."""
    return [(b.callback_data, b.text) for row in (markup.inline_keyboard if markup else []) for b in row
            if b.callback_data]


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Подпись X-Hub-Signature-256: запрос действительно от Meta, а не от кого-то ещё."""
    if not app_secret:
        return True
    if not header:
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


class WaApi:
    def __init__(self, token: str, phone_id: str, version: str = "v24.0", base: str = GRAPH):
        self.token, self.phone_id = token, phone_id
        self.base = f"{base.rstrip('/')}/{version}"
        self._session: aiohttp.ClientSession | None = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT,
                                                  headers={"Authorization": f"Bearer {self.token}"})
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def request(self, method: str, path: str, json: dict | None = None, data=None) -> dict:
        s = await self.session()
        async with s.request(method, f"{self.base}{path}", json=json, data=data) as r:
            payload = await r.json(content_type=None)
            if r.status != 200:
                err = (payload or {}).get("error", {})
                raise WaError(r.status, err.get("code", ""), err.get("message", str(payload)[:200]))
            return payload

    async def _send(self, to: str, kind: str, content: dict) -> dict:
        return await self.request("POST", f"/{self.phone_id}/messages", {
            "messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": kind, kind: content})

    # --- отправка ---
    async def text(self, to: str, text: str) -> None:
        await self._send(to, "text", {"body": cut(text, TEXT), "preview_url": False})

    async def buttons(self, to: str, body: str, buttons: list[tuple[str, str]], footer: str | None = None,
                      image_id: str | None = None) -> None:
        """До трёх кнопок-ответов под текстом; image_id — фото над текстом (карточка б/у)."""
        content = {"type": "button", "body": {"text": cut(body, BODY)},
                   "action": {"buttons": [{"type": "reply", "reply": {"id": i[:256], "title": cut(t, BUTTON_TITLE)}}
                                          for i, t in buttons[:3]]}}
        if footer:
            content["footer"] = {"text": cut(footer, 60)}
        if image_id:
            content["header"] = {"type": "image", "image": {"id": image_id}}
        await self._send(to, "interactive", content)

    async def menu(self, to: str, body: str, button: str, rows: list[tuple[str, str, str]]) -> None:
        """Список до 10 пунктов: (id, название, описание) — открывается кнопкой под текстом."""
        content = {"type": "list", "body": {"text": cut(body, BODY)},
                   "action": {"button": cut(button, BUTTON_TITLE),
                              "sections": [{"title": cut("AVIOR", ROW_TITLE), "rows": [
                                  {"id": i[:200], "title": cut(t, ROW_TITLE),
                                   **({"description": cut(d, ROW_DESC)} if d else {})} for i, t, d in rows[:10]]}]}}
        await self._send(to, "interactive", content)

    async def image(self, to: str, media_id: str, caption: str | None = None) -> None:
        await self._send(to, "image", {"id": media_id, **({"caption": cut(caption, BODY)} if caption else {})})

    async def link(self, to: str, body: str, title: str, url: str) -> None:
        await self._send(to, "interactive", {"type": "cta_url", "body": {"text": cut(body, BODY)},
                                             "action": {"name": "cta_url",
                                                        "parameters": {"display_text": cut(title, BUTTON_TITLE),
                                                                       "url": url}}})

    async def mark_read(self, message_id: str) -> None:
        try:
            await self.request("POST", f"/{self.phone_id}/messages",
                               {"messaging_product": "whatsapp", "status": "read", "message_id": message_id})
        except WaError:
            pass

    async def send(self, to: str, html_text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        """Экран из Telegram-бота: текст + кнопки. До 3 кнопок — кнопки-ответы, больше — список."""
        text = html_to_wa(html_text)
        buttons = [(i, re.sub(r"^\W+", "", t).strip() or t) for i, t in flat_buttons(markup)]
        if not buttons:
            await self.text(to, text)
        elif len(text) > BODY:  # длинный текст — отдельно, кнопки — следом
            await self.text(to, text)
            await self.send(to, "Выберите действие:", markup)
        elif len(buttons) <= 3:
            await self.buttons(to, text, buttons)
        else:
            await self.menu(to, text, "Выбрать", [(i, t, "") for i, t in buttons])

    # --- медиа ---
    async def upload_image(self, data: bytes, filename: str = "photo.jpg") -> str:
        form = aiohttp.FormData()
        form.add_field("messaging_product", "whatsapp")
        form.add_field("type", "image/jpeg")
        form.add_field("file", data, filename=filename, content_type="image/jpeg")
        return (await self.request("POST", f"/{self.phone_id}/media", data=form))["id"]

    async def download(self, media_id: str) -> bytes:
        info = await self.request("GET", f"/{media_id}")
        s = await self.session()
        async with s.get(info["url"]) as r:  # ссылка Meta на файл, тоже с токеном
            if r.status != 200:
                raise WaError(r.status, "download", media_id)
            return await r.read()
