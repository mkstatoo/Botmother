"""Owner panel — categories, sponsors, plans, payment settings, settings, stats, broadcast, receipts."""

from __future__ import annotations

import logging
import re
from contextlib import suppress
from datetime import timedelta
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, MessageOriginChannel
from sqlalchemy import delete, func, select, update

from ..callbacks import A
from ..db.models import (
    BotUser,
    Category,
    ChildBot,
    Content,
    Delivery,
    Payment,
    Plan,
    Sponsor,
    Subscription,
)
from ..services.broadcast import run_broadcast
from ..services.payments import METHOD_NAME
from ..services.sponsors import SponsorError, normalize_chat_ref, resolve_channel
from ..ui import btn, kb, safe_delete, show
from ..util import (
    esc,
    fmt_int,
    fmt_money,
    human_duration,
    parse_cents,
    parse_int,
    utcnow,
)
from .common import IsOwner, price_text, trunc
from .states import BroadcastS, CategoryS, PaySet, PlanS, SettingS, SponsorS

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger(__name__)

router = Router(name="child_admin_manage")
router.message.filter(F.chat.type == ChatType.PRIVATE, IsOwner())
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE, IsOwner())

MAX_SPONSORS = 5
MAX_CATEGORIES = 30
AUTO_DELETE_CHOICES = (0, 30, 60, 300, 900, 3600)


async def _reload(app: App, child_id: int) -> ChildBot:
    async with app.db.session() as s:
        child = await s.get(ChildBot, child_id)
    assert child is not None
    return child


def _back(act: str = "panel", id: int = 0):
    return btn("🔙 بازگشت", act, id)


# ============================================================================= categories
async def render_cats(ui: Message | CallbackQuery, app: App, child: ChildBot) -> None:
    async with app.db.session() as s:
        cats = list((await s.scalars(select(Category).where(Category.bot_id == child.id).order_by(Category.id))).all())
    rows = [[btn(f"🗑 {trunc(c.name)}", "catdel", c.id)] for c in cats]
    rows.append([btn("➕ افزودن دسته", "catadd")])
    rows.append([_back()])
    text = "🗂 <b>دسته‌بندی‌ها</b>\nبرای حذف روی نام دسته بزنید." if cats else "🗂 هنوز دسته‌بندی‌ای ندارید."
    await show(ui, text, kb(*rows))


@router.callback_query(A.filter(F.act == "cats"))
async def cb_cats(callback: CallbackQuery, state: FSMContext, app: App, child: ChildBot) -> None:
    await state.clear()
    await render_cats(callback, app, child)
    await callback.answer()


@router.callback_query(A.filter(F.act == "catadd"))
async def cb_cat_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CategoryS.name)
    await show(callback, "🗂 نام دسته‌بندی جدید را بفرستید:", kb([btn("❌ انصراف", "cats")]))
    await callback.answer()


@router.message(CategoryS.name, F.text)
async def on_cat_name(message: Message, state: FSMContext, app: App, child: ChildBot) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 100:
        await message.answer("نام باید بین ۱ تا ۱۰۰ حرف باشد.")
        return
    async with app.db.session() as s:
        count = int(await s.scalar(select(func.count()).select_from(Category).where(Category.bot_id == child.id)) or 0)
        if count >= MAX_CATEGORIES:
            await message.answer(f"حداکثر {fmt_int(MAX_CATEGORIES)} دسته‌بندی مجاز است.")
            return
        s.add(Category(bot_id=child.id, name=name))
        await s.commit()
    await state.clear()
    await render_cats(message, app, child)


@router.callback_query(A.filter(F.act == "catdel"))
async def cb_cat_del(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    async with app.db.session() as s:
        await s.execute(update(Content).where(Content.bot_id == child.id, Content.category_id == callback_data.id).values(category_id=None))
        await s.execute(delete(Category).where(Category.id == callback_data.id, Category.bot_id == child.id))
        await s.commit()
    await callback.answer("حذف شد.")
    await render_cats(callback, app, child)


# ============================================================================= sponsors
async def render_sponsors(ui: Message | CallbackQuery, app: App, child: ChildBot) -> None:
    child = await _reload(app, child.id)
    async with app.db.session() as s:
        sponsors = list((await s.scalars(select(Sponsor).where(Sponsor.bot_id == child.id).order_by(Sponsor.id))).all())
    lines = [
        "📢 <b>کانال‌های اسپانسر</b>",
        "کاربر قبل از دریافت <b>محتوای رایگان</b> باید در همه این کانال‌ها عضو باشد (مشترکین معاف‌اند).",
        "",
        f"وضعیت شرط عضویت: {'✅ روشن' if child.sponsor_gate else '⛔️ خاموش'}",
    ]
    if sponsors:
        lines.append("")
        lines += [f"• {esc(sp.title)}" + (f" (@{esc(sp.username)})" if sp.username else "") for sp in sponsors]
    rows = [[btn(f"🗑 {trunc(sp.title, 30)}", "spdel", sp.id)] for sp in sponsors]
    if len(sponsors) < MAX_SPONSORS:
        rows.append([btn("➕ افزودن کانال", "spadd")])
    rows.append([btn("🔕 خاموش کردن شرط" if child.sponsor_gate else "🔔 روشن کردن شرط", "spgate")])
    rows.append([_back()])
    await show(ui, "\n".join(lines), kb(*rows))


@router.callback_query(A.filter(F.act == "sp"))
async def cb_sponsors(callback: CallbackQuery, state: FSMContext, app: App, child: ChildBot) -> None:
    await state.clear()
    await render_sponsors(callback, app, child)
    await callback.answer()


@router.callback_query(A.filter(F.act == "spadd"))
async def cb_sponsor_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SponsorS.target)
    await show(
        callback,
        "📢 <b>افزودن کانال اسپانسر</b>\n\n"
        "۱) ربات را <b>ادمین</b> آن کانال کنید (برای بررسی عضویت لازم است).\n"
        "۲) یک پیام از کانال را اینجا <b>فوروارد</b> کنید، یا آیدی/لینک کانال را بفرستید (مثل @mychannel).",
        kb([btn("❌ انصراف", "sp")]),
    )
    await callback.answer()


