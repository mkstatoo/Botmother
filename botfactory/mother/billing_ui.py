"""Mother-bot UI for the platform service: creator renewal + platform-admin user management."""

from __future__ import annotations

import logging
import re
from contextlib import suppress
from datetime import timedelta
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, or_, select, update

from ..callbacks import A
from ..db.models import ChildBot, Creator, ServicePayment
from ..services.billing import KEYS, PERIOD_CHOICES, Config, describe
from ..services.payments import PaymentError
from ..ui import btn, kb, safe_delete, show, url_btn
from ..util import esc, fmt_int, fmt_toman, jdate, parse_int, to_en, utcnow
from .router import PAGE, STATUS_LABEL, _owned
from .wizard import format_card

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger(__name__)

creator_router = Router(name="mother-billing")
admin_router = Router(name="mother-admin")
for _r in (creator_router, admin_router):
    _r.message.filter(F.chat.type == ChatType.PRIVATE)
    _r.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


class ServiceReceipt(StatesGroup):
    receipt = State()


class AdminSetting(StatesGroup):
    value = State()


class AdminSearch(StatesGroup):
    query = State()


class AdminMessage(StatesGroup):
    text = State()


class AdminGift(StatesGroup):
    days = State()


DENIED = "دسترسی ندارید."


# ============================================================================= creator side
async def renew_menu(app: App, child: ChildBot):
    cfg = await app.billing.config()
    now = utcnow()
    head = f"💳 <b>تمدید سرویس @{esc(child.username)}</b>\n\n{describe(child, cfg, now)}\n\n"
    if not cfg.price_irt:
        return head + "هنوز قیمتی برای سرویس تعیین نشده؛ فعلاً نیازی به پرداخت نیست.", kb([btn("🔙 بازگشت", "mb", child.id)])
    if not app.billing.methods(cfg):
        return head + "فعلاً روش پرداختی برای تمدید فعال نشده است. لطفاً با مدیریت پلتفرم تماس بگیرید.", kb([btn("🔙 بازگشت", "mb", child.id)])
    head += f"هزینه هر {fmt_int(cfg.period_days)} روز: <b>{fmt_toman(cfg.price_irt)}</b>\nمدت تمدید را انتخاب کنید:"
    if cfg.note:
        head += f"\n\n{esc(cfg.note)}"
    rows = [[btn(f"{fmt_int(cfg.days_for(n))} روز — {fmt_toman(cfg.price_for(n))}", "svn", child.id, str(n))] for n in PERIOD_CHOICES]
    rows.append([btn("🔙 بازگشت", "mb", child.id)])
    return head, kb(*rows)


@creator_router.callback_query(A.filter(F.act == "sv"))
async def cb_renew(callback: CallbackQuery, callback_data: A, app: App, creator: Creator, state: FSMContext) -> None:
    child = await _owned(app, callback_data.id, creator.id)
    if child is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    await state.clear()
    text, markup = await renew_menu(app, child)
    await show(callback, text, markup)
    await callback.answer()


@creator_router.callback_query(A.filter(F.act == "svn"))
async def cb_renew_periods(callback: CallbackQuery, callback_data: A, app: App, creator: Creator) -> None:
    child = await _owned(app, callback_data.id, creator.id)
    periods = parse_int(callback_data.p)
    if child is None or periods not in PERIOD_CHOICES:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    cfg = await app.billing.config()
    methods = app.billing.methods(cfg)
    rows = []
    if "zarinpal" in methods:
        rows.append([btn("💳 پرداخت آنلاین (درگاه بانکی)", "svp", child.id, f"z{periods}")])
    if "manual" in methods:
        rows.append([btn("🧾 کارت به کارت", "svp", child.id, f"m{periods}")])
    if not rows:
        await callback.answer("روش پرداخت فعال نیست.", show_alert=True)
        return
    rows.append([btn("🔙 بازگشت", "sv", child.id)])
    await show(
        callback,
        f"تمدید @{esc(child.username)} برای {fmt_int(cfg.days_for(periods))} روز\nمبلغ: <b>{fmt_toman(cfg.price_for(periods))}</b>\nروش پرداخت را انتخاب کنید:",
        kb(*rows),
    )
    await callback.answer()


