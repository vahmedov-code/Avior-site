import math


def apply_markup(base: int, pct: float, fixed: int, step: int) -> int:
    """Цена закупки + процент + фиксированная надбавка, округление вверх до step."""
    value = base * (1 + pct / 100) + fixed
    if step > 1:
        return int(math.ceil(value / step - 1e-9) * step)
    return int(round(value))


def fmt_price(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₽"
