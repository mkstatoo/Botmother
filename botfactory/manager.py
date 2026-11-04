"""Runtime manager for the generated (child) bots."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import Update
from sqlalchemy import select, update

from .db.models import ChildBot
from .polling import poll_forever

if TYPE_CHECKING:  # pragma: no cover
    from .app import App

log = logging.getLogger(__name__)


@dataclass
class Runtime:
    bot: Bot
    secret: str
    task: asyncio.Task | None = None


class BotManager:
    def __init__(self, app: App) -> None:
        self.app = app
        self.runtimes: dict[int, Runtime] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ queries
    def get_bot(self, bot_id: int) -> Bot | None:
        rt = self.runtimes.get(bot_id)
        return rt.bot if rt else None

    def is_running(self, bot_id: int) -> bool:
        rt = self.runtimes.get(bot_id)
        return bool(rt and rt.task and not rt.task.done())

    # ------------------------------------------------------------------ lifecycle
    async def start_all(self) -> int:
        async with self.app.db.session() as s:
            rows = list((await s.scalars(select(ChildBot).where(ChildBot.status != "invalid"))).all())
        for child in rows:
            await self.start(child)
        return len(rows)

    async def start(self, child: ChildBot) -> None:
        async with self._lock:
            if child.id in self.runtimes:
                return
            token = self.app.box.decrypt(child.token_enc)
            bot = self.app.make_bot(token)
            rt = Runtime(bot=bot, secret=child.webhook_secret)
            rt.task = asyncio.create_task(self._run(child.id, rt), name=f"child-{child.id}")
            self.runtimes[child.id] = rt

    async def stop(self, bot_id: int, *, delete_webhook: bool = False) -> None:
        async with self._lock:
            rt = self.runtimes.pop(bot_id, None)
        if rt is None:
            return
        if rt.task and rt.task is not asyncio.current_task():
            rt.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await rt.task
        if delete_webhook:
            with contextlib.suppress(Exception):
                await rt.bot.delete_webhook()
        with contextlib.suppress(Exception):
            await rt.bot.session.close()

    async def restart(self, child: ChildBot) -> None:
        await self.stop(child.id)
        await self.start(child)

    async def stop_all(self) -> None:
        for bot_id in list(self.runtimes):
            await self.stop(bot_id)

    # ------------------------------------------------------------------ internals
    async def _run(self, bot_id: int, rt: Runtime) -> None:
        try:
            if self.app.settings.mode == "webhook":
                await self._ensure_webhook(bot_id, rt)
                await asyncio.Event().wait()  # idle until cancelled
            else:
                await poll_forever(rt.bot, self.app.child_dp, on_unauthorized=lambda: self._mark_invalid(bot_id))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("runtime of bot %s crashed", bot_id)

    async def _ensure_webhook(self, bot_id: int, rt: Runtime) -> None:
        url = f"{self.app.settings.base_url}/tg/c/{bot_id}/{rt.secret}"
        backoff = 1.0
        while True:
            try:
                await rt.bot.set_webhook(
                    url,
                    secret_token=rt.secret,
                    allowed_updates=self.app.child_dp.resolve_used_update_types(),
                    drop_pending_updates=False,
                )
                return
            except TelegramUnauthorizedError:
                await self._mark_invalid(bot_id)
                return
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 0.5)
            except (TimeoutError, TelegramAPIError, aiohttp.ClientError) as exc:
                log.warning("set_webhook for bot %s failed: %s", bot_id, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _mark_invalid(self, bot_id: int) -> None:
        async with self.app.db.session() as s:
            child = await s.get(ChildBot, bot_id)
            if child is None:
                return
            await s.execute(update(ChildBot).where(ChildBot.id == bot_id).values(status="invalid"))
            await s.commit()
        await self.stop(bot_id)
        await self.app.notify_creator(
            child.owner_id,
            f"⚠️ توکن ربات @{child.username} دیگر معتبر نیست و ربات متوقف شد.\n"
            "اگر توکن را در BotFather باطل یا عوض کرده‌اید، از «ربات‌های من» توکن جدید را وارد کنید.",
        )

    # ------------------------------------------------------------------ webhook entry
    async def feed_webhook(self, bot_id: int, path_secret: str, header_secret: str | None, data: dict[str, Any]) -> int:
        rt = self.runtimes.get(bot_id)
        if rt is None:
            return 404
        if not hmac.compare_digest(path_secret, rt.secret) or not hmac.compare_digest(header_secret or "", rt.secret):
            return 403
        try:
            upd = Update.model_validate(data, context={"bot": rt.bot})
        except ValueError:
            return 400
        self.app.spawn(self._feed(rt.bot, upd))
        return 200

    async def _feed(self, bot: Bot, upd: Update) -> None:
        try:
            await self.app.child_dp.feed_update(bot, upd)
        except Exception:
            log.exception("error while processing webhook update %s", upd.update_id)
