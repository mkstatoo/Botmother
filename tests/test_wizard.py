"""Guided quick-setup wizard + panel deep link."""

from __future__ import annotations

from sqlalchemy import func, select

from botfactory.db import ChildBot, Plan, Sponsor

from .conftest import MOTHER_ID, token_for
from .fake_tg import msg_update
from .test_flows import BOT, OWNER, USER, buttons, db_all, db_one, has_text, pack


async def create_bot_via_mother(env) -> None:
    await env.tap(MOTHER_ID, OWNER, pack("new"))
    await env.say(MOTHER_ID, OWNER, token_for(BOT))


async def child(env) -> ChildBot:
    return await db_one(env, select(ChildBot).where(ChildBot.id == BOT))


async def test_wizard_happy_path(env):
    await create_bot_via_mother(env)
    last = env.fake.last("SendMessage", MOTHER_ID)
    acts = [b.get("callback_data", "") for b in buttons(last)]
    assert pack("wz", BOT, "start") in acts
    urls = [b["url"] for b in buttons(last) if "url" in b]
    assert f"https://t.me/bot{BOT}_bot?start=panel" in urls

    env.fake.add_channel(-100555, "My Channel", username="mychan")
    env.fake.admin_in.add(-100555)

    await env.tap(MOTHER_ID, OWNER, pack("wz", BOT, "start"))
    await env.say(MOTHER_ID, OWNER, "سلام، خوش آمدید 🎧")
    assert (await child(env)).welcome_text == "سلام، خوش آمدید 🎧"

    await env.tap(MOTHER_ID, OWNER, pack("wz", BOT, "sell_yes"))
    await env.say(MOTHER_ID, OWNER, "۶۰۳۷-۹۹۷۱-۲۳۴۵-۶۷۸۹")
    await env.say(MOTHER_ID, OWNER, "علی رضایی")
    c = await child(env)
    assert c.card_number == "6037 9971 2345 6789" and c.card_holder == "علی رضایی"

    await env.tap(MOTHER_ID, OWNER, pack("wz", BOT, "plan_yes"))
    await env.say(MOTHER_ID, OWNER, "۱۵۰۰۰۰")
    plans = await db_all(env, select(Plan).where(Plan.bot_id == BOT))
    assert len(plans) == 1 and plans[0].days == 30 and plans[0].price_irt == 150000 and plans[0].is_active

    await env.tap(MOTHER_ID, OWNER, pack("wz", BOT, "sp_yes"))
    await env.say(MOTHER_ID, OWNER, "@mychan")
    sponsors = await db_all(env, select(Sponsor).where(Sponsor.bot_id == BOT))
    assert [(s.chat_id, s.title) for s in sponsors] == [(-100555, "My Channel")]
    assert has_text(env, MOTHER_ID, "آماده است", OWNER)


async def test_wizard_all_steps_can_be_skipped(env):
    await create_bot_via_mother(env)
    await env.tap(MOTHER_ID, OWNER, pack("wz", BOT, "start"))
    await env.tap(MOTHER_ID, OWNER, pack("wz", BOT, "welcome_skip"))
    await env.tap(MOTHER_ID, OWNER, pack("wz", BOT, "sell_no"))
    await env.tap(MOTHER_ID, OWNER, pack("wz", BOT, "sp_no"))
    assert has_text(env, MOTHER_ID, "آماده است", OWNER)
    c = await child(env)
    assert c.welcome_text is None and c.card_number is None
    assert await db_one(env, select(func.count()).select_from(Plan)) == 0


async def test_wizard_validates_input(env):
    await create_bot_via_mother(env)
    await env.tap(MOTHER_ID, OWNER, pack("wz", BOT, "start"))
    await env.tap(MOTHER_ID, OWNER, pack("wz", BOT, "welcome_skip"))
    await env.tap(MOTHER_ID, OWNER, pack("wz", BOT, "sell_yes"))
    await env.say(MOTHER_ID, OWNER, "1234")
    assert has_text(env, MOTHER_ID, "۱۶ رقم", OWNER)
    assert (await child(env)).card_number is None
    await env.say(MOTHER_ID, OWNER, "6037997123456789")
    await env.say(MOTHER_ID, OWNER, "x")
    assert has_text(env, MOTHER_ID, "۲ تا ۶۴", OWNER)
    await env.say(MOTHER_ID, OWNER, "Ali")
    await env.tap(MOTHER_ID, OWNER, pack("wz", BOT, "plan_yes"))
    await env.say(MOTHER_ID, OWNER, "12")
    assert await db_one(env, select(func.count()).select_from(Plan)) == 0
    await env.say(MOTHER_ID, OWNER, "abc")
    assert await db_one(env, select(func.count()).select_from(Plan)) == 0
    await env.say(MOTHER_ID, OWNER, "90000")
    assert await db_one(env, select(func.count()).select_from(Plan)) == 1


