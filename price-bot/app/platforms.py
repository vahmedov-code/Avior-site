"""Откуда клиент: один id в общей базе на все мессенджеры.

Telegram — id как есть (положительные); MAX — минус id в MAX; WhatsApp — минус (10^16 + номер телефона).
Так корзина, заказы и статистика общие, а id из разных мессенджеров не пересекаются."""
WA_BASE = 10 ** 16

TELEGRAM, MAX, WHATSAPP = "telegram", "max", "whatsapp"
LABELS = {TELEGRAM: "Telegram", MAX: "MAX", WHATSAPP: "WhatsApp"}


def platform(user_id: int) -> str:
    if user_id > 0:
        return TELEGRAM
    return WHATSAPP if user_id <= -WA_BASE else MAX


def wa_uid(phone: str) -> int:
    return -(WA_BASE + int(phone))


def wa_phone(user_id: int) -> str:
    return str(-user_id - WA_BASE)
