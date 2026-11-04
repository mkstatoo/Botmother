from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import pytest
from aiogram.types import Update
from aiohttp.test_utils import TestClient, TestServer

from botfactory.app import App
from botfactory.config import Settings
from botfactory.db import ChildBot, Creator
from botfactory.web import create_web_app

from .fake_gateway import FakeGateway
from .fake_tg import FakeTelegram, cb_update, msg_update

MOTHER_ID = 10000
MOTHER_TOKEN = f"{MOTHER_ID}:" + "M" * 35
ADMIN_ID = 999


def token_for(bot_id: int) -> str:
    return f"{bot_id}:" + "B" * 35


@dataclass
class Env:
    app: App
    fake: FakeTelegram
    gateway: FakeGateway
    web: TestClient
    tmp: Path

    # ---- feeding updates -------------------------------------------------
    async def feed(self, bot_id: int, payload: dict) -> None:
        if bot_id == MOTHER_ID:
            bot, dp = self.app.mother_bot, self.app.mother_dp
        else:
            bot, dp = self.app.manager.get_bot(bot_id), self.app.child_dp
        assert bot is not None, f"bot {bot_id} is not running"
        update = Update.model_validate(payload, context={"bot": bot})
        await dp.feed_update(bot, update)

    async def say(self, bot_id: int, uid: int, text: str, **extra) -> None:
        await self.feed(bot_id, msg_update(uid, text, **extra))

    async def tap(self, bot_id: int, uid: int, data: str) -> None:
        await self.feed(bot_id, cb_update(uid, data, bot_id))

    # ---- data helpers ------------------------------------------------------
    async def make_child(self, bot_id: int = 20001, owner: int = 500, **fields) -> ChildBot:
        async with self.app.db.session() as s:
            if await s.get(Creator, owner) is None:
                s.add(Creator(id=owner, username=f"o{owner}", full_name="Owner"))
            child = ChildBot(
                id=bot_id,
                owner_id=owner,
                username=f"bot{bot_id}_bot",
                name=f"Bot {bot_id}",
                token_enc=self.app.box.encrypt(token_for(bot_id)),
                webhook_secret=f"secret-{bot_id}",
                **fields,
            )
            s.add(child)
            await s.commit()
        await self.app.manager.start(child)
        return child

    async def drain(self) -> None:
        """Wait for fire-and-forget tasks (notifications, deliveries)."""
        for _ in range(50):
            pending = [t for t in self.app.background if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)


@pytest.fixture
async def env(tmp_path):
    gateway = FakeGateway()
    await gateway.start()
    fake = FakeTelegram()
    settings = Settings(
        _env_file=None,
        bot_token=MOTHER_TOKEN,
        admin_ids=[ADMIN_ID],
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        data_dir=tmp_path,
        base_url="https://bf.example",
        zarinpal_base=gateway.zarinpal_base,
        nowpayments_base=gateway.nowpayments_base,
        broadcast_rate=1000,
    )
    app = App(settings, session_factory=fake.session)
    await app.db.create_all()
    app.http = aiohttp.ClientSession()
    app.mother_bot = app.make_bot(settings.bot_token)
    client = TestClient(TestServer(create_web_app(app)))
    await client.start_server()
    e = Env(app, fake, gateway, client, tmp_path)
    yield e
    await client.close()
    await e.drain()
    pending = list(app.background)
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    await app.manager.stop_all()
    await app.http.close()
    await app.db.dispose()
    await gateway.stop()
