"""End-to-end flows against a fake Telegram + fake payment gateways."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import timedelta

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramUnauthorizedError,
)
from sqlalchemy import func, select

from botfactory.callbacks import A
from botfactory.db import (
    BotUser,
    ChildBot,
    Content,
    Creator,
    Delivery,
    Payment,
    Plan,
    Purchase,
    ScheduledDelete,
    Sponsor,
    Subscription,
)
from botfactory.services.broadcast import run_broadcast
from botfactory.services.cleanup import cleanup_once
from botfactory.util import utcnow

from .conftest import ADMIN_ID, MOTHER_ID, token_for
from .fake_tg import audio_update, msg_update

BOT = 20001
OWNER = 500
USER = 700
MERCHANT = "12345678-1234-1234-1234-123456789012"


def pack(act: str, id: int = 0, p: str = "") -> str:
    return A(act=act, id=id, p=p).pack()


def buttons(call) -> list[dict]:
    markup = call.data.get("reply_markup") or {}
    return [b for row in markup.get("inline_keyboard", []) for b in row]


def has_text(env, bot_id, needle, chat_id=None) -> bool:
    return any(needle in t for t in env.fake.texts(bot_id, chat_id))


async def db_one(env, stmt):
    async with env.app.db.session() as s:
        return await s.scalar(stmt)


async def db_all(env, stmt):
    async with env.app.db.session() as s:
        return list((await s.scalars(stmt)).all())


async def add_content(env, *, title="Song", access="free", irt=None, usd=None, file_id="FID1", bot=BOT, owner=OWNER) -> int:
    await env.feed(bot, audio_update(owner, title=title, file_id=file_id))
    await env.tap(bot, owner, pack("utitle"))
    await env.tap(bot, owner, pack("udesc"))
    await env.tap(bot, owner, pack("uacc", p=access))
    if access == "premium":
        if irt:
            await env.say(bot, owner, str(irt))
        else:
            await env.tap(bot, owner, pack("uprice0"))
        if usd:
            await env.say(bot, owner, str(usd))
        else:
            await env.tap(bot, owner, pack("uusd0"))
    return await db_one(env, select(func.max(Content.id)))


async def configure_zarinpal(env):
    await env.tap(BOT, OWNER, pack("pzs"))
    await env.say(BOT, OWNER, MERCHANT)


async def configure_crypto(env, key="np-api-key-123", secret="ipn-secret-456"):
    await env.tap(BOT, OWNER, pack("pns"))
    await env.say(BOT, OWNER, key)
    await env.say(BOT, OWNER, secret)
    return key, secret


# =============================================================================== mother bot
async def test_mother_creates_bot(env):
    await env.say(MOTHER_ID, OWNER, "/start")
    assert has_text(env, MOTHER_ID, "کارخانه ربات", OWNER)
    await env.tap(MOTHER_ID, OWNER, pack("new"))
    token = token_for(BOT)
    await env.say(MOTHER_ID, OWNER, token)

    async with env.app.db.session() as s:
        child = await s.get(ChildBot, BOT)
    assert child is not None and child.owner_id == OWNER and child.username == f"bot{BOT}_bot"
    assert child.token_enc != token and env.app.box.decrypt(child.token_enc) == token
    assert env.app.manager.get_bot(BOT) is not None
    assert env.fake.find("DeleteMessage", MOTHER_ID), "the message holding the token must be deleted"
    assert has_text(env, MOTHER_ID, "ساخته و فعال شد", OWNER)
    assert env.fake.find("SetMyCommands", BOT)


async def test_mother_rejects_bad_tokens(env):
    await env.tap(MOTHER_ID, OWNER, pack("new"))
    await env.say(MOTHER_ID, OWNER, "not-a-token")
    assert has_text(env, MOTHER_ID, "فرمت توکن")

    env.fake.fail["GetMe"] = lambda c: TelegramUnauthorizedError(method=None, message="Unauthorized") if c.bot_id == 30003 else None
    await env.say(MOTHER_ID, OWNER, token_for(30003))
    assert has_text(env, MOTHER_ID, "معتبر نیست")
    assert await db_one(env, select(func.count()).select_from(ChildBot)) == 0

    # the mother's own token is refused
    env.fake.fail.clear()
    await env.say(MOTHER_ID, OWNER, f"{MOTHER_ID}:" + "M" * 35)
    assert has_text(env, MOTHER_ID, "همین ربات")


async def test_mother_ownership_and_limits(env):
    await env.make_child(BOT, owner=OWNER)
    # someone else cannot register the same bot
    await env.tap(MOTHER_ID, 501, pack("new"))
    await env.say(MOTHER_ID, 501, token_for(BOT))
    assert has_text(env, MOTHER_ID, "قبلاً توسط کاربر دیگری", 501)
    # per-user limit
    env.app.settings.max_bots_per_user = 1
    env.fake.clear()
    await env.tap(MOTHER_ID, OWNER, pack("new"))
    assert env.fake.find("AnswerCallbackQuery", MOTHER_ID, show_alert=True)


async def test_mother_pause_delete_and_isolation(env):
    await env.make_child(BOT, owner=OWNER)
    await env.make_child(20002, owner=OWNER)
    await env.tap(MOTHER_ID, OWNER, pack("mpause", BOT))
    assert await db_one(env, select(ChildBot.status).where(ChildBot.id == BOT)) == "paused"
    # a stranger cannot touch it
    await env.tap(MOTHER_ID, 501, pack("mresume", BOT))
    assert await db_one(env, select(ChildBot.status).where(ChildBot.id == BOT)) == "paused"
    # paused bot answers users with "unavailable" but keeps the owner working
    env.fake.clear()
    await env.say(BOT, USER, "/start")
    assert has_text(env, BOT, "موقتاً در دسترس نیست", USER)
    await env.say(BOT, OWNER, "/start")
    assert has_text(env, BOT, "خوش آمدید", OWNER)
    await env.tap(MOTHER_ID, OWNER, pack("mresume", BOT))
    assert await db_one(env, select(ChildBot.status).where(ChildBot.id == BOT)) == "active"

    # deletion purges everything
    cid = await add_content(env)
    assert cid
    await env.tap(MOTHER_ID, OWNER, pack("mdely", BOT))
    assert await db_one(env, select(func.count()).select_from(ChildBot).where(ChildBot.id == BOT)) == 0
    assert await db_one(env, select(func.count()).select_from(Content)) == 0
    assert await db_one(env, select(func.count()).select_from(BotUser).where(BotUser.bot_id == BOT)) == 0
    assert env.app.manager.get_bot(BOT) is None
    assert env.app.manager.get_bot(20002) is not None


async def test_platform_admin_can_suspend(env):
    await env.make_child(BOT, owner=OWNER)
    await env.say(MOTHER_ID, ADMIN_ID, "/admin")
    assert has_text(env, MOTHER_ID, "مدیریت پلتفرم", ADMIN_ID)
    await env.tap(MOTHER_ID, 501, pack("adms", BOT))  # not an admin → ignored
    assert await db_one(env, select(ChildBot.status)) == "active"
    await env.tap(MOTHER_ID, ADMIN_ID, pack("adms", BOT))
    assert await db_one(env, select(ChildBot.status)) == "suspended"
    await env.say(MOTHER_ID, OWNER, "/start")
    await env.tap(MOTHER_ID, OWNER, pack("mresume", BOT))  # owner can't lift an admin suspension
    assert await db_one(env, select(ChildBot.status)) == "suspended"
    await env.tap(MOTHER_ID, ADMIN_ID, pack("admu", BOT))
    assert await db_one(env, select(ChildBot.status)) == "active"


# =============================================================================== content upload
async def test_owner_upload_flow_and_listing(env):
    await env.make_child(BOT, owner=OWNER)
    # non-owner media is ignored
    await env.feed(BOT, audio_update(USER, "Hack", "HACK"))
    assert await db_one(env, select(func.count()).select_from(Content)) == 0

    cid = await add_content(env, title="Song One", file_id="FID1")
    async with env.app.db.session() as s:
        c = await s.get(Content, cid)
    assert (c.title, c.kind, c.storage, c.file_id, c.access) == ("Song One", "audio", "telegram", "FID1", "free")
    assert c.duration == 120 and c.file_size == 4_000_000
    assert has_text(env, BOT, f"c_{cid}", OWNER)  # share link

    # users see it in the list and via search
    await env.tap(BOT, USER, pack("list", 0, "c"))
    last = env.fake.last("EditMessageText", BOT)
    assert any(b["callback_data"] == pack("get", cid) for b in buttons(last))
    await env.say(BOT, USER, "song")
    assert any(b.get("callback_data") == pack("get", cid) for b in buttons(env.fake.last("SendMessage", BOT)))

    # custom title, description and categories
    await env.tap(BOT, OWNER, pack("catadd"))
    await env.say(BOT, OWNER, "Podcasts")
    assert await db_one(env, select(func.count()).select_from(__import__("botfactory.db", fromlist=["Category"]).Category)) == 1
    await env.feed(BOT, audio_update(OWNER, title="Ep 1", file_id="FID2"))
    await env.say(BOT, OWNER, "My Episode")
    await env.say(BOT, OWNER, "A nice description")
    cat_id = await db_one(env, select(__import__("botfactory.db", fromlist=["Category"]).Category.id))
    await env.tap(BOT, OWNER, pack("ucat", cat_id))
    await env.tap(BOT, OWNER, pack("uacc", p="free"))
    c2 = await db_one(env, select(Content).where(Content.file_id == "FID2"))
    assert (c2.title, c2.description, c2.category_id) == ("My Episode", "A nice description", cat_id)


async def test_owner_can_edit_toggle_delete_content(env):
    await env.make_child(BOT, owner=OWNER)
    cid = await add_content(env, title="Old")
    await env.tap(BOT, OWNER, pack("ce", cid, "title"))
    await env.say(BOT, OWNER, "New title")
    await env.tap(BOT, OWNER, pack("ct", cid, "access"))
    await env.tap(BOT, OWNER, pack("ce", cid, "price_irt"))
    await env.say(BOT, OWNER, "۷۵٬۰۰۰")
    await env.tap(BOT, OWNER, pack("ce", cid, "price_usd"))
    await env.say(BOT, OWNER, "3.5")
    async with env.app.db.session() as s:
        c = await s.get(Content, cid)
    assert (c.title, c.access, c.price_irt, c.price_usd_cents) == ("New title", "premium", 75000, 350)

    await env.tap(BOT, OWNER, pack("ct", cid, "active"))
    env.fake.clear()
    await env.tap(BOT, USER, pack("get", cid))
    assert has_text(env, BOT, "در دسترس نیست", USER)

    await env.tap(BOT, OWNER, pack("cs", cid))  # owner preview works even when inactive
    assert env.fake.find("SendAudio", BOT, chat_id=OWNER)
    await env.tap(BOT, OWNER, pack("cxy", cid))
    assert await db_one(env, select(func.count()).select_from(Content)) == 0


async def test_link_content(env):
    await env.make_child(BOT, owner=OWNER)
    await env.tap(BOT, OWNER, pack("alink"))
    await env.say(BOT, OWNER, "not a url")
    assert has_text(env, BOT, "آدرس معتبر نیست")
    await env.say(BOT, OWNER, "https://example.com/big-file.mp4")
    await env.say(BOT, OWNER, "Big video")
    await env.tap(BOT, OWNER, pack("udesc"))
    await env.tap(BOT, OWNER, pack("uacc", p="free"))
    c = await db_one(env, select(Content))
    assert (c.kind, c.storage, c.external_url) == ("link", "external", "https://example.com/big-file.mp4")
    await env.say(BOT, USER, f"/start c_{c.id}")
    sent = env.fake.last("SendMessage", BOT)
    assert any(b.get("url") == "https://example.com/big-file.mp4" for b in buttons(sent))


async def test_storage_channel_hybrid(env):
    await env.make_child(BOT, owner=OWNER)
    env.fake.add_channel(-100777, "Vault")
    env.fake.admin_in.add(-100777)
    await env.tap(BOT, OWNER, pack("stc"))
    await env.say(BOT, OWNER, "-100777")
    assert await db_one(env, select(ChildBot.storage_channel_id)) == -100777

    # upload → the owner can choose per content
    await env.feed(BOT, audio_update(OWNER, "Vaulted", "FID-V"))
    await env.tap(BOT, OWNER, pack("utitle"))
    await env.tap(BOT, OWNER, pack("udesc"))
    await env.tap(BOT, OWNER, pack("uacc", p="free"))
    assert any(b["callback_data"] == pack("ustore", 0, "channel") for b in buttons(env.fake.last("EditMessageText", BOT)))
    await env.tap(BOT, OWNER, pack("ustore", 0, "channel"))
    c = await db_one(env, select(Content))
    assert c.storage == "channel" and c.storage_chat_id == -100777 and c.storage_message_id
    assert env.fake.find("CopyMessage", BOT, chat_id=-100777)

    # and a second one stored directly in Telegram
    await env.feed(BOT, audio_update(OWNER, "Direct", "FID-D"))
    await env.tap(BOT, OWNER, pack("utitle"))
    await env.tap(BOT, OWNER, pack("udesc"))
    await env.tap(BOT, OWNER, pack("uacc", p="free"))
    await env.tap(BOT, OWNER, pack("ustore", 0, "telegram"))
    direct = await db_one(env, select(Content).where(Content.file_id == "FID-D"))
    assert direct.storage == "telegram"

    # delivery: channel content is copied from the vault, direct content by file_id
    env.fake.clear()
    await env.say(BOT, USER, f"/start c_{c.id}")
    copy = env.fake.find("CopyMessage", BOT, chat_id=USER)
    assert copy and copy[0].data["from_chat_id"] == -100777 and copy[0].data["message_id"] == c.storage_message_id
    await env.say(BOT, USER, f"/start c_{direct.id}")
    assert env.fake.find("SendAudio", BOT, chat_id=USER, audio="FID-D")

    # vault message vanished → falls back to the stored file_id
    env.fake.fail["CopyMessage"] = lambda call: TelegramBadRequest(method=None, message="message to copy not found") if call.data.get("from_chat_id") == -100777 else None
    env.fake.clear()
    await env.say(BOT, USER, f"/start c_{c.id}")
    assert env.fake.find("SendAudio", BOT, chat_id=USER, audio="FID-V")


async def test_storage_channel_requires_admin(env):
    await env.make_child(BOT, owner=OWNER)
    env.fake.add_channel(-100888, "NotAdmin")
    await env.tap(BOT, OWNER, pack("stc"))
    await env.say(BOT, OWNER, "-100888")
    assert has_text(env, BOT, "ادمین")
    assert await db_one(env, select(ChildBot.storage_channel_id)) is None


# =============================================================================== sponsors
async def test_sponsor_gate_for_free_content(env):
    await env.make_child(BOT, owner=OWNER)
    cid = await add_content(env, title="Free song")
    env.fake.add_channel(-100111, "Sponsor One", username="sponsorone")
    env.fake.add_channel(-100222, "Private Sponsor")
    env.fake.admin_in.update({-100111, -100222})

    await env.tap(BOT, OWNER, pack("spadd"))
    origin = {"type": "channel", "date": int(time.time()), "chat": {"id": -100111, "type": "channel", "title": "Sponsor One"}, "message_id": 5}
    await env.feed(BOT, msg_update(OWNER, "fwd", forward_origin=origin))
    await env.tap(BOT, OWNER, pack("spadd"))
    await env.say(BOT, OWNER, "-100222")
    sponsors = await db_all(env, select(Sponsor).order_by(Sponsor.id))
    assert [(s.chat_id, s.username) for s in sponsors] == [(-100111, "sponsorone"), (-100222, None)]
    assert sponsors[1].invite_link == "https://t.me/+fakeinvite"

    # not an admin there → rejected
    env.fake.add_channel(-100333, "Foreign")
    await env.tap(BOT, OWNER, pack("spadd"))
    await env.say(BOT, OWNER, "-100333")
    assert await db_one(env, select(func.count()).select_from(Sponsor)) == 2

    env.fake.clear()
    await env.say(BOT, USER, f"/start c_{cid}")
    assert not env.fake.find("SendAudio", BOT)
    prompt = env.fake.last("SendMessage", BOT)
    urls = [b["url"] for b in buttons(prompt) if "url" in b]
    assert urls == ["https://t.me/sponsorone", "https://t.me/+fakeinvite"]
    assert any(b.get("callback_data") == pack("chk", cid) for b in buttons(prompt))

    # joins only one of them → still blocked, prompt shrinks
    env.fake.members[(-100111, USER)] = "member"
    await env.tap(BOT, USER, pack("chk", cid))
    assert not env.fake.find("SendAudio", BOT)
    assert [b["url"] for b in buttons(env.fake.last("EditMessageText", BOT)) if "url" in b] == ["https://t.me/+fakeinvite"]

    env.fake.members[(-100222, USER)] = "member"
    await env.tap(BOT, USER, pack("chk", cid))
    assert env.fake.find("SendAudio", BOT, chat_id=USER, audio="FID1")
    assert env.fake.find("DeleteMessage", BOT), "the join prompt is cleaned up"
    assert await db_one(env, select(func.count()).select_from(Delivery)) == 1
    assert await db_one(env, select(Content.downloads)) == 1

    # the owner is never gated, and the gate can be switched off
    env.fake.clear()
    await env.say(BOT, OWNER, f"/start c_{cid}")
    assert env.fake.find("SendAudio", BOT, chat_id=OWNER)
    await env.tap(BOT, OWNER, pack("spgate"))
    await env.say(BOT, 800, f"/start c_{cid}")
    assert env.fake.find("SendAudio", BOT, chat_id=800)


async def test_unverifiable_sponsor_does_not_lock_users_out(env):
    await env.make_child(BOT, owner=OWNER)
    cid = await add_content(env)
    async with env.app.db.session() as s:
        s.add(Sponsor(bot_id=BOT, chat_id=-100999, title="Broken", username="broken"))
        await s.commit()
    env.fake.fail["GetChatMember"] = lambda c: TelegramBadRequest(method=None, message="Bad Request: chat not found")
    await env.say(BOT, USER, f"/start c_{cid}")
    assert env.fake.find("SendAudio", BOT, chat_id=USER)


# =============================================================================== payments
async def test_premium_without_any_payment_method(env):
    await env.make_child(BOT, owner=OWNER)
    cid = await add_content(env, access="premium", irt=50000)
    await env.say(BOT, USER, f"/start c_{cid}")
    assert not env.fake.find("SendAudio", BOT)
    assert has_text(env, BOT, "روش پرداختی", USER)


async def test_zarinpal_content_purchase(env):
    await env.make_child(BOT, owner=OWNER)
    await configure_zarinpal(env)
    assert env.fake.find("DeleteMessage", BOT), "merchant id message is deleted"
    async with env.app.db.session() as s:
        child = await s.get(ChildBot, BOT)
    assert env.app.box.decrypt(child.zarinpal_merchant_enc) == MERCHANT
    cid = await add_content(env, access="premium", irt=50000)

    env.fake.clear()
    await env.say(BOT, USER, f"/start c_{cid}")
    assert not env.fake.find("SendAudio", BOT)
    prompt = env.fake.last("SendMessage", BOT)
    assert any(b.get("callback_data") == pack("buyc", cid) for b in buttons(prompt))

    await env.tap(BOT, USER, pack("buyc", cid))
    assert any(b.get("callback_data") == pack("pay", cid, "c.zarinpal") for b in buttons(env.fake.last("EditMessageText", BOT)))
    await env.tap(BOT, USER, pack("pay", cid, "c.zarinpal"))

    pay = await db_one(env, select(Payment))
    assert (pay.status, pay.method, pay.amount, pay.currency, pay.kind) == ("pending", "zarinpal", 50000, "IRT", "content")
    req = env.gateway.requests[0]
    assert req["merchant_id"] == MERCHANT and req["amount"] == 500000  # Toman → Rial
    assert req["callback_url"] == f"https://bf.example/pay/zarinpal/{pay.id}"
    pay_btn = [b for b in buttons(env.fake.last("EditMessageText", BOT)) if "url" in b][0]
    assert pay_btn["url"] == f"{env.gateway.base}/pg/StartPay/{pay.authority}"

    # forged / wrong callbacks do nothing
    r = await env.web.get(f"/pay/zarinpal/{pay.id}", params={"Authority": "WRONG", "Status": "OK"})
    assert r.status == 400 and not env.gateway.verify_calls
    assert await db_one(env, select(func.count()).select_from(Purchase)) == 0

    # real callback
    r = await env.web.get(f"/pay/zarinpal/{pay.id}", params={"Authority": pay.authority, "Status": "OK"})
    assert r.status == 200 and "987654" in await r.text()
    await env.drain()
    assert env.gateway.verify_calls == [{"merchant_id": MERCHANT, "amount": 500000, "authority": pay.authority}]
    paid = await db_one(env, select(Payment))
    assert paid.status == "paid" and paid.ref == "987654" and paid.paid_at is not None
    assert await db_one(env, select(func.count()).select_from(Purchase)) == 1
    assert len(env.fake.find("SendAudio", BOT, chat_id=USER)) == 1
    assert has_text(env, BOT, "فروش جدید", OWNER)

    # replaying the callback is harmless
    r = await env.web.get(f"/pay/zarinpal/{pay.id}", params={"Authority": pay.authority, "Status": "OK"})
    await env.drain()
    assert r.status == 200
    assert len(env.fake.find("SendAudio", BOT, chat_id=USER)) == 1
    assert len(env.gateway.verify_calls) == 1

    # already purchased → later requests deliver immediately
    env.fake.clear()
    await env.say(BOT, USER, f"/start c_{cid}")
    assert env.fake.find("SendAudio", BOT, chat_id=USER)


async def test_zarinpal_failures(env):
    await env.make_child(BOT, owner=OWNER)
    await configure_zarinpal(env)
    cid = await add_content(env, access="premium", irt=50000)

    env.gateway.reject_request = True
    await env.tap(BOT, USER, pack("pay", cid, "c.zarinpal"))
    assert has_text(env, BOT, "درگاه پرداخت درخواست را نپذیرفت", USER)
    env.gateway.reject_request = False

    # user cancels at the bank
    await env.tap(BOT, USER, pack("pay", cid, "c.zarinpal"))
    pay = (await db_all(env, select(Payment).order_by(Payment.id.desc())))[0]
    r = await env.web.get(f"/pay/zarinpal/{pay.id}", params={"Authority": pay.authority, "Status": "NOK"})
    assert r.status == 400
    assert await db_one(env, select(Payment.status).where(Payment.id == pay.id)) == "failed"
    assert not env.gateway.verify_calls

    # bank says verification failed
    await env.tap(BOT, USER, pack("pay", cid, "c.zarinpal"))
    pay2 = (await db_all(env, select(Payment).order_by(Payment.id.desc())))[0]
    env.gateway.reject_verify = True
    r = await env.web.get(f"/pay/zarinpal/{pay2.id}", params={"Authority": pay2.authority, "Status": "OK"})
    assert r.status == 400
    assert await db_one(env, select(func.count()).select_from(Purchase)) == 0
    assert not env.fake.find("SendAudio", BOT)

    # method that is not configured / not for this item
    await env.tap(BOT, USER, pack("pay", cid, "c.crypto"))
    assert env.fake.find("AnswerCallbackQuery", BOT, text="این روش پرداخت فعال نیست.")


async def test_subscription_plans_and_stacking(env):
    await env.make_child(BOT, owner=OWNER)
    await configure_zarinpal(env)
    premium = await add_content(env, title="VIP", access="premium", irt=90000, file_id="VIP1")
    free = await add_content(env, title="Free", file_id="FREE1")
    env.fake.add_channel(-100111, "Sponsor", username="sp")
    env.fake.admin_in.add(-100111)
    async with env.app.db.session() as s:
        s.add(Sponsor(bot_id=BOT, chat_id=-100111, title="Sponsor", username="sp"))
        await s.commit()

    # owner creates a plan through the panel
    await env.tap(BOT, OWNER, pack("pladd"))
    await env.say(BOT, OWNER, "ماهانه")
    await env.say(BOT, OWNER, "30")
    await env.say(BOT, OWNER, "100000")
    await env.say(BOT, OWNER, "0")
    plan = await db_one(env, select(Plan))
    assert (plan.name, plan.days, plan.price_irt, plan.price_usd_cents) == ("ماهانه", 30, 100000, None)

    async def buy():
        await env.tap(BOT, USER, pack("pay", plan.id, "p.zarinpal"))
        pay = (await db_all(env, select(Payment).order_by(Payment.id.desc())))[0]
        r = await env.web.get(f"/pay/zarinpal/{pay.id}", params={"Authority": pay.authority, "Status": "OK"})
        assert r.status == 200
        await env.drain()

    await env.tap(BOT, USER, pack("plans"))
    assert any(b.get("callback_data") == pack("plan", plan.id) for b in buttons(env.fake.last("EditMessageText", BOT)))

    # before subscribing: premium locked, free gated
    env.fake.clear()
    await env.say(BOT, USER, f"/start c_{premium}")
    await env.say(BOT, USER, f"/start c_{free}")
    assert not env.fake.find("SendAudio", BOT)

    await buy()
    sub = await db_one(env, select(Subscription))
    assert sub.plan_id == plan.id and abs((sub.expires_at - sub.starts_at) - timedelta(days=30)) < timedelta(seconds=5)
    assert has_text(env, BOT, "اشتراک شما فعال شد", USER)

    env.fake.clear()
    await env.say(BOT, USER, f"/start c_{premium}")
    await env.say(BOT, USER, f"/start c_{free}")  # subscribers skip the sponsor gate
    assert {c.data["audio"] for c in env.fake.find("SendAudio", BOT, chat_id=USER)} == {"VIP1", "FREE1"}

    await buy()  # renewal stacks on top of the running subscription
    subs = await db_all(env, select(Subscription).order_by(Subscription.id))
    assert len(subs) == 2 and subs[1].starts_at == subs[0].expires_at
    assert abs((subs[1].expires_at - utcnow()) - timedelta(days=60)) < timedelta(minutes=1)

    # removing the plan from sale keeps existing subscribers
    await env.tap(BOT, OWNER, pack("pldel", plan.id))
    assert not await db_one(env, select(Plan.is_active))
    env.fake.clear()
    await env.say(BOT, USER, f"/start c_{premium}")
    assert env.fake.find("SendAudio", BOT, chat_id=USER)


async def test_crypto_payment_via_ipn(env):
    await env.make_child(BOT, owner=OWNER)
    key, secret = await configure_crypto(env)
    cid = await add_content(env, access="premium", irt=None, usd="2.5")
    await env.tap(BOT, USER, pack("pay", cid, "c.crypto"))

    pay = await db_one(env, select(Payment))
    assert (pay.method, pay.amount, pay.currency, pay.authority) == ("crypto", 250, "USD", "inv1")
    inv = env.gateway.invoices[0]
    assert inv["_api_key"] == key and inv["price_amount"] == 2.5 and inv["price_currency"] == "usd"
    assert inv["order_id"] == f"bf-{pay.id}" and inv["ipn_callback_url"] == f"https://bf.example/pay/nowpayments/{pay.id}"
    assert any(b.get("url") == "https://nowpayments.example/pay/inv1" for b in buttons(env.fake.last("EditMessageText", BOT)))

    def ipn(status="finished", amount=2.5, order=None):
        body = {"payment_id": 424242, "payment_status": status, "order_id": order or f"bf-{pay.id}", "price_amount": amount, "price_currency": "usd", "pay_currency": "usdttrc20"}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        sig = hmac.new(secret.encode(), canonical.encode(), hashlib.sha512).hexdigest()
        return json.dumps(body), {"x-nowpayments-sig": sig, "Content-Type": "application/json"}

    url = f"/pay/nowpayments/{pay.id}"
    body, headers = ipn()
    assert (await env.web.post(url, data=body, headers={**headers, "x-nowpayments-sig": "00"})).status == 403
    assert (await env.web.post(url, data=body)).status == 403
    b2, h2 = ipn(amount=0.1)
    assert (await env.web.post(url, data=b2, headers=h2)).status == 400  # amount tampering
    b3, h3 = ipn(order="bf-999")
    assert (await env.web.post(url, data=b3, headers=h3)).status == 400
    assert (await env.web.post("/pay/nowpayments/9999", data=body, headers=headers)).status == 404
    assert await db_one(env, select(func.count()).select_from(Purchase)) == 0

    b4, h4 = ipn("waiting")
    assert (await env.web.post(url, data=b4, headers=h4)).status == 200
    assert await db_one(env, select(Payment.status)) == "pending"

    assert (await env.web.post(url, data=body, headers=headers)).status == 200
    await env.drain()
    assert await db_one(env, select(Payment.status)) == "paid"
    assert await db_one(env, select(Payment.ref)) == "424242"
    assert len(env.fake.find("SendAudio", BOT, chat_id=USER)) == 1
    # duplicate IPN (they retry) must not double-deliver
    assert (await env.web.post(url, data=body, headers=headers)).status == 200
    await env.drain()
    assert len(env.fake.find("SendAudio", BOT, chat_id=USER)) == 1
    assert await db_one(env, select(func.count()).select_from(Purchase)) == 1


async def test_crypto_partial_payment_goes_to_review(env):
    await env.make_child(BOT, owner=OWNER)
    _, secret = await configure_crypto(env)
    cid = await add_content(env, access="premium", usd="5")
    await env.tap(BOT, USER, pack("pay", cid, "c.crypto"))
    pay = await db_one(env, select(Payment))
    body = {"payment_status": "partially_paid", "order_id": f"bf-{pay.id}", "price_amount": 5, "price_currency": "usd"}
    sig = hmac.new(secret.encode(), json.dumps(body, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha512).hexdigest()
    r = await env.web.post(f"/pay/nowpayments/{pay.id}", data=json.dumps(body), headers={"x-nowpayments-sig": sig})
    assert r.status == 200
    await env.drain()
    assert await db_one(env, select(Payment.status)) == "review"
    review = env.fake.find("SendMessage", BOT, chat_id=OWNER, text="ناقص")
    assert review and any(b["callback_data"] == pack("mp", pay.id, "ok") for b in buttons(review[-1]))


async def test_manual_payment_with_owner_approval(env):
    await env.make_child(BOT, owner=OWNER)
    await env.tap(BOT, OWNER, pack("pms"))
    await env.say(BOT, OWNER, "۶۰۳۷-۹۹۱۱-۲۲۳۳-۴۴۵۵")
    await env.say(BOT, OWNER, "Ali Rezaei")
    await env.tap(BOT, OWNER, pack("pmskip"))
    async with env.app.db.session() as s:
        child = await s.get(ChildBot, BOT)
    assert (child.card_number, child.card_holder) == ("6037 9911 2233 4455", "Ali Rezaei")

    cid = await add_content(env, access="premium", irt=30000)
    env.fake.clear()
    await env.tap(BOT, USER, pack("pay", cid, "c.manual"))
    shown = env.fake.last("EditMessageText", BOT).data["text"]
    assert "6037 9911 2233 4455" in shown and "Ali Rezaei" in shown and "۳۰٬۰۰۰ تومان" in shown

    photo = [{"file_id": "RCPT1", "file_unique_id": "r1", "width": 10, "height": 10}]
    await env.feed(BOT, msg_update(USER, None, photo=photo, caption="paid at 10:00"))
    await env.drain()
    pay = await db_one(env, select(Payment))
    assert pay.status == "review" and pay.receipt_file_id == "RCPT1" and pay.receipt_text == "paid at 10:00"
    notice = env.fake.find("SendPhoto", BOT, chat_id=OWNER)
    assert notice and notice[0].data["photo"] == "RCPT1"
    ok_btn = pack("mp", pay.id, "ok")
    assert any(b["callback_data"] == ok_btn for b in buttons(notice[0]))

    # the buyer cannot approve their own payment
    await env.tap(BOT, USER, ok_btn)
    assert await db_one(env, select(Payment.status)) == "review"

    env.fake.clear()
    await env.tap(BOT, OWNER, ok_btn)
    await env.drain()
    assert await db_one(env, select(Payment.status)) == "paid"
    assert env.fake.find("SendAudio", BOT, chat_id=USER)
    await env.tap(BOT, OWNER, ok_btn)  # double click
    await env.drain()
    assert len(env.fake.find("SendAudio", BOT, chat_id=USER)) == 1


async def test_manual_payment_rejection(env):
    await env.make_child(BOT, owner=OWNER, card_number="6037 9911 2233 4455")
    cid = await add_content(env, access="premium", irt=30000)
    await env.tap(BOT, USER, pack("pay", cid, "c.manual"))
    await env.say(BOT, USER, "tracking 12345")
    await env.drain()
    pay = await db_one(env, select(Payment))
    assert pay.status == "review" and pay.receipt_text == "tracking 12345"
    env.fake.clear()
    await env.tap(BOT, OWNER, pack("mp", pay.id, "no"))
    assert await db_one(env, select(Payment.status)) == "rejected"
    assert has_text(env, BOT, "تأیید نشد", USER)
    assert not env.fake.find("SendAudio", BOT)


# =============================================================================== misc behaviours
async def test_auto_delete_after_delivery(env):
    await env.make_child(BOT, owner=OWNER)
    cid = await add_content(env)
    await env.tap(BOT, OWNER, pack("sta", 0, "60"))
    assert await db_one(env, select(ChildBot.auto_delete_seconds)) == 60
    env.fake.clear()
    await env.say(BOT, USER, f"/start c_{cid}")
    rows = await db_all(env, select(ScheduledDelete))
    assert len(rows) == 2  # the file + the warning notice
    assert has_text(env, BOT, "حذف می‌شود", USER)

    assert await cleanup_once(env.app) == 0  # not due yet
    async with env.app.db.session() as s:
        for r in (await s.scalars(select(ScheduledDelete))).all():
            r.delete_at = utcnow() - timedelta(seconds=1)
        await s.commit()
    assert await cleanup_once(env.app) == 2
    assert len(env.fake.find("DeleteMessage", BOT, chat_id=USER)) == 2
    assert await db_one(env, select(func.count()).select_from(ScheduledDelete)) == 0


async def test_protect_content_flag(env):
    await env.make_child(BOT, owner=OWNER)
    cid = await add_content(env)
    await env.tap(BOT, OWNER, pack("stp"))
    env.fake.clear()
    await env.say(BOT, USER, f"/start c_{cid}")
    assert env.fake.find("SendAudio", BOT, chat_id=USER, protect_content=True)


async def test_broadcast_counts_and_blocked_users(env):
    await env.make_child(BOT, owner=OWNER)
    for uid in (701, 702, 703):
        await env.say(BOT, uid, "/start")
    await env.say(BOT, OWNER, "/start")
    env.fake.fail["CopyMessage"] = lambda c: TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user") if c.data["chat_id"] == 702 else None
    stats = await run_broadcast(env.app, BOT, OWNER, 77, rate=1000)
    assert (stats.total, stats.sent, stats.blocked, stats.failed) == (4, 3, 1, 0)
    assert await db_one(env, select(BotUser.is_blocked).where(BotUser.user_id == 702)) is True
    stats = await run_broadcast(env.app, BOT, OWNER, 77, rate=1000)
    assert stats.total == 3  # blocked users are skipped from now on

    # through the owner UI
    env.fake.fail.clear()
    env.fake.clear()
    await env.tap(BOT, OWNER, pack("bc"))
    await env.say(BOT, OWNER, "Hello everyone")
    await env.tap(BOT, OWNER, pack("bcy"))
    await env.drain()
    assert len(env.fake.find("CopyMessage", BOT)) == 3
    assert has_text(env, BOT, "پایان ارسال همگانی", OWNER)


async def test_user_blocking_bot_is_tracked(env):
    await env.make_child(BOT, owner=OWNER)
    await env.say(BOT, USER, "/start")
    upd = {
        "update_id": 99999,
        "my_chat_member": {
            "chat": {"id": USER, "type": "private"},
            "from": {"id": USER, "is_bot": False, "first_name": "U"},
            "date": int(time.time()),
            "old_chat_member": {"status": "member", "user": {"id": BOT, "is_bot": True, "first_name": "B"}},
            "new_chat_member": {"status": "kicked", "until_date": 0, "user": {"id": BOT, "is_bot": True, "first_name": "B"}},
        },
    }
    await env.feed(BOT, upd)
    assert await db_one(env, select(BotUser.is_blocked).where(BotUser.user_id == USER)) is True
    await env.say(BOT, USER, "/start")  # comes back
    assert await db_one(env, select(BotUser.is_blocked).where(BotUser.user_id == USER)) is False


async def test_stats_screen(env):
    await env.make_child(BOT, owner=OWNER)
    await configure_zarinpal(env)
    cid = await add_content(env, access="premium", irt=50000)
    await env.tap(BOT, USER, pack("pay", cid, "c.zarinpal"))
    pay = await db_one(env, select(Payment))
    await env.web.get(f"/pay/zarinpal/{pay.id}", params={"Authority": pay.authority, "Status": "OK"})
    await env.drain()
    await env.tap(BOT, OWNER, pack("stat"))
    text = env.fake.last("EditMessageText", BOT).data["text"]
    assert "۵۰٬۰۰۰ تومان" in text and "کاربران" in text and "پردانلودترین" in text


async def test_welcome_text_is_escaped_and_customisable(env):
    await env.make_child(BOT, owner=OWNER)
    await env.tap(BOT, OWNER, pack("stw"))
    await env.say(BOT, OWNER, "Welcome <b>friends</b> & family")
    env.fake.clear()
    await env.say(BOT, USER, "/start")
    assert "Welcome &lt;b&gt;friends&lt;/b&gt; &amp; family" in env.fake.last("SendMessage", BOT).data["text"]


async def test_non_owner_cannot_reach_panel(env):
    await env.make_child(BOT, owner=OWNER)
    env.fake.clear()
    await env.say(BOT, USER, "/panel")
    await env.tap(BOT, USER, pack("panel"))
    await env.tap(BOT, USER, pack("pzs"))
    assert not has_text(env, BOT, "پنل مدیریت")
    await env.say(BOT, OWNER, "/panel")
    assert has_text(env, BOT, "پنل مدیریت", OWNER)


# =============================================================================== web hooks
async def test_telegram_webhooks_are_authenticated(env):
    await env.make_child(BOT, owner=OWNER)
    payload = msg_update(USER, "/start")

    r = await env.web.post(f"/tg/c/{BOT}/secret-{BOT}", json=payload, headers={"X-Telegram-Bot-Api-Secret-Token": f"secret-{BOT}"})
    assert r.status == 200
    await env.drain()
    for _ in range(50):
        if has_text(env, BOT, "خوش آمدید", USER):
            break
        await __import__("asyncio").sleep(0.02)
    assert has_text(env, BOT, "خوش آمدید", USER)

    assert (await env.web.post(f"/tg/c/{BOT}/wrong", json=payload, headers={"X-Telegram-Bot-Api-Secret-Token": f"secret-{BOT}"})).status == 403
    assert (await env.web.post(f"/tg/c/{BOT}/secret-{BOT}", json=payload)).status == 403
    assert (await env.web.post("/tg/c/4242/secret-4242", json=payload, headers={"X-Telegram-Bot-Api-Secret-Token": "secret-4242"})).status == 404
    assert (await env.web.post(f"/tg/c/{BOT}/secret-{BOT}", data="nope", headers={"X-Telegram-Bot-Api-Secret-Token": f"secret-{BOT}"})).status == 400

    secret = env.app.mother_secret
    r = await env.web.post(f"/tg/m/{secret}", json=msg_update(OWNER, "/start"), headers={"X-Telegram-Bot-Api-Secret-Token": secret})
    assert r.status == 200
    assert (await env.web.post("/tg/m/bad", json=payload, headers={"X-Telegram-Bot-Api-Secret-Token": secret})).status == 403
    assert (await env.web.get("/healthz")).status == 200


async def test_creator_registered_on_first_contact(env):
    await env.say(MOTHER_ID, 888, "/start")
    async with env.app.db.session() as s:
        assert (await s.get(Creator, 888)) is not None
