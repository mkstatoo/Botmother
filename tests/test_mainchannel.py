"""Platform main channel: admin menu + mandatory membership for creators and child-bot users."""

from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest

from .conftest import ADMIN_ID, MOTHER_ID
from .test_flows import BOT, OWNER, USER, add_content, buttons, has_text, pack

CH = -100555
STRANGER = 4242


async def define_channel(env, username="mainchan"):
    env.fake.add_channel(CH, "کانال اصلی", username=username)
    env.fake.admin_in.add(CH)
    await env.tap(MOTHER_ID, ADMIN_ID, pack("admmc"))
    await env.tap(MOTHER_ID, ADMIN_ID, pack("admmcs"))
    await env.say(MOTHER_ID, ADMIN_ID, f"@{username}")


def join(env, uid):
    env.fake.members[(CH, uid)] = "member"


async def test_admin_defines_edits_and_removes_the_channel(env):
    # a non-admin cannot even open the menu
    await env.tap(MOTHER_ID, STRANGER, pack("admmc"))
    assert (await env.app.mainchan.config()).active is False

    env.fake.add_channel(CH, "کانال اصلی", username="mainchan")
    await env.tap(MOTHER_ID, ADMIN_ID, pack("admmc"))
    assert any(b["callback_data"] == pack("admmcs") for b in buttons(env.fake.last("EditMessageText", MOTHER_ID)))
    await env.tap(MOTHER_ID, ADMIN_ID, pack("admmcs"))
    await env.say(MOTHER_ID, ADMIN_ID, "@mainchan")  # the bot is not an admin there yet
    assert not (await env.app.mainchan.config()).active
    assert has_text(env, MOTHER_ID, "ادمین", ADMIN_ID)

    env.fake.admin_in.add(CH)
    await env.say(MOTHER_ID, ADMIN_ID, "@mainchan")
    cfg = await env.app.mainchan.config()
    assert (cfg.chat_id, cfg.username, cfg.gate_children, cfg.gate_creators) == (CH, "mainchan", True, True)
    assert has_text(env, MOTHER_ID, "کانال اصلی روی", ADMIN_ID)

    await env.tap(MOTHER_ID, ADMIN_ID, pack("admmct", p="children"))
    await env.tap(MOTHER_ID, STRANGER, pack("admmct", p="creators"))  # not allowed
    cfg = await env.app.mainchan.config()
    assert (cfg.gate_children, cfg.gate_creators) == (False, True)

    # survives a restart (read back from the DB)
    env.app.mainchan._cfg = None
    assert (await env.app.mainchan.config()).chat_id == CH

    await env.tap(MOTHER_ID, ADMIN_ID, pack("admmcx"))
    assert (await env.app.mainchan.config()).active  # needs confirmation
    await env.tap(MOTHER_ID, ADMIN_ID, pack("admmcx", p="y"))
    assert not (await env.app.mainchan.config()).active


async def test_child_bot_users_must_join_first(env):
    await env.make_child(BOT, owner=OWNER)
    cid = await add_content(env)
    await define_channel(env)

    env.fake.clear()
    await env.say(BOT, USER, f"/start c_{cid}")
    assert not env.fake.find("SendAudio", BOT)
    prompt = env.fake.last("SendMessage", BOT)
    assert "کانال اصلی" in prompt.data["text"]
    btns = buttons(prompt)
    assert any(b.get("url") == "https://t.me/mainchan" for b in btns)
    check = next(b["callback_data"] for b in btns if b.get("callback_data", "").startswith("a:mchk"))
    assert check == pack("mchk", 0, f"c_{cid}")  # the deep link survives

    # buttons / free text are blocked too
    env.fake.clear()
    await env.say(BOT, USER, "song")
    await env.tap(BOT, USER, pack("list", 0, "c"))
    assert not env.fake.find("EditMessageText", BOT)

    # pressing "joined" without joining
    await env.tap(BOT, USER, check)
    assert not has_text(env, BOT, "تأیید شد", USER)

    join(env, USER)
    await env.tap(BOT, USER, check)  # forced re-check, no waiting for the cache
    done = env.fake.last("EditMessageText", BOT)
    assert "تأیید شد" in done.data["text"]
    assert any(b.get("url") == f"https://t.me/bot{BOT}_bot?start=c_{cid}" for b in buttons(done))

    env.fake.clear()
    await env.say(BOT, USER, f"/start c_{cid}")
    assert env.fake.find("SendAudio", BOT, chat_id=USER)


async def test_admins_are_exempt_and_the_gate_can_be_switched_off(env):
    await env.make_child(BOT, owner=OWNER)
    await define_channel(env)
    env.fake.clear()
    await env.say(BOT, ADMIN_ID, "/start")  # platform admin is never gated
    assert not has_text(env, BOT, "کانال اصلی", ADMIN_ID)

    await env.say(BOT, USER, "/start")
    assert has_text(env, BOT, "کانال اصلی", USER)
    await env.tap(MOTHER_ID, ADMIN_ID, pack("admmct", p="children"))
    env.fake.clear()
    await env.say(BOT, USER, "/start")
    assert not has_text(env, BOT, "کانال اصلی", USER)


async def test_creators_must_join_before_using_the_mother_bot(env):
    await define_channel(env)
    env.fake.clear()
    await env.say(MOTHER_ID, STRANGER, "/start")
    assert has_text(env, MOTHER_ID, "کانال اصلی", STRANGER) and not has_text(env, MOTHER_ID, "خوش آمدید", STRANGER)
    await env.tap(MOTHER_ID, STRANGER, pack("new"))
    assert not env.fake.find("EditMessageText", MOTHER_ID)

    join(env, STRANGER)
    await env.tap(MOTHER_ID, STRANGER, pack("mchk"))
    assert "تأیید شد" in env.fake.last("EditMessageText", MOTHER_ID).data["text"]
    env.fake.clear()
    await env.say(MOTHER_ID, STRANGER, "/start")
    assert has_text(env, MOTHER_ID, "خوش آمدید", STRANGER)

    # the admin keeps access without joining
    await env.say(MOTHER_ID, ADMIN_ID, "/start")
    assert has_text(env, MOTHER_ID, "خوش آمدید", ADMIN_ID)


async def test_unverifiable_membership_never_locks_anyone_out(env):
    await env.make_child(BOT, owner=OWNER)
    await define_channel(env)

    def boom(call):
        if call.data.get("user_id") == USER:
            return TelegramBadRequest(method=None, message="Bad Request: chat not found")  # type: ignore[arg-type]

    env.fake.fail["GetChatMember"] = boom
    env.fake.clear()
    await env.say(BOT, USER, "/start")
    assert not has_text(env, BOT, "کانال اصلی", USER)


async def test_membership_lookups_are_cached(env):
    await env.make_child(BOT, owner=OWNER)
    await define_channel(env)
    join(env, USER)
    env.fake.clear()
    for _ in range(5):
        await env.say(BOT, USER, "/start")
    lookups = [c for c in env.fake.find("GetChatMember") if c.data.get("user_id") == USER]
    assert len(lookups) == 1

    # someone who has not joined is re-checked only after a short pause, never per message
    env.fake.clear()
    for _ in range(4):
        await env.say(BOT, STRANGER, "hi")
    lookups = [c for c in env.fake.find("GetChatMember") if c.data.get("user_id") == STRANGER]
    assert len(lookups) == 1
