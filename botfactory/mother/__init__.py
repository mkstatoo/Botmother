from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Dispatcher

from ..routing import attach
from .billing_ui import admin_router, creator_router
from .router import CreatorMiddleware, router
from .wizard import router as wizard_router

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App


def build_mother_dispatcher(app: App) -> Dispatcher:
    dp = Dispatcher(storage=app.storage, name="mother")
    dp.update.outer_middleware(CreatorMiddleware(app))
    attach(dp, wizard_router, creator_router, admin_router, router)
    return dp
