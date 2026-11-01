"""Клиент Bot API мессенджера MAX (по официальной схеме github.com/max-messenger/api-schema).

Клиенты MAX хранятся в общей базе с отрицательными id (-user_id в MAX), чтобы не пересекаться
с id Telegram: корзина, заказы, статистика — общие для обоих мессенджеров."""
from __future__ import annotations

import json
import logging
import re
import ssl

import aiohttp
from aiogram.types import InlineKeyboardMarkup

log = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=40)
UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120)


class MaxError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"MAX {status} {code}: {message}")
        self.status, self.code = status, code

    @property
    def blocked(self) -> bool:
        """Клиент остановил бота или удалил чат — писать ему бесполезно."""
        return self.status in (403, 404)


def to_internal(max_user_id: int) -> int:
    return -int(max_user_id)


def to_max(user_id: int) -> int:
    return -int(user_id)


def is_max_user(user_id: int) -> bool:
    return user_id < 0


def buttons_from_markup(markup: InlineKeyboardMarkup | None) -> list[list[dict]]:
    """Клавиатура aiogram → кнопки MAX: экраны строятся одними и теми же функциями для обоих мессенджеров."""
    rows = []
    for row in (markup.inline_keyboard if markup else []):
        out = []
        for b in row:
            if b.url:
                out.append({"type": "link", "text": b.text[:128], "url": b.url})
            elif b.callback_data:
                out.append({"type": "callback", "text": b.text[:128], "payload": b.callback_data})
        if out:
            rows.append(out)
    return rows


def keyboard(rows: list[list[dict]]) -> list[dict]:
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}] if rows else []


def contact_button(text: str = "📱 Отправить мой номер") -> dict:
    return {"type": "request_contact", "text": text}


PHONE_RE = re.compile(r"^TEL[^:]*:(.+)$", re.M)


def phone_from_contact(payload: dict) -> str | None:
    """Номер из вложения «контакт» (vCard): TEL;TYPE=cell:79001234567 → +79001234567."""
    m = PHONE_RE.search(payload.get("vcf_info") or "")
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return f"+{digits}" if digits else None


class MaxApi:
    def __init__(self, token: str, base_url: str, ca_file: str = ""):
        self.token, self.base = token, base_url.rstrip("/")
        # Домен API MAX может использовать сертификат НУЦ Минцифры — его можно передать файлом
        self.ssl = ssl.create_default_context(cafile=ca_file) if ca_file else None
        self._session: aiohttp.ClientSession | None = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT, headers={"Authorization": self.token})
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                      timeout: aiohttp.ClientTimeout | None = None) -> dict:
        s = await self.session()
        params = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in (params or {}).items() if v is not None}
        async with s.request(method, f"{self.base}{path}", params=params, json=body, ssl=self.ssl,
                             timeout=timeout or TIMEOUT) as r:
            text = await r.text()
            data = json.loads(text) if text.strip().startswith(("{", "[")) else {}
            if r.status != 200:
                raise MaxError(r.status, str(data.get("code", "")), str(data.get("message", text[:200])))
            return data

    # --- бот ---
    async def me(self) -> dict:
        return await self.request("GET", "/me")

    async def updates(self, marker: int | None, timeout: int = 30) -> dict:
        return await self.request("GET", "/updates", {"marker": marker, "timeout": timeout, "limit": 100,
                                                      "types": "bot_started,message_created,message_callback"},
                                  timeout=aiohttp.ClientTimeout(total=timeout + 15))

    # --- сообщения ---
    @staticmethod
    def body(text: str | None, rows: list[list[dict]] | None = None, attachments: list[dict] | None = None) -> dict:
        body = {"text": text[:4000] if text else text, "format": "html",
                "attachments": (attachments or []) + keyboard(rows or [])}
        return body

    async def send(self, user_id: int, text: str | None, rows: list[list[dict]] | None = None,
                   attachments: list[dict] | None = None) -> dict:
        """user_id — id пользователя в MAX (положительный)."""
        data = await self.request("POST", "/messages", {"user_id": user_id, "disable_link_preview": True},
                                  self.body(text, rows, attachments))
        return data.get("message", {})

    async def edit(self, message_id: str, text: str | None, rows: list[list[dict]] | None = None,
                   attachments: list[dict] | None = None) -> None:
        await self.request("PUT", "/messages", {"message_id": message_id}, self.body(text, rows, attachments))

    async def delete(self, message_id: str) -> None:
        await self.request("DELETE", "/messages", {"message_id": message_id})

    async def answer(self, callback_id: str, notification: str | None = None, message: dict | None = None) -> None:
        """Ответ на нажатие кнопки: всплывающее уведомление и/или замена сообщения с кнопками."""
        body = {}
        if notification:
            body["notification"] = notification[:200]
        if message is not None:
            body["message"] = message
        if not body:
            body["notification"] = " "  # MAX ждёт ответа на каждое нажатие
        await self.request("POST", "/answers", {"callback_id": callback_id}, body)

    # --- фото ---
    async def upload_image(self, data: bytes, filename: str = "photo.jpg") -> dict:
        """Загружает картинку и возвращает payload для вложения {"type": "image", "payload": …}."""
        endpoint = await self.request("POST", "/uploads", {"type": "image"})
        form = aiohttp.FormData()
        form.add_field("data", data, filename=filename, content_type="image/jpeg")
        # адрес загрузки — сервер файлов MAX, без токена бота
        async with aiohttp.ClientSession(timeout=UPLOAD_TIMEOUT) as s:
            async with s.post(endpoint["url"], data=form, ssl=self.ssl) as r:
                text = await r.text()
                if r.status != 200:
                    raise MaxError(r.status, "upload", text[:200])
                return json.loads(text)

    async def download(self, url: str) -> bytes:
        async with aiohttp.ClientSession(timeout=UPLOAD_TIMEOUT) as s:
            async with s.get(url, ssl=self.ssl) as r:
                if r.status != 200:
                    raise MaxError(r.status, "download", url[:100])
                return await r.read()


def image(payload: dict) -> dict:
    return {"type": "image", "payload": payload}