@creator_router.callback_query(A.filter(F.act == "svp"))
async def cb_renew_pay(callback: CallbackQuery, callback_data: A, app: App, creator: Creator, state: FSMContext) -> None:
    child = await _owned(app, callback_data.id, creator.id)
    m = re.fullmatch(r"([zm])(\d+)", callback_data.p)
    if child is None or m is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    method = "zarinpal" if m.group(1) == "z" else "manual"
    try:
        sp = await app.billing.create_payment(child, creator.id, int(m.group(2)), method)
        if method == "zarinpal":
            url = await app.billing.start_zarinpal(sp, f"تمدید سرویس ربات @{child.username}")
    except PaymentError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    if method == "zarinpal":
        await show(
            callback,
            f"مبلغ {fmt_toman(sp.amount)} برای {fmt_int(sp.days)} روز تمدید.\nبعد از پرداخت موفق، سرویس خودکار تمدید می‌شود.",
            kb([url_btn("💳 رفتن به درگاه پرداخت", url)], [btn("🔙 بازگشت", "sv", child.id)]),
        )
    else:
        cfg = await app.billing.config()
        await state.set_state(ServiceReceipt.receipt)
        await state.update_data(sp_id=sp.id)
        await show(
            callback,
            f"🧾 مبلغ <b>{fmt_toman(sp.amount)}</b> را به کارت زیر واریز کنید:\n\n"
            f"<code>{esc(cfg.card_number)}</code>\n"
            + (f"به نام: {esc(cfg.card_holder)}\n" if cfg.card_holder else "")
            + "\nسپس <b>عکس رسید</b> (یا شماره پیگیری) را همین‌جا بفرستید. بعد از تأیید مدیریت، سرویس تمدید می‌شود.",
            kb([btn("❌ انصراف", "sv", child.id)]),
        )
    await callback.answer()


@creator_router.message(ServiceReceipt.receipt, F.photo | F.text)
async def on_receipt(message: Message, state: FSMContext, app: App, creator: Creator) -> None:
    data = await state.get_data()
    sp_id = int(data.get("sp_id", 0))
    file_id = message.photo[-1].file_id if message.photo else None
    text = (message.caption or message.text or "").strip() or None
    if text and text.startswith("/"):
        return
    if not file_id and not text:
        await message.answer("عکس رسید یا شماره پیگیری را بفرستید.")
        return
    if not await app.billing.submit_receipt(sp_id, creator.id, file_id, text):
        await state.clear()
        await message.answer("این درخواست پرداخت دیگر معتبر نیست. از «تمدید سرویس» دوباره شروع کنید.", reply_markup=kb([btn("🤖 ربات‌های من", "list")]))
        return
    await state.clear()
    app.spawn(app.billing.notify_admins_receipt(sp_id))
    await message.answer("✅ رسید ثبت شد و برای مدیریت ارسال گردید. بعد از تأیید، سرویس تمدید و به شما اطلاع داده می‌شود.", reply_markup=kb([btn("🤖 ربات‌های من", "list")]))


# ============================================================================= admin: receipts
@admin_router.callback_query(A.filter(F.act.in_({"svok", "svno"})))
async def cb_receipt_decide(callback: CallbackQuery, callback_data: A, app: App, is_admin: bool) -> None:
    if not is_admin:
        await callback.answer(DENIED, show_alert=True)
        return
    result = await (app.billing.approve if callback_data.act == "svok" else app.billing.reject)(callback_data.id)
    with suppress(TelegramAPIError):
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer(result, show_alert=True)


@admin_router.callback_query(A.filter(F.act == "admrv"))
async def cb_receipts(callback: CallbackQuery, app: App, is_admin: bool) -> None:
    if not is_admin:
        await callback.answer(DENIED, show_alert=True)
        return
    async with app.db.session() as s:
        rows = list((await s.scalars(select(ServicePayment).where(ServicePayment.status == "review").order_by(ServicePayment.id).limit(20))).all())
    buttons = [[btn(f"#{sp.id} · {fmt_toman(sp.amount)} · {fmt_int(sp.days)} روز", "admrvi", sp.id)] for sp in rows]
    buttons.append([btn("🔙 بازگشت", "adm")])
    await show(callback, f"🧾 رسیدهای در انتظار بررسی: {fmt_int(len(rows))}", kb(*buttons))
    await callback.answer()


