"""Platform billing (trial → paid service) and the admin user-management panel."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from sqlalchemy import select

from botfactory.db import ChildBot, Creator, Database, ServicePayment
from botfactory.db.models import Base
from botfactory.util import utcnow

from .conftest import ADMIN_ID, MOTHER_ID, token_for
from .test_flows import BOT, MERCHANT, OWNER, USER, buttons, db_all, db_one, has_text, pack

OUTSIDER = 501


async def child(env) -> ChildBot:
    return await db_one(env, select(ChildBot).where(ChildBot.id == BOT))


async def set_up_billing(env, price=100000, **extra):
    await env.app.billing.set("price_irt", str(price))
    for k, v in extra.items():
        await env.app.billing.set(k, v)


async def set_until(env, delta: timedelta, **fields):
    async with env.app.db.session() as s:
        c = await s.get(ChildBot, BOT)
        c.service_until = utcnow() + delta
        for k, v in fields.items():
            setattr(c, k, v)
        await s.commit()


# ------------------------------------------------------------------------------ migration
async def test_old_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    # build the *old* schema: current tables minus the new columns/tables
    import sqlalchemy as sa

    eng = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    eng.dispose()
    con = sqlite3.connect(path)
    for col in ("service_until", "service_paid", "service_notice"):
        con.execute(f"ALTER TABLE child_bots DROP COLUMN {col}")
    con.execute("DROP TABLE service_payments")
    con.execute("DROP TABLE platform_settings")
    con.execute(
        "INSERT INTO child_bots (id, owner_id, username, name, token_enc, status, webhook_secret, default_storage,"
        " sponsor_gate, protect_content, auto_delete_seconds, created_at) VALUES"
        " (1, 5, 'old_bot', 'Old', 'x', 'active', 's', 'telegram', 1, 0, 0, '2024-01-01 00:00:00')"
    )
    con.commit()
    con.close()

    db = Database(f"sqlite+aiosqlite:///{path}")
    added = await db.create_all()
    assert sorted(added) == ["child_bots.service_notice", "child_bots.service_paid", "child_bots.service_until"]
    async with db.session() as s:
        row = await s.get(ChildBot, 1)
        assert row.username == "old_bot" and row.service_paid is False and row.service_notice == 0 and row.service_until is None
    assert await db.create_all() == []  # idempotent
    await db.dispose()


async def test_legacy_bots_get_one_trial(env):
    await env.make_child(BOT)
    assert (await child(env)).service_until is None  # exempt until the one-time backfill
    assert await env.app.billing.backfill_legacy() == 1
    until = (await child(env)).service_until
    assert until is not None and timedelta(days=6) < until - utcnow() <= timedelta(days=7, minutes=1)
    assert await env.app.billing.backfill_legacy() == 0  # only once


# ------------------------------------------------------------------------------ trial on creation
async def test_new_bot_gets_trial_and_mentions_it(env):
    await set_up_billing(env, trial_days="5")
    await env.tap(MOTHER_ID, OWNER, pack("new"))
    await env.say(MOTHER_ID, OWNER, token_for(BOT))
    c = await child(env)
    assert timedelta(days=4) < c.service_until - utcnow() <= timedelta(days=5, minutes=1)
    assert c.service_paid is False
    assert has_text(env, MOTHER_ID, "۵ روز اول کاملاً رایگان", OWNER)
    await env.tap(MOTHER_ID, OWNER, pack("mb", BOT))
    assert has_text(env, MOTHER_ID, "دوره آزمایشی", OWNER)


# ------------------------------------------------------------------------------ expiry & reminders
async def test_tick_is_noop_until_a_price_is_set(env):
    await env.make_child(BOT)
    await set_until(env, timedelta(days=-30))
    assert await env.app.billing.tick() == {"reminded": 0, "expired": 0}
    assert (await child(env)).status == "active"


async def test_reminders_grace_expiry_and_renewal(env):
    await env.make_child(BOT)
    await set_up_billing(env)
    billing = env.app.billing

    await set_until(env, timedelta(days=10))
    assert (await billing.tick())["reminded"] == 0

    await set_until(env, timedelta(days=2, hours=1))
    assert (await billing.tick())["reminded"] == 1
    assert (await billing.tick())["reminded"] == 0  # idempotent
    await env.drain()
    assert has_text(env, MOTHER_ID, "دیگر", OWNER)

    await set_until(env, timedelta(hours=5))
    assert (await billing.tick())["reminded"] == 1
    assert has_text(env, MOTHER_ID, "کمتر از ۲۴ ساعت", OWNER)

    await set_until(env, timedelta(days=-1))  # inside the 3-day grace
    assert (await billing.tick())["reminded"] == 1
    assert (await child(env)).status == "active"
    assert has_text(env, MOTHER_ID, "مهلت") or has_text(env, MOTHER_ID, "هنوز کار می‌کند")

    await set_until(env, timedelta(days=-5))
    stats = await billing.tick()
    assert stats == {"reminded": 1, "expired": 1}
    assert (await child(env)).status == "expired"
    assert (await billing.tick()) == {"reminded": 0, "expired": 0}

    # end users are locked out, the owner is not
    env.fake.clear()
    await env.say(BOT, USER, "/start")
    assert has_text(env, BOT, "موقتاً در دسترس نیست", USER)

    # renewal: counts from now (not from the old end), reactivates and resets reminders
    await billing.extend(BOT, 30, paid=True)
    c = await child(env)
    assert c.status == "active" and c.service_paid and c.service_notice == 0
    assert timedelta(days=29) < c.service_until - utcnow() <= timedelta(days=30, minutes=1)


async def test_extension_stacks_on_remaining_time(env):
    await env.make_child(BOT)
    await set_until(env, timedelta(days=10))
    await env.app.billing.extend(BOT, 30, paid=True)
    left = (await child(env)).service_until - utcnow()
    assert timedelta(days=39) < left <= timedelta(days=40, minutes=1)


async def test_paused_bot_cannot_dodge_expiry(env):
    await env.make_child(BOT, status="paused")
    await set_up_billing(env)
    await set_until(env, timedelta(days=-9))
    await env.app.billing.tick()
    assert (await child(env)).status == "expired"


async def test_suspended_bots_are_left_alone(env):
    await env.make_child(BOT, status="suspended")
    await set_up_billing(env)
    await set_until(env, timedelta(days=-9))
    assert await env.app.billing.tick() == {"reminded": 0, "expired": 0}
    assert (await child(env)).status == "suspended"


# ------------------------------------------------------------------------------ creator renewal
async def test_online_renewal_with_zarinpal(env):
    await env.make_child(BOT)
    await set_until(env, timedelta(days=-1))
    await set_up_billing(env, zarinpal_merchant=env.app.box.encrypt(MERCHANT))
    await env.tap(MOTHER_ID, OWNER, pack("sv", BOT))
    assert has_text(env, MOTHER_ID, "تمدید سرویس", OWNER)
    await env.tap(MOTHER_ID, OWNER, pack("svn", BOT, "3"))
    acts = [b.get("callback_data") for b in buttons(env.fake.last("EditMessageText", MOTHER_ID))]
    assert pack("svp", BOT, "z3") in acts and pack("svp", BOT, "m3") not in acts  # no card configured

    await env.tap(MOTHER_ID, OWNER, pack("svp", BOT, "z3"))
    sp = await db_one(env, select(ServicePayment))
    assert (sp.status, sp.method, sp.amount, sp.days, sp.creator_id) == ("pending", "zarinpal", 300000, 90, OWNER)
    req = env.gateway.requests[-1]
    assert req["merchant_id"] == MERCHANT and req["amount"] == 3_000_000
    assert req["callback_url"] == f"https://bf.example/pay/service/{sp.id}"

    r = await env.web.get(f"/pay/service/{sp.id}", params={"Authority": "WRONG", "Status": "OK"})
    assert r.status == 400
    r = await env.web.get(f"/pay/service/{sp.id}", params={"Authority": sp.authority, "Status": "OK"})
    assert r.status == 200
    until = (await child(env)).service_until
    assert timedelta(days=89) < until - utcnow() <= timedelta(days=90, minutes=1)
    assert (await child(env)).service_paid

    r = await env.web.get(f"/pay/service/{sp.id}", params={"Authority": sp.authority, "Status": "OK"})  # replay
    assert r.status == 200
    assert (await child(env)).service_until == until  # extended exactly once
    assert len(env.gateway.verify_calls) == 1


async def test_failed_online_payment_changes_nothing(env):
    await env.make_child(BOT)
    await set_until(env, timedelta(days=1))
    await set_up_billing(env, zarinpal_merchant=env.app.box.encrypt(MERCHANT))
    await env.tap(MOTHER_ID, OWNER, pack("svp", BOT, "z1"))
    sp = await db_one(env, select(ServicePayment))
    before = (await child(env)).service_until
    r = await env.web.get(f"/pay/service/{sp.id}", params={"Authority": sp.authority, "Status": "NOK"})
    assert r.status == 400
    assert (await db_one(env, select(ServicePayment))).status == "failed"
    assert (await child(env)).service_until == before


async def test_manual_receipt_flow_and_admin_approval(env):
    await env.make_child(BOT)
    await set_until(env, timedelta(days=1))
    await set_up_billing(env, card_number="6037 9971 2345 6789", card_holder="پلتفرم")
    await env.tap(MOTHER_ID, OWNER, pack("svp", BOT, "m1"))
    assert has_text(env, MOTHER_ID, "6037 9971 2345 6789", OWNER)
    await env.say(MOTHER_ID, OWNER, "", photo=[{"file_id": "RCPT1", "file_unique_id": "u", "width": 1, "height": 1}], caption="واریز شد")
    await env.drain()
    sp = await db_one(env, select(ServicePayment))
    assert sp.status == "review" and sp.receipt_file_id == "RCPT1"
    sent = env.fake.last("SendPhoto", MOTHER_ID)
    assert sent.data["chat_id"] == ADMIN_ID
    assert pack("svok", sp.id) in [b["callback_data"] for b in buttons(sent)]

    before = (await child(env)).service_until
    await env.tap(MOTHER_ID, OUTSIDER, pack("svok", sp.id))  # not an admin
    assert (await db_one(env, select(ServicePayment))).status == "review"
    await env.tap(MOTHER_ID, ADMIN_ID, pack("svok", sp.id))
    assert (await db_one(env, select(ServicePayment))).status == "paid"
    assert (await child(env)).service_until - before > timedelta(days=29)
    await env.tap(MOTHER_ID, ADMIN_ID, pack("svok", sp.id))  # double tap
    await env.tap(MOTHER_ID, ADMIN_ID, pack("svno", sp.id))
    assert (await db_one(env, select(ServicePayment))).status == "paid"
    assert (await child(env)).service_until - before < timedelta(days=31)


async def test_admin_can_reject_receipt(env):
    await env.make_child(BOT)
    await set_up_billing(env, card_number="6037 9971 2345 6789")
    await env.tap(MOTHER_ID, OWNER, pack("svp", BOT, "m1"))
    await env.say(MOTHER_ID, OWNER, "شماره پیگیری 123")
    sp = await db_one(env, select(ServicePayment))
    await env.tap(MOTHER_ID, ADMIN_ID, pack("svno", sp.id))
    assert (await db_one(env, select(ServicePayment))).status == "rejected"
    assert has_text(env, MOTHER_ID, "تأیید نشد", OWNER)


async def test_renewal_unavailable_without_price_or_methods(env):
    await env.make_child(BOT)
    await env.tap(MOTHER_ID, OWNER, pack("sv", BOT))
    assert has_text(env, MOTHER_ID, "نیازی به پرداخت نیست", OWNER)
    await set_up_billing(env)
    await env.tap(MOTHER_ID, OWNER, pack("sv", BOT))
    assert has_text(env, MOTHER_ID, "روش پرداختی", OWNER)
    await env.tap(MOTHER_ID, OWNER, pack("svp", BOT, "m1"))  # forged callback
    assert not await db_all(env, select(ServicePayment))


async def test_cannot_renew_someone_elses_bot(env):
    await env.make_child(BOT)
    await set_up_billing(env, card_number="6037 9971 2345 6789")
    await env.tap(MOTHER_ID, OUTSIDER, pack("svp", BOT, "m1"))
    assert not await db_all(env, select(ServicePayment))


# ------------------------------------------------------------------------------ admin panel
async def test_admin_panel_is_admin_only(env):
    await env.make_child(BOT)
    for act in ("admc", "admset", "admrv", "admpay", "admq"):
        env.fake.clear()
        await env.tap(MOTHER_ID, OUTSIDER, pack(act))
        assert not has_text(env, MOTHER_ID, "سازندگان", OUTSIDER), act
        assert not has_text(env, MOTHER_ID, "تنظیمات اشتراک", OUTSIDER), act
    await env.tap(MOTHER_ID, OUTSIDER, pack("admg", BOT, "30"))
    await env.tap(MOTHER_ID, OUTSIDER, pack("admgu", BOT))
    await env.tap(MOTHER_ID, OUTSIDER, pack("admban", OWNER))
    assert (await child(env)).service_until is None
    assert not (await db_one(env, select(Creator).where(Creator.id == OWNER))).is_banned


async def test_admin_dashboard_list_search_and_detail(env):
    await env.make_child(BOT, owner=OWNER)
    await env.make_child(20002, owner=OWNER + 1)
    await env.say(MOTHER_ID, ADMIN_ID, "/admin")
    assert has_text(env, MOTHER_ID, "اشتراک سرویس", ADMIN_ID)
    await env.tap(MOTHER_ID, ADMIN_ID, pack("admc", 0))
    labels = [b["text"] for b in buttons(env.fake.last("EditMessageText", MOTHER_ID))]
    assert any("o500" in t for t in labels) and any("o501" in t for t in labels)

    await env.tap(MOTHER_ID, ADMIN_ID, pack("admq"))
    await env.say(MOTHER_ID, ADMIN_ID, "bot20002")  # by child username
    res = [b["callback_data"] for b in buttons(env.fake.last("SendMessage", MOTHER_ID))]
    assert pack("admcd", OWNER + 1) in res and pack("admcd", OWNER) not in res
    await env.tap(MOTHER_ID, ADMIN_ID, pack("admq"))
    await env.say(MOTHER_ID, ADMIN_ID, str(OWNER))  # by id
    assert pack("admcd", OWNER) in [b["callback_data"] for b in buttons(env.fake.last("SendMessage", MOTHER_ID))]

    await env.say(MOTHER_ID, ADMIN_ID, f"/user @o{OWNER}")
    assert has_text(env, MOTHER_ID, f"bot{BOT}_bot", ADMIN_ID)
    await env.say(MOTHER_ID, OUTSIDER, f"/user {OWNER}")
    assert not has_text(env, MOTHER_ID, f"bot{BOT}_bot", OUTSIDER)


async def test_ban_suspends_bots_and_unban_restores(env):
    await env.make_child(BOT)
    await env.tap(MOTHER_ID, ADMIN_ID, pack("admban", OWNER))
    assert (await db_one(env, select(Creator).where(Creator.id == OWNER))).is_banned
    assert (await child(env)).status == "suspended"
    env.fake.clear()
    await env.say(MOTHER_ID, OWNER, "/start")  # banned creators are ignored
    assert not env.fake.texts(MOTHER_ID, OWNER)
    await env.say(BOT, USER, "/start")
    assert has_text(env, BOT, "موقتاً در دسترس نیست", USER)
    await env.tap(MOTHER_ID, ADMIN_ID, pack("admun", OWNER))
    assert not (await db_one(env, select(Creator).where(Creator.id == OWNER))).is_banned
    assert (await child(env)).status == "active"
    await env.tap(MOTHER_ID, ADMIN_ID, pack("admban", ADMIN_ID))  # admins can't be banned
    assert not (await db_one(env, select(Creator).where(Creator.id == ADMIN_ID))).is_banned


async def test_admin_gift_unlimited_and_end(env):
    await env.make_child(BOT)
    await set_until(env, timedelta(days=1))
    await env.tap(MOTHER_ID, ADMIN_ID, pack("admg", BOT, "30"))
    left = (await child(env)).service_until - utcnow()
    assert timedelta(days=30) < left < timedelta(days=32)
    sp = await db_one(env, select(ServicePayment))
    assert (sp.method, sp.amount, sp.status, sp.days) == ("admin", 0, "paid", 30)
    assert not (await child(env)).service_paid  # a gift is not a paid period

    await env.tap(MOTHER_ID, ADMIN_ID, pack("admgc", BOT))
    await env.say(MOTHER_ID, ADMIN_ID, "abc")
    await env.say(MOTHER_ID, ADMIN_ID, "۱۰")
    assert (await child(env)).service_until - utcnow() > timedelta(days=39)

    await env.tap(MOTHER_ID, ADMIN_ID, pack("admge", BOT))
    c = await child(env)
    assert c.status == "expired" and c.service_until < utcnow()
    await env.tap(MOTHER_ID, ADMIN_ID, pack("admgu", BOT))
    c = await child(env)
    assert c.status == "active" and c.service_until is None


async def test_admin_edits_settings(env):
    await env.tap(MOTHER_ID, ADMIN_ID, pack("admset"))
    assert has_text(env, MOTHER_ID, "تنظیمات اشتراک", ADMIN_ID)

    await env.tap(MOTHER_ID, ADMIN_ID, pack("admse", 0, "price_irt"))
    await env.say(MOTHER_ID, ADMIN_ID, "خیلی")
    assert (await env.app.billing.config()).price_irt == 0
    await env.say(MOTHER_ID, ADMIN_ID, "۲۵۰٬۰۰۰")
    cfg = await env.app.billing.config()
    assert cfg.price_irt == 250000 and cfg.enforced

    await env.tap(MOTHER_ID, ADMIN_ID, pack("admse", 0, "trial_days"))
    await env.say(MOTHER_ID, ADMIN_ID, "14")
    assert (await env.app.billing.config()).trial_days == 14

    await env.tap(MOTHER_ID, ADMIN_ID, pack("admse", 0, "card_number"))
    await env.say(MOTHER_ID, ADMIN_ID, "123")
    assert (await env.app.billing.config()).card_number == ""
    await env.say(MOTHER_ID, ADMIN_ID, "6037997123456789")
    assert (await env.app.billing.config()).card_number == "6037 9971 2345 6789"

    await env.tap(MOTHER_ID, ADMIN_ID, pack("admse", 0, "zarinpal_merchant"))
    await env.say(MOTHER_ID, ADMIN_ID, "bad")
    assert (await env.app.billing.config()).zarinpal_merchant == ""
    await env.say(MOTHER_ID, ADMIN_ID, MERCHANT)
    assert (await env.app.billing.config()).zarinpal_merchant == MERCHANT
    raw = await env.app.billing.get_raw("zarinpal_merchant")
    assert raw and MERCHANT not in raw  # stored encrypted
    assert env.fake.find("DeleteMessage", MOTHER_ID)  # the secret message was removed from the chat

    await env.tap(MOTHER_ID, ADMIN_ID, pack("admt"))
    assert not (await env.app.billing.config()).enabled


async def test_disabled_billing_never_locks_bots(env):
    await env.make_child(BOT)
    await set_up_billing(env, billing_enabled="0")
    await set_until(env, timedelta(days=-30))
    await env.app.billing.tick()
    assert (await child(env)).status == "active"


async def test_stats_and_receipts_list(env):
    await env.make_child(BOT)
    await set_up_billing(env, card_number="6037 9971 2345 6789")
    await env.tap(MOTHER_ID, OWNER, pack("svp", BOT, "m1"))
    await env.say(MOTHER_ID, OWNER, "ref 1")
    st = await env.app.billing.stats()
    assert st["review"] == 1 and st["free"] == 1
    await env.tap(MOTHER_ID, ADMIN_ID, pack("admrv"))
    assert has_text(env, MOTHER_ID, "رسیدهای در انتظار", ADMIN_ID)
    await env.tap(MOTHER_ID, ADMIN_ID, pack("admpay"))
    assert has_text(env, MOTHER_ID, "آخرین پرداخت", ADMIN_ID)
