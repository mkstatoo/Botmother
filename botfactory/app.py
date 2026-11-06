"""Application container: wires settings, DB, bots, payments and the web server."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from collections.abc import Callable, Coroutine
from typing import Any

import aiohttp
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, Update
from aiohttp import web

from .child import build_child_dispatcher
from .config import Settings
from .crypto import SecretBox, load_or_create_key
from .db import ChildBot, Database
from .manager import BotManager
from .mother import build_mother_dispatcher
from .polling import poll_forever
from .services.billing import Billing
from .services.cleanup import cleanup_loop
from .services.payments import PaymentService

log = logging.getLogger(__name__)


class App:
    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: Callable[[], BaseSession] | None = None,
        storage: BaseStorage | None = None,
    ) -> None:
        self.settings = settings
        self.db = Database(settings.database_url)
        self.box = SecretBox(load_or_create_key(settings.fernet_key, settings.data_dir))
        self.session_factory = session_factory or self._default_session
        self.storage = storage or self._make_storage()
        self.http: aiohttp.ClientSession | None = None
        self.mother_bot: Bot | None = None
        self.mother_username: str | None = None
        self.mother_secret = self.box.derive("mother-webhook")
        self.background: set[asyncio.Task] = set()
        self._tasks: list[asyncio.Task] = []
        self._runner: web.AppRunner | None = None

        self.manager = BotManager(self)
        self.payments = PaymentService(self)
        self.billing = Billing(self)
        self.mother_dp = build_mother_dispatcher(self)
        self.child_dp = build_child_dispatcher(self)

    # ------------------------------------------------------------------ factories
    def _default_session(self) -> BaseSession:
        proxy = self.settings.proxy_url
        return AiohttpSession(proxy=proxy) if proxy else AiohttpSession()

    def _make_storage(self) -> BaseStorage:
        if self.settings.redis_url:
            from aiogram.fsm.storage.redis import RedisStorage

            return RedisStorage.from_url(self.settings.redis_url)
        return MemoryStorage()

    def make_bot(self, token: str) -> Bot:
        return Bot(token, session=self.session_factory(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    # ------------------------------------------------------------------ helpers
    def spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    async def notify_creator(self, user_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> bool:
        """Message a creator through the mother bot (best effort)."""
        if self.mother_bot is None:
            return False
        try:
            await self.mother_bot.send_message(user_id, text, reply_markup=reply_markup)
            return True
        except TelegramAPIError as exc:
            log.warning("cannot notify creator %s: %s", user_id, exc)
            return False

    async def notify_owner(self, child: ChildBot, text: str) -> None:
        """Message the owner through the child bot, falling back to the mother bot."""
        bot = self.manager.get_bot(child.id)
        if bot is not None:
            try:
                await bot.send_message(child.owner_id, text)
                return
            except TelegramAPIError:
                pass
        await self.notify_creator(child.owner_id, f"[@{child.username}] {text}")

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        from .web import create_web_app

        s = self.settings
        added = await self.db.create_all()
        if added:
            log.info("database upgraded, columns added: %s", ", ".join(added))
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.mother_bot = self.make_bot(s.bot_token)

        backoff = 1.0
        for attempt in range(1, 7):
            try:
                me = await self.mother_bot.get_me()
                self.mother_username = me.username
                log.info("mother bot: @%s", me.username)
                break
            except TelegramAPIError as exc:
                if attempt == 6:
                    raise
                log.warning("cannot reach Telegram (%s); retry in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff *= 2

        if s.mode == "webhook":
            if not s.base_url.startswith("https://"):
                raise RuntimeError("MODE=webhook requires BASE_URL to be a public https:// address")
            await self.mother_bot.set_webhook(
                f"{s.base_url}/tg/m/{self.mother_secret}",
                secret_token=self.mother_secret,
                allowed_updates=self.mother_dp.resolve_used_update_types(),
            )
        else:
            self._tasks.append(asyncio.create_task(poll_forever(self.mother_bot, self.mother_dp), name="mother-poll"))

        migrated = await self.billing.backfill_legacy()
        if migrated:
            log.info("started a trial period for %d existing bot(s)", migrated)
        started = await self.manager.start_all()
        log.info("started %d child bot(s)", started)
        self._tasks.append(asyncio.create_task(cleanup_loop(self), name="cleanup"))
        self._tasks.append(asyncio.create_task(self.billing.loop(), name="billing"))

        if s.web_enabled:
            self._runner = web.AppRunner(create_web_app(self))
            await self._runner.setup()
            await web.TCPSite(self._runner, s.web_host, s.web_port).start()
            log.info("web server on %s:%s", s.web_host, s.web_port)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        pending = list(self.background)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)  # let in-flight DB work unwind
        await self.manager.stop_all()
        if self.mother_bot is not None:
            with contextlib.suppress(Exception):
                await self.mother_bot.session.close()
        if self.http is not None:
            await self.http.close()
            self.http = None
        await self.db.dispose()

    # ------------------------------------------------------------------ webhook entry
    async def feed_mother_webhook(self, path_secret: str, header_secret: str | None, data: dict[str, Any]) -> int:
        if self.mother_bot is None:
            return 503
        if not hmac.compare_digest(path_secret, self.mother_secret) or not hmac.compare_digest(
            header_secret or "", self.mother_secret
        ):
            return 403
        try:
            update = Update.model_validate(data, context={"bot": self.mother_bot})
        except ValueError:
            return 400
        self.spawn(self._feed_mother(update))
        return 200

    async def _feed_mother(self, update: Update) -> None:
        try:
            assert self.mother_bot is not None
            await self.mother_dp.feed_update(self.mother_bot, update)
        except Exception:
            log.exception("error while processing mother update %s", update.update_id)
