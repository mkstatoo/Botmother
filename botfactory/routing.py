"""Router attachment helper.

Handlers are declared on module-level routers, and aiogram lets a router belong to only
one parent. A process normally builds a single :class:`App`, but tests (and tooling) create
several one after another, so we release the previous parent before attaching.
"""

from __future__ import annotations

from aiogram import Dispatcher, Router


def attach(dp: Dispatcher, *routers: Router) -> None:
    for router in routers:
        router._parent_router = None
        dp.include_router(router)
