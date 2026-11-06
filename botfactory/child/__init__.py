from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import Dispatcher
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import ErrorEvent

from ..routing import attach
from . import admin_content, admin_manage, shop_admin, shop_user, user
from .middleware import ChildContext

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger(__name__)


def build_child_dispatcher(app: App) -> Dispatcher:
    """One dispatcher serves every generated bot (the tenant is resolved per update)."""
    dp = Dispatcher(storage=app.storage, name="children")
    dp.update.outer_middleware(ChildContext(app))
    # order matters: /start first, then owner routers (media uploads), end-user router last
    attach(dp, user.start_router, shop_admin.router, admin_content.router, admin_manage.router, shop_user.router, user.router)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        if isinstance(event.exception, TelegramForbiddenError):
            return True  # user blocked the bot: nothing to do
        log.error("handler error: %s", event.exception, exc_info=event.exception)
        update = event.update
        try:
            if update.callback_query is not None:
                await update.callback_query.answer("خطایی رخ داد. دوباره تلاش کنید.", show_alert=True)
            elif update.message is not None:
                await update.message.answer("⚠️ خطایی رخ داد. لطفاً دوباره تلاش کنید.")
        except Exception:  # noqa: BLE001
            pass
        return True

    return dp