@router.message(SponsorS.target)
async def on_sponsor_target(message: Message, state: FSMContext, app: App, child: ChildBot, bot: Bot) -> None:
    ref: str | int | None = None
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        ref = origin.chat.id
    elif message.text:
        ref = normalize_chat_ref(message.text)
    if ref is None:
        await message.answer("❌ متوجه نشدم. پیامی از کانال فوروارد کنید یا آیدی کانال را بفرستید (مثل @mychannel).", reply_markup=kb([btn("❌ انصراف", "sp")]))
        return
    try:
        info = await resolve_channel(bot, ref)
    except SponsorError as exc:
        await message.answer(f"❌ {esc(exc)}", reply_markup=kb([btn("❌ انصراف", "sp")]))
        return
    async with app.db.session() as s:
        count = int(await s.scalar(select(func.count()).select_from(Sponsor).where(Sponsor.bot_id == child.id)) or 0)
        exists = await s.scalar(select(Sponsor.id).where(Sponsor.bot_id == child.id, Sponsor.chat_id == info.chat_id))
        if exists is not None:
            await message.answer("این کانال قبلاً اضافه شده است.")
            return
        if count >= MAX_SPONSORS:
            await message.answer(f"حداکثر {fmt_int(MAX_SPONSORS)} کانال اسپانسر مجاز است.")
            return
        s.add(Sponsor(bot_id=child.id, chat_id=info.chat_id, title=info.title, username=info.username, invite_link=info.invite_link))
        await s.commit()
    await state.clear()
    await render_sponsors(message, app, child)


@router.callback_query(A.filter(F.act == "spdel"))
async def cb_sponsor_del(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    async with app.db.session() as s:
        await s.execute(delete(Sponsor).where(Sponsor.id == callback_data.id, Sponsor.bot_id == child.id))
        await s.commit()
    await callback.answer("حذف شد.")
    await render_sponsors(callback, app, child)


@router.callback_query(A.filter(F.act == "spgate"))
async def cb_sponsor_gate(callback: CallbackQuery, app: App, child: ChildBot) -> None:
    async with app.db.session() as s:
        db_child = await s.get(ChildBot, child.id)
        assert db_child is not None
        db_child.sponsor_gate = not db_child.sponsor_gate
        await s.commit()
    await callback.answer("انجام شد.")
    await render_sponsors(callback, app, child)


# ============================================================================= plans
async def render_plans(ui: Message | CallbackQuery, app: App, child: ChildBot) -> None:
    async with app.db.session() as s:
        plans = list((await s.scalars(select(Plan).where(Plan.bot_id == child.id, Plan.is_active.is_(True)).order_by(Plan.id))).all())
    lines = ["💎 <b>پلن‌های اشتراک</b>", "مشترکین به همه محتواهای ویژه دسترسی دارند."]
    if plans:
        lines.append("")
        lines += [f"• {esc(p.name)} — {fmt_int(p.days)} روز — {price_text(p.price_irt, p.price_usd_cents) or '—'}" for p in plans]
    else:
        lines.append("\nهنوز پلنی تعریف نشده است.")
    rows = [[btn(f"🗑 {trunc(p.name, 30)}", "pldel", p.id)] for p in plans]
    rows.append([btn("➕ افزودن پلن", "pladd")])
    rows.append([_back("pp")])
    await show(ui, "\n".join(lines), kb(*rows))


@router.callback_query(A.filter(F.act == "pl"))
async def cb_plans(callback: CallbackQuery, state: FSMContext, app: App, child: ChildBot) -> None:
    await state.clear()
    await render_plans(callback, app, child)
    await callback.answer()


@router.callback_query(A.filter(F.act == "pladd"))
async def cb_plan_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PlanS.name)
    await show(callback, "💎 نام پلن را بفرستید (مثلاً «ماهانه»):", kb([btn("❌ انصراف", "pl")]))
    await callback.answer()


