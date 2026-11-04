"""Whole-application wiring: start/stop, polling, webhook registration, revoked tokens."""

from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramUnauthorizedError
from sqlalchemy import select

from botfactory.app import App
from botfactory.callbacks import A
from botfactory.config import Settings
from botfactory.db import ChildBot, Creator

from .conftest import MOTHER_ID, MOTHER_TOKEN, token_for
from .fake_tg import FakeTelegram, msg_update


def make_settings(tmp_path, **kw) -> Settings:
    values = dict(
        _env_file=None,
        bot_token=MOTHER_TOKEN,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/life.db",
        data_dir=tmp_path,
        web_host="127.0.0.1",
        web_port=0,
    )
    values.update(kw)
    return Settings(**values)


async def seed_child(app: App, bot_id: int = 20001, owner: int = 500) -> None:
    await app.db.create_all()
    async with app.db.session() as s:
        s.add(Creator(id=owner, username="o", full_name="Owner"))
        s.add(
            ChildBot(
                id=bot_id,
                owner_id=owner,
                username=f"bot{bot_id}_bot",
                name="Child",
                token_enc=app.box.encrypt(token_for(bot_id)),
                webhook_secret=f"whsec-{bot_id}",
            )
        )
        await s.commit()


async def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


async def test_polling_mode_end_to_end(tmp_path):
    fake = FakeTelegram()
    app = App(make_settings(tmp_path, base_url="http://127.0.0.1"), session_factory=fake.session)
    await seed_child(app)
    await app.start()
    try:
        assert app.manager.is_running(20001)
        # an update delivered through getUpdates is processed by the right bot, and the offset advances
        fake.inbox[20001] = [msg_update(700, "/start")]
        await wait_for(lambda: any("خوش آمدید" in t for t in fake.texts(20001, 700)))
        upd_id = fake.inbox  # (drained)
        assert upd_id[20001] == []
        await wait_for(lambda: any(c.name == "GetUpdates" and c.bot_id == 20001 and c.data.get("offset") for c in fake.calls))
        # the mother bot polls too and answers creators
        fake.inbox[MOTHER_ID] = [msg_update(500, "/start")]
        await wait_for(lambda: any("کارخانه ربات" in t for t in fake.texts(MOTHER_ID, 500)))
        assert not fake.find("SetWebhook")
        assert fake.find("DeleteWebhook", 20001), "polling clears a leftover webhook first"
    finally:
        await asyncio.wait_for(app.stop(), 10)
    assert not app.manager.runtimes


async def test_webhook_mode_registers_secret_webhooks(tmp_path):
    fake = FakeTelegram()
    app = App(make_settings(tmp_path, mode="webhook", base_url="https://bf.example"), session_factory=fake.session)
    await seed_child(app)
    await app.start()
    try:
        await wait_for(lambda: len(fake.find("SetWebhook")) == 2)
        mother = fake.find("SetWebhook", MOTHER_ID)[0].data
        assert mother["url"] == f"https://bf.example/tg/m/{app.mother_secret}" and mother["secret_token"] == app.mother_secret
        child = fake.find("SetWebhook", 20001)[0].data
        assert child["url"] == "https://bf.example/tg/c/20001/whsec-20001" and child["secret_token"] == "whsec-20001"
        assert "message" in child["allowed_updates"] and "callback_query" in child["allowed_updates"]
        assert not fake.find("GetUpdates")
    finally:
        await asyncio.wait_for(app.stop(), 10)


async def test_webhook_mode_requires_https(tmp_path):
    fake = FakeTelegram()
    app = App(make_settings(tmp_path, mode="webhook", base_url="http://insecure.example"), session_factory=fake.session)
    with pytest.raises(RuntimeError, match="https"):
        await app.start()
    await app.stop()


async def test_revoked_token_marks_bot_invalid_and_notifies_owner(tmp_path):
    fake = FakeTelegram()
    app = App(make_settings(tmp_path), session_factory=fake.session)
    await seed_child(app)
    fake.fail["GetUpdates"] = lambda c: TelegramUnauthorizedError(method=None, message="Unauthorized") if c.bot_id == 20001 else None
    await app.start()
    try:
        await wait_for(lambda: not app.manager.runtimes)
        async with app.db.session() as s:
            assert await s.scalar(select(ChildBot.status)) == "invalid"
        assert any("معتبر نیست" in t for t in fake.texts(MOTHER_ID, 500))
    finally:
        await asyncio.wait_for(app.stop(), 10)


async def test_token_replacement_keeps_same_bot_only(tmp_path):
    fake = FakeTelegram()
    app = App(make_settings(tmp_path), session_factory=fake.session)
    await seed_child(app)
    fake.fail["GetUpdates"] = lambda c: None
    await app.start()
    try:
        from aiogram.types import Update

        async def say(text):
            upd = Update.model_validate(msg_update(500, text), context={"bot": app.mother_bot})
            await app.mother_dp.feed_update(app.mother_bot, upd)

        async def tap(data):
            from .fake_tg import cb_update

            upd = Update.model_validate(cb_update(500, data, MOTHER_ID), context={"bot": app.mother_bot})
            await app.mother_dp.feed_update(app.mother_bot, upd)

        await tap(A(act="mtok", id=20001).pack())
        await say(token_for(29999))  # a different bot's token
        assert any("مربوط به ربات دیگری" in t for t in fake.texts(MOTHER_ID, 500))
        old = (await _token_enc(app))
        await say("20001:" + "Z" * 35)  # same bot id, rotated secret
        assert await _token_enc(app) != old
        assert app.box.decrypt(await _token_enc(app)) == "20001:" + "Z" * 35
        assert app.manager.is_running(20001)
    finally:
        await asyncio.wait_for(app.stop(), 10)


async def _token_enc(app: App) -> str:
    async with app.db.session() as s:
        return await s.scalar(select(ChildBot.token_enc))
