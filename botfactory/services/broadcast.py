"""Broadcast a message to all users of a child bot (rate limited)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from sqlalchemy import select, update

from ..db.models import BotUser

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger(__name__)


@dataclass
class BroadcastStats:
    total: int = 0
    sent: int = 0
    blocked: int = 0
    failed: int = 0


async def run_broadcast(
    app: App, bot_id: int, from_chat_id: int, message_id: int, *, rate: int | None = None
) -> BroadcastStats:
    bot = app.manager.get_bot(bot_id)
    stats = BroadcastStats()
    if bot is None:
        return stats
    rate = max(1, rate or app.settings.broadcast_rate)
    delay = 1 / rate

    async with app.db.session() as s:
        user_ids = list(
            (
                await s.scalars(
                    select(BotUser.user_id).where(
                        BotUser.bot_id == bot_id, BotUser.is_blocked.is_(False), BotUser.is_banned.is_(False)
                    )
                )
            ).all()
        )
    stats.total = len(user_ids)
    blocked: list[int] = []

    for uid in user_ids:
        for attempt in (1, 2):
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=from_chat_id, message_id=message_id)
                stats.sent += 1
            except TelegramRetryAfter as exc:
                if attempt == 1:
                    await asyncio.sleep(exc.retry_after + 0.5)
                    continue
                stats.failed += 1
            except TelegramForbiddenError:
                stats.blocked += 1
                blocked.append(uid)
            except TelegramAPIError as exc:
                log.warning("broadcast to %s failed: %s", uid, exc)
                stats.failed += 1
            break
        await asyncio.sleep(delay)

    if blocked:
        async with app.db.session() as s:
            await s.execute(
                update(BotUser).where(BotUser.bot_id == bot_id, BotUser.user_id.in_(blocked)).values(is_blocked=True)
            )
            await s.commit()
    return stats
