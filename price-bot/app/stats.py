"""Статистика посещений бота: кто заходил, когда и что смотрел."""
import html
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware

from .config import Config
from .db import DB
from .pricing import fmt_price

log = logging.getLogger(__name__)


def today(cfg: Config) -> datetime:
    return datetime.now(ZoneInfo(cfg.timezone))


class VisitMiddleware(BaseMiddleware):
    """Отмечает каждого, кто пишет боту или жмёт кнопки. Владелец в статистику не попадает."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        cfg: Config = data["cfg"]
        if user is not None and not user.is_bot and user.id not in cfg.admin_ids:
            try:
                data["db"].track_visit(user.id, user.username, user.full_name, today(cfg).date().isoformat())
            except Exception:  # статистика не должна мешать работе бота
                log.exception("Не удалось записать посещение")
        return await handler(event, data)


def is_customer(user_id: int, cfg: Config) -> bool:
    return user_id not in cfg.admin_ids


def format_stats(db: DB, cfg: Config) -> str:
    now = today(cfg)
    periods = [("Сегодня", 1), ("7 дней", 7), ("30 дней", 30)]
    lines = [f"📊 <b>Статистика бота</b> на {now:%d.%m %H:%M}", "",
             f"👥 Всего заходили: <b>{db.total_visitors()}</b>", ""]
    lines.append("<b>Посетители</b> (новые · активные · действий)")
    for label, days in periods:
        since = (now.date() - timedelta(days=days - 1)).isoformat()
        new, active, actions = db.visitor_stats(since)
        lines.append(f"{label}: 🆕 {new} · 👤 {active} · 👆 {actions}")

    lines += ["", "<b>Заказы</b> (оформлено · подтверждено · сумма подтверждённых)"]
    for label, days in periods:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
        total, ok, revenue = db.order_stats(int(start.timestamp()))
        lines.append(f"{label}: {total} · {ok} · {fmt_price(revenue)}")

    top = db.top_categories((now.date() - timedelta(days=6)).isoformat())
    if top:
        lines += ["", "<b>Что смотрят</b> (7 дней)"]
        lines += [f"{i}. {html.escape(title)} — {n}" for i, (title, n) in enumerate(top, start=1)]

    recent = db.recent_visitors(5)
    if recent:
        lines += ["", "<b>Последние посетители</b>"]
        for v in recent:
            who = f"@{v['username']}" if v["username"] else html.escape(v["full_name"] or str(v["user_id"]))
            seen = datetime.fromtimestamp(v["last_seen"], ZoneInfo(cfg.timezone))
            lines.append(f'• <a href="tg://user?id={v["user_id"]}">{who}</a> — {seen:%d.%m %H:%M}')
    lines += ["", "<i>Ваши собственные заходы не считаются.</i>"]
    return "\n".join(lines)