async def test_wizard_sponsor_bot_not_admin(env):
    await create_bot_via_mother(env)
    env.fake.add_channel(-100666, "Chan", username="nochan")  # bot is NOT admin
    await env.tap(MOTHER_ID, OWNER, pack("wz", BOT, "sp_yes"))
    await env.say(MOTHER_ID, OWNER, "t.me/nochan")
    assert has_text(env, MOTHER_ID, "باید در این کانال ادمین باشد", OWNER)
    assert not has_text(env, MOTHER_ID, "منقضی", OWNER)
    assert await db_one(env, select(func.count()).select_from(Sponsor)) == 0
    # still in the step: after fixing, retry works
    env.fake.admin_in.add(-100666)
    await env.say(MOTHER_ID, OWNER, "@nochan")
    assert await db_one(env, select(func.count()).select_from(Sponsor)) == 1
    # garbage input is explained, not crashed
    await env.tap(MOTHER_ID, OWNER, pack("wz", BOT, "sp_yes"))
    await env.say(MOTHER_ID, OWNER, "هیچی")
    assert has_text(env, MOTHER_ID, "متوجه نشدم", OWNER)


async def test_start_command_leaves_wizard_and_wizard_is_owner_only(env):
    await create_bot_via_mother(env)
    await env.tap(MOTHER_ID, OWNER, pack("wz", BOT, "start"))
    await env.say(MOTHER_ID, OWNER, "/start")
    assert has_text(env, MOTHER_ID, "خوش آمدید", OWNER)
    await env.say(MOTHER_ID, OWNER, "متن بعد از خروج")
    assert (await child(env)).welcome_text is None  # no longer in the wizard

    # a stranger cannot drive someone else's wizard
    env.fake.clear()
    await env.tap(MOTHER_ID, USER, pack("wz", BOT, "start"))
    assert env.fake.find("AnswerCallbackQuery", MOTHER_ID, show_alert=True)
    await env.tap(MOTHER_ID, USER, pack("wz", BOT, "welcome_skip"))
    assert not has_text(env, MOTHER_ID, "مرحله ۲", USER)


async def test_panel_deep_link_and_buyer_sees_manual_plan(env):
    await create_bot_via_mother(env)
    await env.tap(MOTHER_ID, OWNER, pack("wz", BOT, "start"))
    await env.tap(MOTHER_ID, OWNER, pack("wz", BOT, "welcome_skip"))
    await env.tap(MOTHER_ID, OWNER, pack("wz", BOT, "sell_yes"))
    await env.say(MOTHER_ID, OWNER, "6037997123456789")
    await env.say(MOTHER_ID, OWNER, "Ali Rezaei")
    await env.tap(MOTHER_ID, OWNER, pack("wz", BOT, "plan_yes"))
    await env.say(MOTHER_ID, OWNER, "150000")

    # owner → panel
    await env.feed(BOT, msg_update(OWNER, "/start panel"))
    assert has_text(env, BOT, "پنل مدیریت", OWNER)
    # a normal user gets the ordinary home, never the panel
    await env.feed(BOT, msg_update(USER, "/start panel"))
    assert not has_text(env, BOT, "پنل مدیریت", USER)
    assert env.fake.texts(BOT, USER)

    # the plan created by the wizard is purchasable with the card
    await env.tap(BOT, USER, pack("plans"))
    labels = [b["text"] for b in buttons(env.fake.last("EditMessageText", BOT))]
    assert any("اشتراک ماهانه" in t and "30" not in t and "۳۰" in t for t in labels), labels
