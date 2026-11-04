"""Guided quick-setup for a freshly created bot.

A creator with no technical background answers a handful of simple questions
(welcome text, do you sell?, card number, monthly price, sponsor channel) and
ends up with a configured bot. Every step is optional and saved immediately.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, MessageOriginChannel
from sqlalchemy import func, select, update

from ..callbacks import A
from ..db.models import ChildBot, Creator, Plan, Sponsor
from ..services.sponsors import SponsorError, normalize_chat_ref, resolve_channel
from ..ui import btn, kb, show, url_btn
from ..util import esc, fmt_toman, parse_int, to_en
from .router import _owned

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger(__name__)

router = Router(name="mother_wizard")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

MAX_WELCOME = 1000
MIN_PRICE, MAX_PRICE = 1_000, 100_000_000
NOT_COMMAND = ~F.text.startswith("/")


class Wizard(StatesGroup):
    welcome = State()
    card = State()
    holder = State()
    price = State()
    sponsor = State()


def panel_link(child: ChildBot) -> str:
    return f"https://t.me/{child.username}?start=panel"


def _cancel(child_id: int):
    return btn("⏹ ادامه بعداً", "wz", child_id, "done")


def _yes_no(child_id: int, yes: str, no: str):
    return kb([btn("✅ بله", "wz", child_id, yes), btn("❌ خیر", "wz", child_id, no)], [_cancel(child_id)])


def format_card(text: str) -> str | None:
    digits = re.sub(r"[\s\-]", "", to_en(text or ""))
    if not re.fullmatch(r"\d{16}", digits):
        return None
    return " ".join(digits[i : i + 4] for i in range(0, 16, 4))


# ----------------------------------------------------------------------------- steps
async def ask_welcome(event: Message | CallbackQuery, state: FSMContext, child: ChildBot) -> None:
    await state.set_state(Wizard.welcome)
    await state.update_data(bot_id=child.id)
    await show(
        event,
        "👋 <b>مرحله ۱ از ۴ — پیام خوش‌آمد</b>\n\n"
        f"وقتی کسی ربات @{esc(child.username)} را استارت می‌کند، چه پیامی ببیند؟\n"
        "مثلاً: «به کانال آموزش‌های صوتی من خوش آمدید 🎧»\n\n"
        "متن را بفرستید، یا «پیش‌فرض» را بزنید.",
        kb([btn("⏭ پیش‌فرض", "wz", child.id, "welcome_skip")], [_cancel(child.id)]),
    )


async def ask_sell(event: Message | CallbackQuery, state: FSMContext, child: ChildBot) -> None:
    await state.set_state(None)
    await show(
        event,
        "💰 <b>مرحله ۲ از ۴ — فروش</b>\n\n"
        "می‌خواهید محتوای پولی هم بفروشید؟\n"
        "(با «خیر» همه‌چیز رایگان است؛ بعداً هم می‌شود از پنل فعالش کرد.)",
        _yes_no(child.id, "sell_yes", "sell_no"),
    )


async def ask_card(event: Message | CallbackQuery, state: FSMContext, child: ChildBot) -> None:
    await state.set_state(Wizard.card)
    await state.update_data(bot_id=child.id)
    await show(
        event,
        "💳 <b>شماره کارت</b>\n\n"
        "مشتری‌ها پول را کارت‌به‌کارت به همین کارت واریز می‌کنند و رسید می‌فرستند؛ شما با یک دکمه تأیید می‌کنید.\n\n"
        "شماره‌ی <b>۱۶ رقمی</b> کارت را بفرستید:",
        kb([_cancel(child.id)]),
    )


async def ask_plan(event: Message | CallbackQuery, state: FSMContext, child: ChildBot) -> None:
    await state.set_state(None)
    await show(
        event,
        "🗓 <b>اشتراک ماهانه</b>\n\n"
        "یک اشتراک ۳۰ روزه بسازم که با آن همه‌ی محتوای ویژه باز شود؟",
        _yes_no(child.id, "plan_yes", "plan_no"),
    )


async def ask_sponsor(event: Message | CallbackQuery, state: FSMContext, child: ChildBot) -> None:
    await state.set_state(None)
    await show(
        event,
        "📢 <b>مرحله ۴ از ۴ — کانال اسپانسر</b>\n\n"
        "می‌خواهید کاربران برای دریافت محتوای رایگان، اول عضو کانالی شوند؟\n"
        "(برای معرفی کانال خودتان یا تبلیغ کانال‌های دیگر مفید است.)",
        _yes_no(child.id, "sp_yes", "sp_no"),
    )


async def ask_sponsor_channel(event: Message | CallbackQuery, state: FSMContext, child: ChildBot) -> None:
    await state.set_state(Wizard.sponsor)
    await state.update_data(bot_id=child.id)
    await show(
        event,
        "📢 <b>معرفی کانال</b>\n\n"
        "<b>اول</b> ربات خودتان (@" + esc(child.username) + ") را در آن کانال ادمین کنید:\n"
        "• وارد کانال شوید ← نام کانال ← Administrators ← Add Admin\n"
        f"• @{esc(child.username)} را جستجو و اضافه کنید ← تأیید (همان تنظیمات پیش‌فرض کافی است)\n\n"
        "<b>بعد</b> اینجا یکی از این‌ها را بفرستید:\n"
        "• آیدی کانال، مثل <code>@mychannel</code>\n"
        "• یا یک پیام از آن کانال را فوروارد کنید",
        kb([btn("⏭ رد شدن", "wz", child.id, "sp_no")], [_cancel(child.id)]),
    )


async def finish(event: Message | CallbackQuery, state: FSMContext, app: App, child: ChildBot) -> None:
    await state.clear()
    async with app.db.session() as s:
        child = await s.get(ChildBot, child.id) or child
        plans = int(await s.scalar(select(func.count()).select_from(Plan).where(Plan.bot_id == child.id, Plan.is_active.is_(True))) or 0)
        sponsors = int(await s.scalar(select(func.count()).select_from(Sponsor).where(Sponsor.bot_id == child.id)) or 0)
    tick = lambda ok: "✅" if ok else "▫️"
    text = (
        f"🎉 <b>ربات @{esc(child.username)} آماده است!</b>\n\n"
        f"{tick(child.welcome_text)} پیام خوش‌آمد\n"
        f"{tick(child.card_number)} پرداخت کارت‌به‌کارت\n"
        f"{tick(plans)} اشتراک ماهانه\n"
        f"{tick(sponsors)} کانال اسپانسر\n\n"
        "👇 <b>قدم بعدی:</b> «باز کردن پنل» را بزنید، سپس <b>هر فایل صوتی یا ویدئو</b> را برای ربات بفرستید تا به‌عنوان محتوا ثبت شود."
    )
    await show(
        event,
        text,
        kb(
            [url_btn("⚙️ باز کردن پنل ربات", panel_link(child))],
            [btn("🤖 ربات‌های من", "list"), btn("🏠 خانه", "home")],
        ),
    )


# ----------------------------------------------------------------------------- entry / callbacks
@router.callback_query(A.filter(F.act == "wz"))
async def cb_wizard(callback: CallbackQuery, callback_data: A, state: FSMContext, app: App, creator: Creator) -> None:
    child = await _owned(app, callback_data.id, creator.id)
    if child is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    step = callback_data.p

    if step == "start":
        await ask_welcome(callback, state, child)
    elif step == "welcome_skip":
        await ask_sell(callback, state, child)
    elif step == "sell_no":
        await ask_sponsor(callback, state, child)
    elif step == "sell_yes":
        await ask_card(callback, state, child)
    elif step == "plan_yes":
        await state.set_state(Wizard.price)
        await state.update_data(bot_id=child.id)
        await show(
            callback,
            "🗓 <b>قیمت اشتراک ماهانه</b>\n\nقیمت را به <b>تومان</b> بفرستید (مثلاً <code>150000</code>):",
            kb([_cancel(child.id)]),
        )
    elif step == "plan_no":
        await ask_sponsor(callback, state, child)
    elif step == "sp_yes":
        await ask_sponsor_channel(callback, state, child)
    elif step in ("sp_no", "done"):
        await finish(callback, state, app, child)
    await callback.answer()


async def _current(message: Message, state: FSMContext, app: App, creator: Creator) -> ChildBot | None:
    data = await state.get_data()
    child = await _owned(app, int(data.get("bot_id", 0)), creator.id)
    if child is None:
        await state.clear()
        await message.answer("نشست منقضی شد. از «🤖 ربات‌های من» دوباره شروع کنید.", reply_markup=kb([btn("🤖 ربات‌های من", "list")]))
    return child


@router.message(Wizard.welcome, F.text, NOT_COMMAND)
async def on_welcome(message: Message, state: FSMContext, app: App, creator: Creator) -> None:
    child = await _current(message, state, app, creator)
    if child is None:
        return
    text = (message.text or "").strip()
    if len(text) > MAX_WELCOME:
        await message.answer(f"❌ متن خیلی بلند است (حداکثر {MAX_WELCOME} حرف). کوتاه‌تر بفرستید.")
        return
    async with app.db.session() as s:
        await s.execute(update(ChildBot).where(ChildBot.id == child.id).values(welcome_text=text))
        await s.commit()
    await ask_sell(message, state, child)


@router.message(Wizard.card, F.text, NOT_COMMAND)
async def on_card(message: Message, state: FSMContext, app: App, creator: Creator) -> None:
    child = await _current(message, state, app, creator)
    if child is None:
        return
    card = format_card(message.text or "")
    if card is None:
        await message.answer("❌ شماره کارت باید ۱۶ رقم باشد. دوباره بفرستید (اعداد فارسی هم قبول است).", reply_markup=kb([_cancel(child.id)]))
        return
    await state.update_data(card=card)
    await state.set_state(Wizard.holder)
    await message.answer(
        f"✅ کارت <code>{card}</code> ثبت شد.\n\n<b>نام صاحب کارت</b> را بفرستید (به مشتری نشان داده می‌شود):",
        reply_markup=kb([_cancel(child.id)]),
    )


@router.message(Wizard.holder, F.text, NOT_COMMAND)
async def on_holder(message: Message, state: FSMContext, app: App, creator: Creator) -> None:
    child = await _current(message, state, app, creator)
    if child is None:
        return
    holder = (message.text or "").strip()
    if not 2 <= len(holder) <= 64:
        await message.answer("❌ نام باید بین ۲ تا ۶۴ حرف باشد.", reply_markup=kb([_cancel(child.id)]))
        return
    card = (await state.get_data()).get("card")
    async with app.db.session() as s:
        await s.execute(update(ChildBot).where(ChildBot.id == child.id).values(card_number=card, card_holder=holder))
        await s.commit()
    await message.answer("✅ اطلاعات کارت ذخیره شد.")
    await ask_plan(message, state, child)


@router.message(Wizard.price, F.text, NOT_COMMAND)
async def on_price(message: Message, state: FSMContext, app: App, creator: Creator) -> None:
    child = await _current(message, state, app, creator)
    if child is None:
        return
    price = parse_int(message.text or "")
    if price is None or not MIN_PRICE <= price <= MAX_PRICE:
        await message.answer(
            f"❌ یک عدد به تومان بفرستید (بین {fmt_toman(MIN_PRICE)} و {fmt_toman(MAX_PRICE)}).",
            reply_markup=kb([_cancel(child.id)]),
        )
        return
    async with app.db.session() as s:
        s.add(Plan(bot_id=child.id, name="اشتراک ماهانه", days=30, price_irt=price))
        await s.commit()
    await message.answer(f"✅ اشتراک ماهانه با قیمت {fmt_toman(price)} ساخته شد.")
    await ask_sponsor(message, state, child)


@router.message(Wizard.sponsor)
async def on_sponsor(message: Message, state: FSMContext, app: App, creator: Creator) -> None:
    if message.text and message.text.startswith("/"):
        return
    child = await _current(message, state, app, creator)
    if child is None:
        return
    ref: str | int | None = None
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        ref = origin.chat.id
    elif message.text:
        ref = normalize_chat_ref(message.text)
    retry = kb([btn("⏭ رد شدن", "wz", child.id, "sp_no")], [_cancel(child.id)])
    if ref is None:
        await message.answer("❌ متوجه نشدم. آیدی کانال را بفرستید (مثل <code>@mychannel</code>) یا یک پیام از آن را فوروارد کنید.", reply_markup=retry)
        return
    bot = app.manager.get_bot(child.id)
    if bot is None:
        await message.answer("ربات الان فعال نیست؛ این مرحله را بعداً از «پنل ← کانال‌های اسپانسر» انجام دهید.", reply_markup=retry)
        return
    try:
        info = await resolve_channel(bot, ref)
    except SponsorError as exc:
        await message.answer(
            f"❌ {esc(exc)}\n\nربات را در کانال ادمین کنید و دوباره آیدی را بفرستید.", reply_markup=retry
        )
        return
    async with app.db.session() as s:
        exists = await s.scalar(select(Sponsor.id).where(Sponsor.bot_id == child.id, Sponsor.chat_id == info.chat_id))
        if exists is None:
            s.add(Sponsor(bot_id=child.id, chat_id=info.chat_id, title=info.title, username=info.username, invite_link=info.invite_link))
            await s.commit()
    await message.answer(f"✅ کانال «{esc(info.title)}» به‌عنوان اسپانسر ثبت شد.")
    await finish(message, state, app, child)
