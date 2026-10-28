"""CRM сервиса (cms.avior.moscow): заявки на ремонт из бота и статус заказа.

Те же адреса, что использует сайт: lead.php шлёт заявки в api/lead_intake.php (с секретом
из настроек CRM «Приём заявок с сайта»), форма «Статус ремонта» спрашивает api/status.php."""
from __future__ import annotations

import logging

import aiohttp

from .config import Config

log = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=10)


class CrmUnavailable(Exception):
    """CRM не ответила — сеть, сбой сервера. Клиенту: «попробуйте позже»."""


async def send_lead(cfg: Config, name: str, phone: str, text: str) -> str | None:
    """Создаёт заявку в CRM. Возвращает None при успехе или причину ошибки (для владельца)."""
    if not cfg.crm_lead_secret:
        return "не задан CRM_LEAD_SECRET в .env"
    payload = {"name": name, "phone": phone, "text": text, "channel": "telegram"}
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.post(f"{cfg.crm_api_url}/lead_intake.php", json=payload,
                              headers={"X-Lead-Secret": cfg.crm_lead_secret}) as r:
                body = (await r.text())[:200]
                if 200 <= r.status < 300:
                    return None
                return f"CRM ответила {r.status}: {body}"
    except (aiohttp.ClientError, TimeoutError) as e:
        return f"CRM недоступна ({type(e).__name__})"


async def order_status(cfg: Config, order_no: str, phone: str) -> dict | None:
    """{order_no, device, status} или None, если заказ с таким номером и телефоном не найден."""
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(f"{cfg.crm_api_url}/status.php", params={"order_no": order_no, "phone": phone}) as r:
                if r.status != 200:
                    raise CrmUnavailable(f"HTTP {r.status}")
                data = await r.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        raise CrmUnavailable(str(e)) from e
    if not data.get("found"):
        return None
    return {"order_no": str(data.get("order_no") or order_no), "device": str(data.get("device") or ""),
            "status": str(data.get("status") or "")}