@admin_router.callback_query(A.filter(F.act == "admrvi"))
async def cb_receipt_view(callback: CallbackQuery, callback_data: A, app: App, is_admin: bool) -> None:
    if not is_admin:
        await callback.answer(DENIED, show_alert=True)
        return
    await app.billing.notify_admins_receipt(callback_data.id, only=callback.from_user.id)
    await callback.answer("رسید ارسال شد.")


@admin_router.callback_query(A.filter(F.act == "admpay"))
async def cb_payments_log(callback: CallbackQuery, app: App, is_admin: bool) -> None:
    if not is_admin:
        await callback.answer(DENIED, show_alert=True)
        return
    async with app.db.session() as s:
        rows = list((await s.scalars(select(ServicePayment).order_by(ServicePayment.id.desc()).limit(15))).all())
        names = {c.id: c.username for c in (await s.scalars(select(ChildBot).where(ChildBot.id.in_([r.bot_id for r in rows])))).all()} if rows else {}
    label = {"paid": "✅", "review": "🕓", "pending": "⌛️", "failed": "✖️", "rejected": "❌"}
    method = {"zarinpal": "درگاه", "manual": "کارت", "admin": "هدیه"}
    lines = [
        f"{label.get(r.status, r.status)} {jdate(r.created_at)} · @{esc(names.get(r.bot_id, r.bot_id))} · {fmt_int(r.days)}روز · {fmt_toman(r.amount)} · {method.get(r.method, r.method)}"
        for r in rows
    ]
    await show(callback, "💰 <b>آخرین پرداخت‌های سرویس</b>\n\n" + ("\n".join(lines) or "هنوز پرداختی ثبت نشده."), kb([btn("🔙 بازگشت", "adm")]))
    await callback.answer()


# ============================================================================= admin: settings
SETTING_LABELS = {
    "trial_days": "روز آزمایشی رایگان",
    "grace_days": "روز مهلت بعد از انقضا",
    "price_irt": "قیمت هر دوره (تومان)",
    "period_days": "طول هر دوره (روز)",
    "card_number": "شماره کارت دریافت وجه",
    "card_holder": "نام صاحب کارت",
    "zarinpal_merchant": "مرچنت زرین‌پال پلتفرم",
    "note": "توضیح نمایشی هنگام تمدید",
}


async def settings_view(app: App):
    cfg: Config = await app.billing.config()
    merchant = "✅ تنظیم شده" if cfg.zarinpal_merchant else "—"
    if cfg.zarinpal_merchant and not app.settings.payments_enabled:
        merchant += " (BASE_URL تنظیم نشده؛ پرداخت آنلاین غیرفعال است)"
    text = (
        "⚙️ <b>تنظیمات اشتراک پلتفرم</b>\n\n"
        f"وضعیت اعمال اشتراک: {'✅ فعال' if cfg.enabled else '⛔️ غیرفعال'}\n"
        f"🎁 دوره آزمایشی: {fmt_int(cfg.trial_days)} روز\n"
        f"⏳ مهلت بعد از انقضا: {fmt_int(cfg.grace_days)} روز\n"
        f"💵 قیمت هر دوره: {fmt_toman(cfg.price_irt) if cfg.price_irt else 'تعیین نشده'}\n"
        f"📆 طول هر دوره: {fmt_int(cfg.period_days)} روز\n"
        f"💳 کارت: {esc(cfg.card_number) or '—'} {('· ' + esc(cfg.card_holder)) if cfg.card_holder else ''}\n"
        f"🏦 زرین‌پال: {merchant}\n"
        f"📝 توضیح: {esc(cfg.note) or '—'}\n"
    )
    if not cfg.price_irt:
        text += "\n⚠️ تا وقتی قیمت تعیین نشود، هیچ رباتی قفل نمی‌شود."
    rows = [[btn("⛔️ غیرفعال کردن اشتراک" if cfg.enabled else "✅ فعال کردن اشتراک", "admt")]]
    keys = list(SETTING_LABELS)
    for i in range(0, len(keys), 2):
        rows.append([btn("✏️ " + SETTING_LABELS[k], "admse", 0, k) for k in keys[i : i + 2]])
    rows.append([btn("🔙 بازگشت", "adm")])
    return text, kb(*rows)


