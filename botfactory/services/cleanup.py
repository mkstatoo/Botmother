"""Background maintenance: auto-delete delivered messages, expire stale payments."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError
from sqlalchemy import delete, select, update

from ..db.models import Payment, ScheduledDelete
from ..util import utcnow

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger(__name__)


async def cleanup_once(app: App) -> int:
    """Delete due messages and expire stale pending payments. Returns deleted-message count."""
    now = utcnow()
    done_ids: list[int] = []
    deleted = 0

    async with app.db.session() as s:
        rows = list(
            (await s.scalars(select(ScheduledDelete).where(ScheduledDelete.delete_at <= now).limit(500))).all()
        )

    for row in rows:
        bot = app.manager.get_bot(row.bot_id)
        if bot is None:
            continue  # bot not running right now: keep the row, retry later
        try:
            await bot.delete_message(row.chat_id, row.message_id)
            deleted += 1
        except TelegramAPIError:
            pass  # already deleted / too old / user blocked → nothing more to do
        done_ids.append(row.id)

    async with app.db.session() as s:
        if done_ids:
            await s.execute(delete(ScheduledDelete).where(ScheduledDelete.id.in_(done_ids)))
        await s.execute(
            update(Payment)
            .where(Payment.status == "pending", Payment.created_at < now - timedelta(days=2))
            .values(status="expired")
        )
        await s.commit()
    await app.shop.gc(now)  # self rate-limited (every few minutes)
    return deleted


async def cleanup_loop(app: App, interval: float = 15.0) -> None:
    while True:
        try:
            await cleanup_once(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("cleanup iteration failed")
        await asyncio.sleep(interval)