@router.message(PlanS.name, F.text)
async def on_plan_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 100:
        await message.answer("نام باید بین ۱ تا ۱۰۰ حرف باشد.")
        return
    await state.update_data(name=name)
    await state.set_state(PlanS.days)
    await message.answer("⏳ مدت اعتبار پلن را به <b>روز</b> بفرستید (مثلاً 30):")


@router.message(PlanS.days, F.text)
async def on_plan_days(message: Message, state: FSMContext) -> None:
    days = parse_int(message.text or "")
    if not days or days > 3650:
        await message.answer("❌ عددی بین ۱ تا ۳۶۵۰ بفرستید.")
        return
    await state.update_data(days=days)
    await state.set_state(PlanS.price_irt)
    await message.answer("💰 قیمت پلن به <b>تومان</b> (برای درگاه بانکی/واریز دستی). برای رد کردن 0 بفرستید:")


@router.message(PlanS.price_irt, F.text)
async def on_plan_price_irt(message: Message, state: FSMContext) -> None:
    n = parse_int(message.text or "")
    if n is None:
        await message.answer("❌ فقط عدد بفرستید.")
        return
    await state.update_data(price_irt=n or None)
    await state.set_state(PlanS.price_usd)
    await message.answer("🪙 قیمت پلن به <b>دلار</b> (برای ارز دیجیتال)، مثل 5 یا 2.5. برای رد کردن 0 بفرستید:")


@router.message(PlanS.price_usd, F.text)
async def on_plan_price_usd(message: Message, state: FSMContext, app: App, child: ChildBot) -> None:
    cents = parse_cents(message.text or "")
    if cents is None:
        await message.answer("❌ عدد معتبر بفرستید.")
        return
    data = await state.get_data()
    if not data.get("price_irt") and not cents:
        await message.answer("حداقل یکی از قیمت‌ها (تومان یا دلار) باید تعیین شود. قیمت دلار را دوباره بفرستید:")
        return
    async with app.db.session() as s:
        s.add(Plan(bot_id=child.id, name=data["name"], days=int(data["days"]), price_irt=data.get("price_irt"), price_usd_cents=cents or None))
        await s.commit()
    await state.clear()
    await render_plans(message, app, child)


