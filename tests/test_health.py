"""Latency metrics + the admin health screen."""

from __future__ import annotations

import logging

from botfactory.mother.health_ui import verdict
from botfactory.services.metrics import Metrics

from .conftest import ADMIN_ID, MOTHER_ID
from .test_flows import BOT, USER, pack


def test_summary_percentiles_and_window():
    m = Metrics()
    for i in range(1, 101):
        m.api("SendMessage", i / 100, True)
    m.api("SendMessage", 9.0, False)  # failures are counted, not averaged
    m.api("GetUpdates", 30.0, False)  # failed long polls are tracked separately
    s = m.api_summary()
    assert s.n == 100 and s.errors == 1
    assert abs(s.p50 - 0.51) < 0.02 and abs(s.p95 - 0.96) < 0.02 and s.worst == 1.0
    assert m.poll_errors() == 1
    assert m.api_summary(window=0).n == 0  # outside the window


def test_verdict_names_the_cause():
    assert "مسیر شبکه" in verdict(2.0, 50, 0, 0.1, 5000)[0]
    assert verdict(0.2, 50, 0, 0.1, 5000)[0].startswith("🟢")
    assert any("ناپایدار" in v for v in verdict(0.2, 50, 8, 0.1, 5000))
    assert any("پردازنده" in v for v in verdict(0.2, 50, 0, 2.0, 5000))
    assert any("حافظه" in v for v in verdict(0.2, 50, 0, 0.1, 100))
    assert verdict(0.2, 1, 0, None, None)[0].startswith("ℹ️")  # not enough data yet
    # fast on average but 30% of calls fail → must not look healthy
    assert verdict(0.2, 14, 0, 0.1, 5000, api_failed=6)[0].startswith("🔴")


async def test_real_traffic_is_measured_and_shown_to_admins_only(env, caplog):
    await env.make_child(BOT, owner=500)
    for _ in range(3):
        await env.say(BOT, USER, "/start")
    assert env.app.metrics.api_summary().n >= 3 and env.app.metrics.update_summary().n >= 3

    await env.tap(MOTHER_ID, 12345, pack("admhl"))  # a stranger gets nothing
    assert not env.fake.find("EditMessageText", MOTHER_ID)

    await env.tap(MOTHER_ID, ADMIN_ID, pack("admhl"))
    text = env.fake.last("EditMessageText", MOTHER_ID).data["text"]
    assert "سلامت سرویس" in text and "میانگین" in text
    await env.tap(MOTHER_ID, ADMIN_ID, pack("admhlt"))
    assert "تست زندهٔ همین لحظه" in env.fake.last("EditMessageText", MOTHER_ID).data["text"]
    assert env.fake.find("GetMe", MOTHER_ID)


async def test_slow_updates_are_logged(env, caplog, monkeypatch):
    from botfactory.services import metrics as mod

    monkeypatch.setattr(mod, "SLOW_UPDATE", -1.0)  # everything counts as slow
    await env.make_child(BOT, owner=500)
    with caplog.at_level(logging.WARNING):
        await env.say(BOT, USER, "/start")
    assert any("slow update" in r.message for r in caplog.records)


# ------------------------------------------------------------------------------ network resilience
async def test_dead_connection_is_retried_instead_of_failing_the_click(env, monkeypatch):
    from aiogram.exceptions import TelegramNetworkError

    from botfactory.services import metrics as mod

    monkeypatch.setattr(mod, "RETRY_PAUSE", 0.0)
    await env.make_child(BOT, owner=500)
    state = {"n": 0}

    def flaky(call):
        state["n"] += 1
        if state["n"] <= 2:  # two stale sockets in a row
            return TelegramNetworkError(method=None, message="HTTP Client says - ServerDisconnectedError: Server disconnected")  # type: ignore[arg-type]

    env.fake.fail["SendMessage"] = flaky
    env.fake.clear()
    await env.say(BOT, USER, "/start")
    assert state["n"] == 3  # 2 failures + 1 success
    assert env.app.metrics.api_summary().errors >= 2  # every attempt is visible in the health screen


async def test_send_that_timed_out_is_not_repeated_but_an_edit_is(env, monkeypatch):
    from aiogram.exceptions import TelegramNetworkError

    from botfactory.services import metrics as mod

    monkeypatch.setattr(mod, "RETRY_PAUSE", 0.0)
    await env.make_child(BOT, owner=500)
    calls = {"send": 0, "edit": 0}

    def send(call):
        calls["send"] += 1
        return TelegramNetworkError(method=None, message="HTTP Client says - Request timeout error")  # type: ignore[arg-type]

    def edit(call):
        calls["edit"] += 1
        if calls["edit"] == 1:
            return TelegramNetworkError(method=None, message="HTTP Client says - Request timeout error")  # type: ignore[arg-type]

    env.fake.fail["SendMessage"] = send
    try:
        await env.say(BOT, USER, "/start")  # the welcome message may have been delivered → never repeated
    except TelegramNetworkError:
        pass  # (the error notice fails the same way; nothing is retried either)
    assert calls["send"] == 2  # welcome + the error notice, each exactly once
    env.fake.fail.pop("SendMessage")
    env.fake.fail["EditMessageText"] = edit
    await env.tap(BOT, USER, pack("list", 0, "c"))
    assert calls["edit"] == 2


async def test_sessions_use_short_timeout_and_keepalive(env):
    from aiogram.client.session.aiohttp import AiohttpSession

    env.app.session_factory = lambda: AiohttpSession()
    bot = env.app.make_bot(f"{BOT}:" + "B" * 35)
    assert bot.session.timeout == 20
    assert bot.session._connector_init.get("keepalive_timeout") == 10
