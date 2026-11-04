"""Unit tests: utilities, crypto, signature verification, access rules."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta

import pytest

from botfactory.crypto import SecretBox, load_or_create_key
from botfactory.db import Content, Subscription
from botfactory.services.access import Decision, check_access, subscription_until
from botfactory.services.payments import verify_ipn_signature
from botfactory.services.sponsors import normalize_chat_ref
from botfactory.util import fmt_toman, fmt_usd, parse_cents, parse_int, to_en, utcnow


def test_digits_and_numbers():
    assert to_en("۱۲۳٤٥٦") == "123456"
    assert parse_int("۱۲٬۵۰۰") == 12500
    assert parse_int("12,500") == 12500
    assert parse_int("abc") is None
    assert parse_int("-5") is None
    assert parse_cents("2.5") == 250
    assert parse_cents("۲٫۵۰") == 250
    assert parse_cents("1,000") == 100000
    assert parse_cents("2.555") is None
    assert fmt_toman(1500000) == "۱٬۵۰۰٬۰۰۰ تومان"
    assert fmt_usd(250) == "۲.۵۰ دلار"
    assert fmt_usd(500) == "۵ دلار"


def test_secretbox_roundtrip_and_key_file(tmp_path):
    key = load_or_create_key(None, tmp_path)
    assert load_or_create_key(None, tmp_path) == key  # stable across runs
    box = SecretBox(key)
    enc = box.encrypt("123:SECRET")
    assert "SECRET" not in enc and box.decrypt(enc) == "123:SECRET"
    assert box.derive("x") == box.derive("x") != box.derive("y")
    with pytest.raises(ValueError):
        SecretBox(load_or_create_key("", tmp_path.parent / "other")).decrypt(enc)


def test_chat_ref_parsing():
    assert normalize_chat_ref("@my_channel") == "@my_channel"
    assert normalize_chat_ref("https://t.me/my_channel") == "@my_channel"
    assert normalize_chat_ref("t.me/my_channel/") == "@my_channel"
    assert normalize_chat_ref("-1001234567890") == -1001234567890
    assert normalize_chat_ref("https://t.me/+AbCdEf") is None
    assert normalize_chat_ref("hi") is None


def _sign(payload: dict, secret: str, **dumps) -> tuple[bytes, str]:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), **dumps)
    raw = json.dumps(payload).encode()
    return raw, hmac.new(secret.encode(), canonical.encode(), hashlib.sha512).hexdigest()


def test_ipn_signature():
    payload = {"payment_status": "finished", "order_id": "bf-1", "price_amount": 5.0, "nested": {"b": 1, "a": 2.0}}
    raw, sig = _sign(payload, "s3cret-value")
    assert verify_ipn_signature(raw, sig, "s3cret-value")
    assert not verify_ipn_signature(raw, sig, "other-secret")
    assert not verify_ipn_signature(raw, "", "s3cret-value")
    assert not verify_ipn_signature(b"not json", sig, "s3cret-value")
    # sender serialised 5.0 as 5 (JavaScript style)
    js_payload = {"payment_status": "finished", "order_id": "bf-1", "price_amount": 5, "nested": {"b": 1, "a": 2}}
    _, js_sig = _sign(js_payload, "s3cret-value")
    assert verify_ipn_signature(raw, js_sig, "s3cret-value")
    # non-ASCII payload signed with ensure_ascii=False
    uni = {"order_description": "سلام", "order_id": "bf-1"}
    raw_u, sig_u = _sign(uni, "s3cret-value", ensure_ascii=False)
    assert verify_ipn_signature(raw_u, sig_u, "s3cret-value")


async def test_access_rules(env):
    child = await env.make_child(owner=500)
    bot = env.app.manager.get_bot(child.id)
    async with env.app.db.session() as s:
        free = Content(bot_id=child.id, title="f", kind="audio", file_id="x", access="free")
        prem = Content(bot_id=child.id, title="p", kind="audio", file_id="y", access="premium")
        s.add_all([free, prem])
        await s.commit()

        assert (await check_access(s, bot, child, 1, free, is_owner=False)).decision == Decision.OK
        assert (await check_access(s, bot, child, 1, prem, is_owner=False)).decision == Decision.PAYMENT
        assert (await check_access(s, bot, child, 500, prem, is_owner=True)).decision == Decision.OK

        now = utcnow()
        s.add(Subscription(bot_id=child.id, user_id=1, starts_at=now - timedelta(days=40), expires_at=now - timedelta(days=10)))
        await s.commit()
        assert await subscription_until(s, child.id, 1) is None  # expired
        assert (await check_access(s, bot, child, 1, prem, is_owner=False)).decision == Decision.PAYMENT
        s.add(Subscription(bot_id=child.id, user_id=1, starts_at=now, expires_at=now + timedelta(days=5)))
        await s.commit()
        assert await subscription_until(s, child.id, 1) is not None
        assert (await check_access(s, bot, child, 1, prem, is_owner=False)).decision == Decision.OK


def test_settings_from_environment(monkeypatch, tmp_path):
    from botfactory.config import Settings

    monkeypatch.setenv("BOT_TOKEN", "123456:" + "A" * 35)
    monkeypatch.setenv("ADMIN_IDS", "11, 22,33")
    monkeypatch.setenv("BASE_URL", "https://example.com/")
    monkeypatch.setenv("PROXY_URL", "")
    s = Settings(_env_file=None)
    assert s.admin_ids == [11, 22, 33]
    assert s.base_url == "https://example.com" and s.payments_enabled and s.web_enabled
    assert s.proxy_url is None and s.mode == "polling"
    monkeypatch.setenv("ADMIN_IDS", "")
    monkeypatch.setenv("BASE_URL", "")
    s = Settings(_env_file=None)
    assert s.admin_ids == [] and not s.payments_enabled and not s.web_enabled