@admin_router.callback_query(A.filter(F.act == "admset"))
async def cb_settings(callback: CallbackQuery, app: App, is_admin: bool, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer(DENIED, show_alert=True)
        return
    await state.clear()
    text, markup = await settings_view(app)
    await show(callback, text, markup)
    await callback.answer()


@admin_router.callback_query(A.filter(F.act == "admt"))
async def cb_toggle(callback: CallbackQuery, app: App, is_admin: bool) -> None:
    if not is_admin:
        await callback.answer(DENIED, show_alert=True)
        return
    cfg = await app.billing.config()
    await app.billing.set("billing_enabled", "0" if cfg.enabled else "1")
    text, markup = await settings_view(app)
    await show(callback, text, markup)
    await callback.answer("انجام شد.")


@admin_router.callback_query(A.filter(F.act == "admse"))
async def cb_setting_edit(callback: CallbackQuery, callback_data: A, is_admin: bool, state: FSMContext) -> None:
    key = callback_data.p
    if not is_admin or key not in SETTING_LABELS:
        await callback.answer(DENIED, show_alert=True)
        return
    await state.set_state(AdminSetting.value)
    await state.update_data(key=key)
    hint = "عدد بفرستید." if key.endswith(("_days", "_irt")) else "متن را بفرستید."
    if key == "card_number":
        hint = "شماره کارت ۱۶ رقمی را بفرستید."
    if key != "price_irt" and key.endswith("_days"):
        hint += " (۰ هم مجاز است)" if key in ("trial_days", "grace_days") else ""
    hint += "\nبرای پاک‌کردن مقدار، «-» بفرستید." if key in ("card_number", "card_holder", "zarinpal_merchant", "note") else ""
    await show(callback, f"✏️ <b>{SETTING_LABELS[key]}</b>\n{hint}", kb([btn("❌ انصراف", "admset")]))
    await callback.answer()


@admin_router.message(AdminSetting.value, F.text)
async def on_setting_value(message: Message, state: FSMContext, app: App, is_admin: bool) -> None:
    if not is_admin:
        return
    key = (await state.get_data()).get("key", "")
    raw = (message.text or "").strip()
    if key not in SETTING_LABELS:
        await state.clear()
        return
    value: str
    if key in ("trial_days", "grace_days", "price_irt", "period_days"):
        n = parse_int(raw)
        if n is None or (key == "period_days" and n < 1) or n > 10_000_000_000:
            await message.answer("عدد معتبر بفرستید.", reply_markup=kb([btn("❌ انصراف", "admset")]))
            return
        value = str(n)
    elif raw == "-":
        value = ""
    elif key == "card_number":
        card = format_card(raw)
        if card is None:
            await message.answer("شماره کارت باید ۱۶ رقم باشد.", reply_markup=kb([btn("❌ انصراف", "admset")]))
            return
        value = card
    elif key == "zarinpal_merchant":
        merchant = to_en(raw).strip()
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", merchant):
            await message.answer("مرچنت زرین‌پال یک کد ۳۶ کاراکتری است. دوباره بفرستید.", reply_markup=kb([btn("❌ انصراف", "admset")]))
            return
        value = app.box.encrypt(merchant)
        await safe_delete(message)  # do not leave the secret in the chat
    else:
        value = raw[:300]
    await app.billing.set(key, value)
    await state.clear()
    text, markup = await settings_view(app)
    await message.answer("✅ ذخیره شد.\n\n" + text, reply_markup=markup)


# ============================================================================= admin: creators
def _creator_label(c: Creator, bots: int) -> str:
    who = f"@{c.username}" if c.username else (c.full_name or str(c.id))
    return f"{'🚫 ' if c.is_banned else ''}{who} · 🤖{bots}"


async def _bot_counts_by_owner(app: App, owner_ids: list[int]) -> dict[int, int]:
    if not owner_ids:
        return {}
    async with app.db.session() as s:
        rows = (await s.execute(select(ChildBot.owner_id, func.count()).where(ChildBot.owner_id.in_(owner_ids)).group_by(ChildBot.owner_id))).all()
    return {o: n for o, n in rows}


@admin_router.callback_query(A.filter(F.act == "admc"))
async def cb_creators(callback: CallbackQuery, callback_data: A, app: App, is_admin: bool, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer(DENIED, show_alert=True)
        return
    await state.clear()
    page = callback_data.id
    async with app.db.session() as s:
        total = int(await s.scalar(select(func.count()).select_from(Creator)) or 0)
        rows = list((await s.scalars(select(Creator).order_by(Creator.created_at.desc()).offset(page * PAGE).limit(PAGE))).all())
    counts = await _bot_counts_by_owner(app, [c.id for c in rows])
    buttons = [[btn(_creator_label(c, counts.get(c.id, 0)), "admcd", c.id)] for c in rows]
    nav = []
    if page > 0:
        nav.append(btn("◀️ قبلی", "admc", page - 1))
    if (page + 1) * PAGE < total:
        nav.append(btn("بعدی ▶️", "admc", page + 1))
    if nav:
        buttons.append(nav)
    buttons.append([btn("🔎 جستجو", "admq")])
    buttons.append([btn("🔙 بازگشت", "adm")])
    await show(callback, f"👤 <b>سازندگان</b> ({fmt_int(total)})", kb(*buttons))
    await callback.answer()


@admin_router.callback_query(A.filter(F.act == "admq"))
async def cb_search_ask(callback: CallbackQuery, is_admin: bool, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer(DENIED, show_alert=True)
        return
    await state.set_state(AdminSearch.query)
    await show(callback, "🔎 شناسه عددی، @یوزرنیم یا بخشی از نام سازنده (یا یوزرنیم ربات) را بفرستید.", kb([btn("❌ انصراف", "admc", 0)]))
    await callback.answer()


@admin_router.message(AdminSearch.query, F.text)
async def on_search(message: Message, state: FSMContext, app: App, is_admin: bool) -> None:
    if not is_admin:
        return
    q = to_en((message.text or "").strip()).lstrip("@")
    if not q:
        return
    async with app.db.session() as s:
        cond = [Creator.username.ilike(f"%{q}%"), Creator.full_name.ilike(f"%{q}%")]
        if q.isdigit():
            cond.append(Creator.id == int(q))
        bot_owner = select(ChildBot.owner_id).where(ChildBot.username.ilike(f"%{q}%"))
        cond.append(Creator.id.in_(bot_owner))
        rows = list((await s.scalars(select(Creator).where(or_(*cond)).order_by(Creator.created_at.desc()).limit(PAGE))).all())
    await state.clear()
    counts = await _bot_counts_by_owner(app, [c.id for c in rows])
    buttons = [[btn(_creator_label(c, counts.get(c.id, 0)), "admcd", c.id)] for c in rows]
    buttons.append([btn("🔎 جستجوی دیگر", "admq")])
    buttons.append([btn("🔙 بازگشت", "adm")])
    await message.answer(f"نتیجه جستجو: {fmt_int(len(rows))} مورد", reply_markup=kb(*buttons))


async def creator_view(app: App, creator_id: int):
    cfg = await app.billing.config()
    now = utcnow()
    async with app.db.session() as s:
        c = await s.get(Creator, creator_id)
        bots = list((await s.scalars(select(ChildBot).where(ChildBot.owner_id == creator_id).order_by(ChildBot.created_at))).all()) if c else []
        paid = int(
            await s.scalar(
                select(func.coalesce(func.sum(ServicePayment.amount), 0)).where(
                    ServicePayment.creator_id == creator_id, ServicePayment.status == "paid", ServicePayment.method != "admin"
                )
            )
            or 0
        )
    if c is None:
        return None
    lines = [
        f"👤 <b>{esc(c.full_name) or '—'}</b> {('@' + esc(c.username)) if c.username else ''}",
        f"شناسه: <code>{c.id}</code>",
        f"عضویت: {jdate(c.created_at)}",
        f"وضعیت: {'🚫 مسدود' if c.is_banned else '✅ عادی'}",
        f"💰 مجموع پرداخت سرویس: {fmt_toman(paid)}",
        "",
        f"🤖 ربات‌ها ({fmt_int(len(bots))}):",
    ]
    for b in bots:
        lines.append(f"• @{esc(b.username)} — {STATUS_LABEL.get(b.status, b.status)}\n   {describe(b, cfg, now)}")
    rows = [[btn(f"🤖 @{b.username}", "admb", b.id)] for b in bots]
    rows.append([btn("✅ رفع مسدودی" if c.is_banned else "🚫 مسدود کردن", "admun" if c.is_banned else "admban", c.id)])
    rows.append([btn("✉️ ارسال پیام", "admmsg", c.id)])
    rows.append([btn("🔙 بازگشت", "admc", 0)])
    return "\n".join(lines), kb(*rows)


@admin_router.callback_query(A.filter(F.act == "admcd"))
async def cb_creator_detail(callback: CallbackQuery, callback_data: A, app: App, is_admin: bool, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer(DENIED, show_alert=True)
        return
    await state.clear()
    view = await creator_view(app, callback_data.id)
    if view is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    await show(callback, *view)
    await callback.answer()


@admin_router.callback_query(A.filter(F.act.in_({"admban", "admun"})))
async def cb_ban(callback: CallbackQuery, callback_data: A, app: App, is_admin: bool) -> None:
    if not is_admin:
        await callback.answer(DENIED, show_alert=True)
        return
    cid = callback_data.id
    if cid in app.settings.admin_ids:
        await callback.answer("مدیر پلتفرم را نمی‌توان مسدود کرد.", show_alert=True)
        return
    ban = callback_data.act == "admban"
    async with app.db.session() as s:
        await s.execute(update(Creator).where(Creator.id == cid).values(is_banned=ban))
        if ban:
            await s.execute(update(ChildBot).where(ChildBot.owner_id == cid, ChildBot.status.in_(("active", "paused", "expired"))).values(status="suspended"))
        else:
            await s.execute(update(ChildBot).where(ChildBot.owner_id == cid, ChildBot.status == "suspended").values(status="active"))
        await s.commit()
    view = await creator_view(app, cid)
    if view is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    await show(callback, *view)
    await callback.answer("مسدود شد؛ رباتهایش هم متوقف شدند." if ban else "رفع مسدودی شد.", show_alert=True)


@admin_router.callback_query(A.filter(F.act == "admmsg"))
async def cb_msg_ask(callback: CallbackQuery, callback_data: A, is_admin: bool, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer(DENIED, show_alert=True)
        return
    await state.set_state(AdminMessage.text)
    await state.update_data(cid=callback_data.id)
    await show(callback, "✉️ پیام خود را برای این سازنده بفرستید (هر نوع پیام).", kb([btn("❌ انصراف", "admcd", callback_data.id)]))
    await callback.answer()


@admin_router.message(AdminMessage.text)
async def on_msg(message: Message, state: FSMContext, app: App, is_admin: bool) -> None:
    if not is_admin:
        return
    cid = int((await state.get_data()).get("cid", 0))
    await state.clear()
    bot = app.mother_bot
    ok = False
    if bot is not None:
        try:
            await bot.copy_message(cid, message.chat.id, message.message_id)
            ok = True
        except TelegramAPIError as exc:
            log.warning("admin message to %s failed: %s", cid, exc)
    await message.answer("✅ ارسال شد." if ok else "❌ ارسال نشد (کاربر ربات را بلاک کرده یا شناسه نامعتبر است).", reply_markup=kb([btn("👤 پروفایل", "admcd", cid)], [btn("🔙 مدیریت", "adm")]))


# ============================================================================= admin: bot service actions
async def _after_service_change(callback: CallbackQuery, app: App, bot_id: int, note: str) -> None:
    from .router import cb_admin_bot

    await callback.answer(note, show_alert=True)
    await cb_admin_bot(callback, A(act="admb", id=bot_id), True, app)


@admin_router.callback_query(A.filter(F.act == "admg"))
async def cb_gift(callback: CallbackQuery, callback_data: A, app: App, is_admin: bool) -> None:
    days = parse_int(callback_data.p)
    if not is_admin or not days:
        await callback.answer(DENIED, show_alert=True)
        return
    child = await app.billing.grant(callback_data.id, days, callback.from_user.id)
    if child is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    assert child.service_until is not None
    await app.notify_creator(child.owner_id, f"🎁 مدیریت پلتفرم {fmt_int(days)} روز سرویس رایگان به ربات @{esc(child.username)} هدیه داد.\nپایان سرویس: {jdate(child.service_until)}")
    await _after_service_change(callback, app, child.id, f"{fmt_int(days)} روز اضافه شد.")


@admin_router.callback_query(A.filter(F.act == "admgc"))
async def cb_gift_custom(callback: CallbackQuery, callback_data: A, is_admin: bool, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer(DENIED, show_alert=True)
        return
    await state.set_state(AdminGift.days)
    await state.update_data(bot_id=callback_data.id)
    await show(callback, "تعداد روز هدیه را بفرستید (عدد).", kb([btn("❌ انصراف", "admb", callback_data.id)]))
    await callback.answer()


@admin_router.message(AdminGift.days, F.text)
async def on_gift_days(message: Message, state: FSMContext, app: App, is_admin: bool) -> None:
    if not is_admin:
        return
    days = parse_int(message.text or "")
    bot_id = int((await state.get_data()).get("bot_id", 0))
    if not days or days > 3650:
        await message.answer("عدد معتبر (۱ تا ۳۶۵۰) بفرستید.", reply_markup=kb([btn("❌ انصراف", "admb", bot_id)]))
        return
    await state.clear()
    child = await app.billing.grant(bot_id, days, message.from_user.id)  # type: ignore[union-attr]
    if child is None:
        await message.answer("ربات پیدا نشد.")
        return
    assert child.service_until is not None
    await app.notify_creator(child.owner_id, f"🎁 مدیریت پلتفرم {fmt_int(days)} روز سرویس رایگان به ربات @{esc(child.username)} هدیه داد.\nپایان سرویس: {jdate(child.service_until)}")
    await message.answer(f"✅ {fmt_int(days)} روز اضافه شد.", reply_markup=kb([btn("🤖 ربات", "admb", bot_id)]))


@admin_router.callback_query(A.filter(F.act.in_({"admgu", "admge"})))
async def cb_unlimited_or_end(callback: CallbackQuery, callback_data: A, app: App, is_admin: bool) -> None:
    if not is_admin:
        await callback.answer(DENIED, show_alert=True)
        return
    if callback_data.act == "admgu":
        child = await app.billing.set_unlimited(callback_data.id)
        note = "سرویس نامحدود شد."
    else:
        child = await app.billing.expire_now(callback_data.id)
        note = "سرویس پایان یافت."
    if child is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    await _after_service_change(callback, app, child.id, note)


@admin_router.message(Command("user"))
async def cmd_user(message: Message, app: App, is_admin: bool) -> None:
    """/user <id | @username> — jump straight to a creator."""
    if not is_admin:
        return
    q = to_en((message.text or "").split(maxsplit=1)[1] if len((message.text or "").split(maxsplit=1)) > 1 else "").strip().lstrip("@")
    if not q:
        await message.answer("استفاده: <code>/user شناسه یا @یوزرنیم</code>")
        return
    async with app.db.session() as s:
        cond = [Creator.username.ilike(q)]
        if q.isdigit():
            cond.append(Creator.id == int(q))
        c = await s.scalar(select(Creator).where(or_(*cond)))
    view = await creator_view(app, c.id) if c else None
    if view is None:
        await message.answer("سازنده‌ای پیدا نشد.")
        return
    await message.answer(*view[:1], reply_markup=view[1])
