"""Loads the child bot + end-user context for every incoming update."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Update

from ..db.models import BotUser, ChildBot
from ..util import utcnow

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger(__name__)


class ChildContext(BaseMiddleware):
    def __init__(self, app: App) -> None:
        self.app = app

    async def __call__(self, handler, event: Update, data: dict[str, Any]):
        user = data.get("event_from_user")
        bot: Bot = data["bot"]
        if user is None or user.is_bot:
            return None

        now = utcnow()
        async with self.app.db.session() as s:
            child = await s.get(ChildBot, bot.id)
            if child is None:
                return None
            bu = await s.get(BotUser, (bot.id, user.id))

            # the user blocked / unblocked the bot
            mcm = event.my_chat_member
            if mcm is not None:
                if bu is not None and mcm.chat.type == ChatType.PRIVATE:
                    bu.is_blocked = mcm.new_chat_member.status in ("kicked", "left")
                    await s.commit()
                return None

            if bu is None:
                bu = BotUser(bot_id=bot.id, user_id=user.id, username=user.username, full_name=user.full_name)
                s.add(bu)
                await s.commit()
            elif (
                bu.is_blocked
                or bu.username != user.username
                or bu.full_name != user.full_name
                or now - bu.last_seen > timedelta(minutes=5)
            ):
                bu.username, bu.full_name, bu.last_seen, bu.is_blocked = user.username, user.full_name, now, False
                await s.commit()

        is_owner = user.id == child.owner_id
        if bu.is_banned and not is_owner:
            return None
        if child.status != "active" and not is_owner:
            await self._unavailable(event, bot, user.id)
            return None

        data.update(app=self.app, child=child, bot_user=bu, is_owner=is_owner)
        return await handler(event, data)

    @staticmethod
    async def _unavailable(event: Update, bot: Bot, user_id: int) -> None:
        text = "⏸ این ربات موقتاً در دسترس نیست. لطفاً بعداً دوباره تلاش کنید."
        try:
            if event.callback_query is not None:
                await event.callback_query.answer(text, show_alert=True)
            elif event.message is not None:
                await bot.send_message(user_id, text)
        except TelegramAPIError:
            pass