@router.callback_query(A.filter(F.act == "pldel"))
async def cb_plan_del(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    # soft delete: existing subscribers keep their access
    async with app.db.session() as s:
        await s.execute(update(Plan).where(Plan.id == callback_data.id, Plan.bot_id == child.id).values(is_active=False))
        await s.commit()
    await callback.answer("حذف شد.")
    await render_plans(callback, app, child)


# ============================================================================= payment settings
async def render_pay_hub(ui: Message | CallbackQuery, app: App, child: ChildBot) -> None:
    child = await _reload(app, child.id)
    async with app.db.session() as s:
        pending = int(
            await s.scalar(select(func.count()).select_from(Payment).where(Payment.bot_id == child.id, Payment.status == "review")) or 0
        )
    ok = lambda v: "✅" if v else "❌"
    lines = [
        "💎 <b>پلن‌ها و پرداخت</b>",
        "",
        f"{ok(child.zarinpal_merchant_enc)} درگاه بانکی (زرین‌پال)",
        f"{ok(child.nowpayments_key_enc and child.nowpayments_ipn_enc)} ارز دیجیتال (NOWPayments)",
        f"{ok(child.card_number or child.manual_note)} واریز دستی (کارت به کارت / کیف پول)",
        "",
        "وجه پرداخت‌ها مستقیماً به حساب درگاه خودتان واریز می‌شود.",
    ]
    if not app.settings.payments_enabled:
        lines.append("\n⚠️ آدرس عمومی سرور (BASE_URL) تنظیم نشده؛ درگاه‌های آنلاین فعلاً غیرفعال‌اند و فقط واریز دستی کار می‌کند.")
    rows = [
        [btn("💎 پلن‌های اشتراک", "pl")],
        [btn("💳 زرین‌پال", "pz"), btn("🪙 ارز دیجیتال", "pn")],
        [btn("🧾 واریز دستی", "pm")],
        [btn(f"📥 رسیدهای در انتظار ({fmt_int(pending)})", "pend")],
        [_back()],
    ]
    await show(ui, "\n".join(lines), kb(*rows))


@router.callback_query(A.filter(F.act == "pp"))
async def cb_pay_hub(callback: CallbackQuery, state: FSMContext, app: App, child: ChildBot) -> None:
    await state.clear()
    await render_pay_hub(callback, app, child)
    await callback.answer()


async def _gateway_screen(ui, app, child, kind: str) -> None:
    child = await _reload(app, child.id)
    if kind == "pz":
        on = bool(child.zarinpal_merchant_enc)
        text = (
            "💳 <b>زرین‌پال</b>\n\n"
            f"وضعیت: {'✅ فعال' if on else '❌ تنظیم نشده'}\n\n"
            "مرچنت‌کد (۳۶ کاراکتری) را از پنل زرین‌پال خودتان بردارید. مبالغ به تومان تعریف می‌شوند."
        )
        rows = [[btn("✏️ ثبت / تغییر مرچنت‌کد", "pzs")]] + ([[btn("🗑 حذف", "pzc")]] if on else [])
    elif kind == "pn":
        on = bool(child.nowpayments_key_enc and child.nowpayments_ipn_enc)
        text = (
            "🪙 <b>ارز دیجیتال (NOWPayments)</b>\n\n"
            f"وضعیت: {'✅ فعال' if on else '❌ تنظیم نشده'}\n\n"
            "از پنل NOWPayments یک <b>API Key</b> و یک <b>IPN Secret</b> بسازید. قیمت این روش به دلار است."
        )
        rows = [[btn("✏️ ثبت / تغییر کلیدها", "pns")]] + ([[btn("🗑 حذف", "pnc")]] if on else [])
    else:
        on = bool(child.card_number or child.manual_note)
        text = (
            "🧾 <b>واریز دستی</b>\n\n"
            f"وضعیت: {'✅ فعال' if on else '❌ تنظیم نشده'}\n"
            + (f"💳 کارت: <code>{esc(child.card_number)}</code>\n" if child.card_number else "")
            + (f"👤 به نام: {esc(child.card_holder)}\n" if child.card_holder else "")
            + (f"📝 {esc(trunc(child.manual_note, 120))}\n" if child.manual_note else "")
            + "\nکاربر مبلغ را واریز و رسید را می‌فرستد؛ شما با یک دکمه تأیید یا رد می‌کنید."
        )
        rows = [[btn("✏️ ثبت / تغییر اطلاعات", "pms")]] + ([[btn("🗑 حذف", "pmc")]] if on else [])
    rows.append([_back("pp")])
    await show(ui, text, kb(*rows))


@router.callback_query(A.filter(F.act.in_({"pz", "pn", "pm"})))
async def cb_gateway(callback: CallbackQuery, callback_data: A, state: FSMContext, app: App, child: ChildBot) -> None:
    await state.clear()
    await _gateway_screen(callback, app, child, callback_data.act)
    await callback.answer()


async def _ask(callback: CallbackQuery, state: FSMContext, st, text: str, back: str, extra=None) -> None:
    await state.set_state(st)
    rows = ([extra] if extra else []) + [[btn("❌ انصراف", back)]]
    await show(callback, text, kb(*rows))
    await callback.answer()


@router.callback_query(A.filter(F.act == "pzs"))
async def cb_pzs(callback: CallbackQuery, state: FSMContext) -> None:
    await _ask(callback, state, PaySet.zarinpal, "💳 مرچنت‌کد زرین‌پال را بفرستید (پیام شما پس از دریافت حذف می‌شود):", "pz")


@router.message(PaySet.zarinpal, F.text)
async def on_zarinpal(message: Message, state: FSMContext, app: App, child: ChildBot) -> None:
    value = (message.text or "").strip()
    await safe_delete(message)
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", value):
        await message.answer("❌ مرچنت‌کد باید ۳۶ کاراکتر (حروف a-f، عدد و خط تیره) باشد. دوباره بفرستید:")
        return
    async with app.db.session() as s:
        await s.execute(update(ChildBot).where(ChildBot.id == child.id).values(zarinpal_merchant_enc=app.box.encrypt(value)))
        await s.commit()
    await state.clear()
    await _gateway_screen(message, app, child, "pz")


@router.callback_query(A.filter(F.act == "pzc"))
async def cb_pzc(callback: CallbackQuery, app: App, child: ChildBot) -> None:
    async with app.db.session() as s:
        await s.execute(update(ChildBot).where(ChildBot.id == child.id).values(zarinpal_merchant_enc=None))
        await s.commit()
    await callback.answer("حذف شد.")
    await _gateway_screen(callback, app, child, "pz")


@router.callback_query(A.filter(F.act == "pns"))
async def cb_pns(callback: CallbackQuery, state: FSMContext) -> None:
    await _ask(callback, state, PaySet.now_key, "🪙 <b>API Key</b> پنل NOWPayments را بفرستید (پیام حذف می‌شود):", "pn")


@router.message(PaySet.now_key, F.text)
async def on_now_key(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    await safe_delete(message)
    if len(value) < 8 or " " in value:
        await message.answer("❌ کلید معتبر نیست. دوباره بفرستید:")
        return
    await state.update_data(now_key=value)
    await state.set_state(PaySet.now_ipn)
    await message.answer("🔐 حالا <b>IPN Secret</b> را بفرستید (پیام حذف می‌شود):")


@router.message(PaySet.now_ipn, F.text)
async def on_now_ipn(message: Message, state: FSMContext, app: App, child: ChildBot) -> None:
    value = (message.text or "").strip()
    await safe_delete(message)
    if len(value) < 8 or " " in value:
        await message.answer("❌ مقدار معتبر نیست. دوباره بفرستید:")
        return
    data = await state.get_data()
    async with app.db.session() as s:
        await s.execute(
            update(ChildBot)
            .where(ChildBot.id == child.id)
            .values(nowpayments_key_enc=app.box.encrypt(data["now_key"]), nowpayments_ipn_enc=app.box.encrypt(value))
        )
        await s.commit()
    await state.clear()
    await _gateway_screen(message, app, child, "pn")


@router.callback_query(A.filter(F.act == "pnc"))
async def cb_pnc(callback: CallbackQuery, app: App, child: ChildBot) -> None:
    async with app.db.session() as s:
        await s.execute(update(ChildBot).where(ChildBot.id == child.id).values(nowpayments_key_enc=None, nowpayments_ipn_enc=None))
        await s.commit()
    await callback.answer("حذف شد.")
    await _gateway_screen(callback, app, child, "pn")


@router.callback_query(A.filter(F.act == "pms"))
async def cb_pms(callback: CallbackQuery, state: FSMContext) -> None:
    await _ask(
        callback, state, PaySet.card, "💳 شماره کارت ۱۶ رقمی را بفرستید.\nاگر فقط آدرس کیف پول/توضیحات می‌خواهید بدهید، دکمه زیر را بزنید.", "pm",
        extra=[btn("بدون کارت (فقط توضیحات)", "pmnocard")],
    )


@router.message(PaySet.card, F.text)
async def on_card(message: Message, state: FSMContext) -> None:
    digits = re.sub(r"\D", "", _fa_to_en(message.text or ""))
    if len(digits) != 16:
        await message.answer("❌ شماره کارت باید ۱۶ رقم باشد. دوباره بفرستید:")
        return
    await state.update_data(card=" ".join(digits[i : i + 4] for i in range(0, 16, 4)))
    await state.set_state(PaySet.card_holder)
    await message.answer("👤 نام صاحب کارت را بفرستید:")


def _fa_to_en(text: str) -> str:
    from ..util import to_en

    return to_en(text)


@router.callback_query(A.filter(F.act == "pmnocard"), PaySet.card)
async def cb_no_card(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(card=None, holder=None)
    await state.set_state(PaySet.note)
    await show(callback, "📝 توضیحات پرداخت (مثلاً آدرس کیف پول و شبکه) را بفرستید:", kb([btn("❌ انصراف", "pm")]))
    await callback.answer()


@router.message(PaySet.card_holder, F.text)
async def on_holder(message: Message, state: FSMContext) -> None:
    holder = (message.text or "").strip()[:100]
    if not holder:
        await message.answer("نام را بفرستید:")
        return
    await state.update_data(holder=holder)
    await state.set_state(PaySet.note)
    await message.answer("📝 توضیحات اضافه (اختیاری) را بفرستید، یا رد کنید:", reply_markup=kb([btn("⏭ رد کردن", "pmskip")]))


async def _save_manual(ui: Message | CallbackQuery, state: FSMContext, app: App, child: ChildBot, note: str | None) -> None:
    data = await state.get_data()
    if not data.get("card") and not note:
        await show(ui, "❌ حداقل شماره کارت یا توضیحات لازم است. توضیحات را بفرستید:", kb([btn("❌ انصراف", "pm")]))
        return
    async with app.db.session() as s:
        await s.execute(
            update(ChildBot).where(ChildBot.id == child.id).values(card_number=data.get("card"), card_holder=data.get("holder"), manual_note=note)
        )
        await s.commit()
    await state.clear()
    await _gateway_screen(ui, app, child, "pm")


@router.message(PaySet.note, F.text)
async def on_note(message: Message, state: FSMContext, app: App, child: ChildBot) -> None:
    await _save_manual(message, state, app, child, (message.text or "").strip()[:500] or None)


@router.callback_query(A.filter(F.act == "pmskip"), PaySet.note)
async def cb_note_skip(callback: CallbackQuery, state: FSMContext, app: App, child: ChildBot) -> None:
    await callback.answer()
    await _save_manual(callback, state, app, child, None)


@router.callback_query(A.filter(F.act == "pmc"))
async def cb_pmc(callback: CallbackQuery, app: App, child: ChildBot) -> None:
    async with app.db.session() as s:
        await s.execute(update(ChildBot).where(ChildBot.id == child.id).values(card_number=None, card_holder=None, manual_note=None))
        await s.commit()
    await callback.answer("حذف شد.")
    await _gateway_screen(callback, app, child, "pm")


# ============================================================================= manual receipts
@router.callback_query(A.filter(F.act == "mp"))
async def cb_review(callback: CallbackQuery, callback_data: A, app: App) -> None:
    owner_id = callback.from_user.id
    result = (
        await app.payments.approve(callback_data.id, owner_id)
        if callback_data.p == "ok"
        else await app.payments.reject(callback_data.id, owner_id)
    )
    text = {
        "ok": "✅ تأیید شد و برای کاربر ارسال گردید." if callback_data.p == "ok" else "❌ رد شد.",
        "already": "این پرداخت قبلاً بررسی شده است.",
        "forbidden": "اجازه این کار را ندارید.",
        "missing": "پرداخت پیدا نشد.",
    }[result]
    if result in ("ok", "already") and isinstance(callback.message, Message):
        with suppress(TelegramAPIError):
            await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer(text, show_alert=True)


@router.callback_query(A.filter(F.act == "pend"))
async def cb_pending(callback: CallbackQuery, app: App, child: ChildBot) -> None:
    async with app.db.session() as s:
        rows = list(
            (
                await s.scalars(
                    select(Payment).where(Payment.bot_id == child.id, Payment.status == "review").order_by(Payment.id.desc()).limit(20)
                )
            ).all()
        )
    if not rows:
        await show(callback, "📥 رسید در انتظاری وجود ندارد.", kb([_back("pp")]))
    else:
        buttons = [[btn(f"🧾 #{p.id} · {fmt_money(p.amount, p.currency)} · {METHOD_NAME.get(p.method, p.method)}", "pendv", p.id)] for p in rows]
        buttons.append([_back("pp")])
        await show(callback, "📥 <b>رسیدهای در انتظار</b>\nروی هر مورد بزنید تا برای بررسی دوباره ارسال شود.", kb(*buttons))
    await callback.answer()


@router.callback_query(A.filter(F.act == "pendv"))
async def cb_pending_view(callback: CallbackQuery, callback_data: A, app: App) -> None:
    await callback.answer()
    await app.payments.notify_review(callback_data.id)


# ============================================================================= settings
def _storage_label(value: str) -> str:
    return "🗄 کانال ذخیره‌ساز" if value == "channel" else "📡 تلگرام مستقیم"


async def render_settings(ui: Message | CallbackQuery, app: App, child: ChildBot) -> None:
    child = await _reload(app, child.id)
    lines = [
        "⚙️ <b>تنظیمات</b> @" + esc(child.username),
        "",
        f"📝 پیام خوش‌آمد: {'سفارشی' if child.welcome_text else 'پیش‌فرض'}",
        f"🗄 کانال ذخیره‌ساز: {'✅ تنظیم شده' if child.storage_channel_id else '❌ ندارد'}",
    ]
    if child.storage_channel_id:
        lines.append(f"📦 ذخیره‌سازی پیش‌فرض محتوای جدید: {_storage_label(child.default_storage)}")
    lines += [
        f"🔒 جلوگیری از فوروارد/ذخیره: {'روشن' if child.protect_content else 'خاموش'}",
        f"⏳ حذف خودکار فایل‌های ارسالی: {human_duration(child.auto_delete_seconds)}",
    ]
    rows = [
        [btn("📝 پیام خوش‌آمد", "stw"), btn("♻️ پیش‌فرض", "stwr")],
        [btn("🗄 کانال ذخیره‌ساز", "stc")] + ([btn("❌ حذف کانال", "stcc")] if child.storage_channel_id else []),
    ]
    if child.storage_channel_id:
        rows.append([btn(f"📦 پیش‌فرض: {_storage_label(child.default_storage)}", "std")])
    rows.append([btn(f"🔒 محافظت: {'روشن ←  خاموش کن' if child.protect_content else 'خاموش ← روشن کن'}", "stp")])
    rows.append([btn("⏳ حذف خودکار", "sta")])
    rows.append([_back()])
    await show(ui, "\n".join(lines), kb(*rows))


@router.callback_query(A.filter(F.act == "st"))
async def cb_settings(callback: CallbackQuery, state: FSMContext, app: App, child: ChildBot) -> None:
    await state.clear()
    await render_settings(callback, app, child)
    await callback.answer()


async def _update_child(app: App, child_id: int, **values) -> None:
    async with app.db.session() as s:
        await s.execute(update(ChildBot).where(ChildBot.id == child_id).values(**values))
        await s.commit()


@router.callback_query(A.filter(F.act == "stw"))
async def cb_welcome(callback: CallbackQuery, state: FSMContext) -> None:
    await _ask(callback, state, SettingS.welcome, "📝 متن پیام خوش‌آمد را بفرستید (حداکثر ۱۰۰۰ حرف):", "st")


@router.message(SettingS.welcome, F.text)
async def on_welcome(message: Message, state: FSMContext, app: App, child: ChildBot) -> None:
    text = (message.text or "").strip()
    if not text or len(text) > 1000:
        await message.answer("متن باید بین ۱ تا ۱۰۰۰ حرف باشد.")
        return
    await _update_child(app, child.id, welcome_text=text)
    await state.clear()
    await render_settings(message, app, child)


@router.callback_query(A.filter(F.act == "stwr"))
async def cb_welcome_reset(callback: CallbackQuery, app: App, child: ChildBot) -> None:
    await _update_child(app, child.id, welcome_text=None)
    await callback.answer("بازنشانی شد.")
    await render_settings(callback, app, child)


@router.callback_query(A.filter(F.act == "stc"))
async def cb_channel(callback: CallbackQuery, state: FSMContext) -> None:
    await _ask(
        callback, state, SettingS.channel,
        "🗄 <b>کانال ذخیره‌ساز</b>\n\nیک کانال <b>خصوصی</b> بسازید، ربات را با اجازه «ارسال پیام» ادمین کنید، سپس یک پیام از کانال را اینجا فوروارد کنید یا آیدی کانال را بفرستید.",
        "st",
    )


@router.message(SettingS.channel)
async def on_channel(message: Message, state: FSMContext, app: App, child: ChildBot, bot: Bot) -> None:
    ref: str | int | None = None
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        ref = origin.chat.id
    elif message.text:
        ref = normalize_chat_ref(message.text)
    if ref is None:
        await message.answer("❌ متوجه نشدم. پیامی از کانال فوروارد کنید یا آیدی را بفرستید.")
        return
    try:
        info = await resolve_channel(bot, ref)
        me = await bot.get_chat_member(info.chat_id, bot.id)
    except SponsorError as exc:
        await message.answer(f"❌ {esc(exc)}")
        return
    except TelegramAPIError as exc:
        await message.answer(f"❌ {esc(exc)}")
        return
    if me.status == ChatMemberStatus.ADMINISTRATOR and getattr(me, "can_post_messages", True) is False:
        await message.answer("❌ ربات اجازه «ارسال پیام» در این کانال را ندارد. اجازه را بدهید و دوباره امتحان کنید.")
        return
    await _update_child(app, child.id, storage_channel_id=info.chat_id, default_storage="channel")
    await state.clear()
    await render_settings(message, app, child)


@router.callback_query(A.filter(F.act == "stcc"))
async def cb_channel_clear(callback: CallbackQuery, app: App, child: ChildBot) -> None:
    await _update_child(app, child.id, storage_channel_id=None, default_storage="telegram")
    await callback.answer("حذف شد. محتواهای قبلی همچنان از کانال ارسال می‌شوند.", show_alert=True)
    await render_settings(callback, app, child)


@router.callback_query(A.filter(F.act == "std"))
async def cb_default_storage(callback: CallbackQuery, app: App, child: ChildBot) -> None:
    fresh = await _reload(app, child.id)
    await _update_child(app, child.id, default_storage="telegram" if fresh.default_storage == "channel" else "channel")
    await callback.answer("انجام شد.")
    await render_settings(callback, app, child)


@router.callback_query(A.filter(F.act == "stp"))
async def cb_protect(callback: CallbackQuery, app: App, child: ChildBot) -> None:
    fresh = await _reload(app, child.id)
    await _update_child(app, child.id, protect_content=not fresh.protect_content)
    await callback.answer("انجام شد.")
    await render_settings(callback, app, child)


@router.callback_query(A.filter(F.act == "sta"))
async def cb_auto_delete(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    if callback_data.p:
        seconds = int(callback_data.p) if callback_data.p.isdigit() else 0
        if seconds not in AUTO_DELETE_CHOICES:
            await callback.answer()
            return
        await _update_child(app, child.id, auto_delete_seconds=seconds)
        await callback.answer("ذخیره شد.")
        await render_settings(callback, app, child)
        return
    rows = [[btn(("🚫 غیرفعال" if sec == 0 else f"⏳ {human_duration(sec)}"), "sta", 0, str(sec))] for sec in AUTO_DELETE_CHOICES]
    rows.append([_back("st")])
    await show(callback, "⏳ فایل‌های ارسالی پس از چه مدتی از چت کاربر حذف شوند؟", kb(*rows))
    await callback.answer()


# ============================================================================= stats
async def _scalar(s, stmt) -> int:
    return int(await s.scalar(stmt) or 0)


async def render_stats(ui: Message | CallbackQuery, app: App, child: ChildBot) -> None:
    now = utcnow()
    b = child.id
    async with app.db.session() as s:
        users = await _scalar(s, select(func.count()).select_from(BotUser).where(BotUser.bot_id == b))
        new24 = await _scalar(s, select(func.count()).select_from(BotUser).where(BotUser.bot_id == b, BotUser.joined_at > now - timedelta(days=1)))
        new7 = await _scalar(s, select(func.count()).select_from(BotUser).where(BotUser.bot_id == b, BotUser.joined_at > now - timedelta(days=7)))
        active7 = await _scalar(s, select(func.count()).select_from(BotUser).where(BotUser.bot_id == b, BotUser.last_seen > now - timedelta(days=7)))
        blocked = await _scalar(s, select(func.count()).select_from(BotUser).where(BotUser.bot_id == b, BotUser.is_blocked.is_(True)))
        contents = await _scalar(s, select(func.count()).select_from(Content).where(Content.bot_id == b))
        premium = await _scalar(s, select(func.count()).select_from(Content).where(Content.bot_id == b, Content.access == "premium"))
        deliveries = await _scalar(s, select(func.count()).select_from(Delivery).where(Delivery.bot_id == b))
        deliveries7 = await _scalar(s, select(func.count()).select_from(Delivery).where(Delivery.bot_id == b, Delivery.created_at > now - timedelta(days=7)))
        subscribers = await _scalar(
            s, select(func.count(func.distinct(Subscription.user_id))).where(Subscription.bot_id == b, Subscription.expires_at > now)
        )
        revenue_rows = (
            await s.execute(
                select(Payment.currency, func.sum(Payment.amount), func.count())
                .where(Payment.bot_id == b, Payment.status == "paid")
                .group_by(Payment.currency)
            )
        ).all()
        revenue30_rows = (
            await s.execute(
                select(Payment.currency, func.sum(Payment.amount))
                .where(Payment.bot_id == b, Payment.status == "paid", Payment.paid_at > now - timedelta(days=30))
                .group_by(Payment.currency)
            )
        ).all()
        top = list(
            (await s.scalars(select(Content).where(Content.bot_id == b, Content.downloads > 0).order_by(Content.downloads.desc()).limit(5))).all()
        )

    lines = [
        "📊 <b>آمار</b> @" + esc(child.username),
        "",
        f"👥 کاربران: {fmt_int(users)} (۲۴ ساعت: {fmt_int(new24)} · ۷ روز: {fmt_int(new7)})",
        f"🟢 فعال در ۷ روز: {fmt_int(active7)} · 🚫 مسدودکننده: {fmt_int(blocked)}",
        f"🎞 محتوا: {fmt_int(contents)} (ویژه: {fmt_int(premium)})",
        f"📥 دریافت‌ها: {fmt_int(deliveries)} (۷ روز: {fmt_int(deliveries7)})",
        f"💎 مشترکین فعال: {fmt_int(subscribers)}",
    ]
    if revenue_rows:
        lines += ["", "💰 <b>فروش کل</b>"]
        lines += [f"• {fmt_money(int(total), cur)} ({fmt_int(cnt)} تراکنش)" for cur, total, cnt in revenue_rows]
        if revenue30_rows:
            lines.append("۳۰ روز اخیر: " + " + ".join(fmt_money(int(total), cur) for cur, total in revenue30_rows))
    if top:
        lines += ["", "🔥 <b>پردانلودترین‌ها</b>"]
        lines += [f"{i}. {esc(trunc(c.title, 35))} — {fmt_int(c.downloads)}" for i, c in enumerate(top, 1)]
    await show(ui, "\n".join(lines), kb([btn("🔄 به‌روزرسانی", "stat")], [_back()]))


@router.callback_query(A.filter(F.act == "stat"))
async def cb_stats(callback: CallbackQuery, app: App, child: ChildBot) -> None:
    await render_stats(callback, app, child)
    await callback.answer()


# ============================================================================= broadcast
@router.callback_query(A.filter(F.act == "bc"))
async def cb_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await _ask(callback, state, BroadcastS.message, "📨 پیامی را که می‌خواهید برای همه کاربران ارسال شود بفرستید (متن، عکس، فایل، …):", "panel")


@router.message(BroadcastS.message)
async def on_broadcast_message(message: Message, state: FSMContext, app: App, child: ChildBot) -> None:
    async with app.db.session() as s:
        total = await _scalar(
            s,
            select(func.count()).select_from(BotUser).where(BotUser.bot_id == child.id, BotUser.is_blocked.is_(False), BotUser.is_banned.is_(False)),
        )
    await state.set_state(BroadcastS.confirm)
    await state.update_data(chat_id=message.chat.id, message_id=message.message_id)
    await message.answer(f"این پیام برای {fmt_int(total)} کاربر ارسال شود؟", reply_markup=kb([btn("✅ ارسال", "bcy")], [btn("❌ انصراف", "panel")]))


@router.callback_query(A.filter(F.act == "bcy"), BroadcastS.confirm)
async def cb_broadcast_go(callback: CallbackQuery, state: FSMContext, app: App, child: ChildBot, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    await callback.answer("ارسال شروع شد.")
    await show(callback, "📨 ارسال آغاز شد؛ نتیجه را همین‌جا می‌فرستم.", kb([_back()]))
    owner_id = callback.from_user.id

    async def job() -> None:
        stats = await run_broadcast(app, child.id, int(data["chat_id"]), int(data["message_id"]))
        with suppress(TelegramAPIError):
            await bot.send_message(
                owner_id,
                f"📨 <b>پایان ارسال همگانی</b>\n✅ موفق: {fmt_int(stats.sent)}\n🚫 مسدودکننده: {fmt_int(stats.blocked)}\n⚠️ ناموفق: {fmt_int(stats.failed)}",
            )

    app.spawn(job())
