"""End-user experience of a generated bot: browse, search, get content, pay."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from ..callbacks import A
from ..db.models import BotUser, Category, ChildBot, Content, Order, Plan, Product, Purchase
from ..services.access import Decision, check_access, subscription_until
from ..services.delivery import KIND_ICON, ContentUnavailable, deliver
from ..services.payments import METHOD_LABEL, PaymentError, Target
from ..services.sponsors import join_url
from ..ui import btn, kb, safe_delete, show, url_btn
from ..util import esc, fmt_int, fmt_money, jdate
from .admin_content import PANEL_TEXT, panel_markup
from .common import PAGE, home_markup, price_text, trunc, welcome_text
from .states import Receipt

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger(__name__)

router = Router(name="child_user")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

# /start must work from inside any conversation state, so it lives on its own router
# that is attached before the owner routers (whose state handlers would swallow it).
start_router = Router(name="child_start")
start_router.message.filter(F.chat.type == ChatType.PRIVATE)


# ----------------------------------------------------------------------------- start / home
async def send_home(ui: Message | CallbackQuery, app: App, child: ChildBot, is_owner: bool) -> None:
    uid = ui.from_user.id if ui.from_user else None
    await show(ui, welcome_text(child), await home_markup(app, child, is_owner, uid))


@start_router.message(CommandStart(deep_link=True))
async def cmd_start_deep(
    message: Message, command: CommandObject, state: FSMContext, app: App, child: ChildBot, is_owner: bool
) -> None:
    await state.clear()
    payload = command.args or ""
    if payload.startswith("c_") and payload[2:].isdigit():
        await request_content(message, int(payload[2:]), app=app, child=child, is_owner=is_owner, user_id=message.from_user.id)  # type: ignore[union-attr]
    elif payload.startswith("s_") and payload[2:].isdigit() and child.shop_enabled:
        from .shop_user import show_product

        await show_product(message, app, child, int(payload[2:]), 0, is_owner)
    elif payload == "shop" and is_owner:
        from .shop_admin import menu_view

        text, markup = await menu_view(app, child)
        await message.answer(text, reply_markup=markup)
    elif payload == "panel" and is_owner:
        await message.answer(PANEL_TEXT.format(username=esc(child.username)), reply_markup=panel_markup())
    else:
        await send_home(message, app, child, is_owner)


@start_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, app: App, child: ChildBot, is_owner: bool) -> None:
    await state.clear()
    await send_home(message, app, child, is_owner)


@router.callback_query(A.filter(F.act == "home"))
async def cb_home(callback: CallbackQuery, state: FSMContext, app: App, child: ChildBot, is_owner: bool) -> None:
    await state.clear()
    await send_home(callback, app, child, is_owner)
    await callback.answer()


# ----------------------------------------------------------------------------- listing
@router.callback_query(A.filter(F.act == "list"))
async def cb_list(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    category_id, arg = callback_data.id, callback_data.p
    async with app.db.session() as s:
        cats = list((await s.scalars(select(Category).where(Category.bot_id == child.id).order_by(Category.id))).all())
        if arg == "c" and cats:
            rows = [[btn(f"🗂 {trunc(c.name)}", "list", c.id, "0")] for c in cats]
            rows.append([btn("📂 همه محتواها", "list", 0, "0")])
            rows.append([btn("🏠 منوی اصلی", "home")])
            await show(callback, "یک دسته‌بندی را انتخاب کنید:", kb(*rows))
            await callback.answer()
            return
        page = int(arg) if arg.isdigit() else 0
        cond = [Content.bot_id == child.id, Content.is_active.is_(True)]
        if category_id:
            cond.append(Content.category_id == category_id)
        total = int(await s.scalar(select(func.count()).select_from(Content).where(*cond)) or 0)
        items = list(
            (await s.scalars(select(Content).where(*cond).order_by(Content.id.desc()).offset(page * PAGE).limit(PAGE))).all()
        )

    if not items:
        await show(callback, "هنوز محتوایی اینجا نیست.", kb([btn("🏠 منوی اصلی", "home")]))
        await callback.answer()
        return
    rows = [
        [btn(f"{'💎' if c.access == 'premium' else KIND_ICON.get(c.kind, '📦')} {trunc(c.title)}", "get", c.id)] for c in items
    ]
    nav = []
    if page > 0:
        nav.append(btn("◀️ قبلی", "list", category_id, str(page - 1)))
    if (page + 1) * PAGE < total:
        nav.append(btn("بعدی ▶️", "list", category_id, str(page + 1)))
    if nav:
        rows.append(nav)
    rows.append([btn("🔙 دسته‌ها" if cats else "🏠 منوی اصلی", "list" if cats else "home", 0, "c")])
    await show(callback, f"📚 <b>محتواها</b> ({fmt_int(total)})", kb(*rows))
    await callback.answer()


# ----------------------------------------------------------------------------- content request
async def request_content(
    ui: Message | CallbackQuery,
    content_id: int,
    *,
    app: App,
    child: ChildBot,
    is_owner: bool,
    user_id: int,
    from_check: bool = False,
) -> None:
    bot: Bot = ui.bot  # type: ignore[assignment]
    async with app.db.session() as s:
        content = await s.get(Content, content_id)
        if content is None or content.bot_id != child.id or (not content.is_active and not is_owner):
            await show(ui, "❌ این محتوا در دسترس نیست.", kb([btn("🏠 منوی اصلی", "home")]))
            return
        result = await check_access(s, bot, child, user_id, content, is_owner=is_owner)
        plans = []
        if result.decision == Decision.PAYMENT:
            plans = list(
                (
                    await s.scalars(
                        select(Plan).where(Plan.bot_id == child.id, Plan.is_active.is_(True)).order_by(Plan.id)
                    )
                ).all()
            )

    if result.decision == Decision.OK:
        try:
            await deliver(app, bot, child, content, user_id, user_id)
        except ContentUnavailable:
            await bot.send_message(user_id, "⚠️ ارسال این فایل با مشکل روبه‌رو شد. لطفاً به مدیر ربات اطلاع دهید.")
            await app.notify_owner(child, f"⚠️ فایل «{esc(content.title)}» قابل ارسال نیست (حذف شده یا نامعتبر). آن را دوباره آپلود کنید.")
        except TelegramForbiddenError:
            return
        if from_check and isinstance(ui, CallbackQuery) and isinstance(ui.message, Message):
            await safe_delete(ui.message)
        return

    if result.decision == Decision.SPONSORS:
        lines = "\n".join(f"• {esc(sp.title)}" for sp in result.missing)
        rows = []
        for sp in result.missing:
            url = join_url(sp)
            if url:
                rows.append([url_btn(f"📢 {trunc(sp.title, 30)}", url)])
        rows.append([btn("✅ عضو شدم، بررسی کن", "chk", content.id)])
        await show(
            ui,
            f"🔒 برای دریافت «<b>{esc(content.title)}</b>» ابتدا در کانال‌های زیر عضو شوید، سپس دکمه بررسی را بزنید:\n\n{lines}",
            kb(*rows),
        )
        return

    # payment required
    rows = []
    lines = [f"💎 «<b>{esc(content.title)}</b>» محتوای ویژه است."]
    if app.payments.methods_for(child, content.price_irt, content.price_usd_cents):
        lines.append(f"\n🛒 قیمت این محتوا: {price_text(content.price_irt, content.price_usd_cents)}")
        rows.append([btn(f"🛒 خرید این محتوا — {price_text(content.price_irt, content.price_usd_cents)}", "buyc", content.id)])
    sellable = [p for p in plans if app.payments.methods_for(child, p.price_irt, p.price_usd_cents)]
    if sellable:
        lines.append("\n💎 یا با اشتراک به همه محتواهای ویژه دسترسی پیدا کنید:")
        for p in sellable:
            rows.append([btn(f"💎 {trunc(p.name, 24)} ({fmt_int(p.days)} روز) — {price_text(p.price_irt, p.price_usd_cents)}", "plan", p.id)])
    if not rows:
        lines.append("\nفعلاً روش پرداختی برای این محتوا فعال نیست؛ لطفاً به مدیر ربات اطلاع دهید.")
    rows.append([btn("🏠 منوی اصلی", "home")])
    await show(ui, "\n".join(lines), kb(*rows))


@router.callback_query(A.filter(F.act.in_({"get", "chk"})))
async def cb_get(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot, is_owner: bool) -> None:
    await callback.answer()
    await request_content(
        callback, callback_data.id, app=app, child=child, is_owner=is_owner,
        user_id=callback.from_user.id, from_check=callback_data.act == "chk",
    )


# ----------------------------------------------------------------------------- plans & buying
def _method_rows(app: App, child: ChildBot, price_irt, price_usd, kind: str, target_id: int, back: tuple[str, int]):
    methods = app.payments.methods_for(child, price_irt, price_usd)
    rows = [[btn(METHOD_LABEL[m], "pay", target_id, f"{kind}.{m}")] for m in methods]
    rows.append([btn("🔙 بازگشت", back[0], back[1])])
    return methods, rows


@router.callback_query(A.filter(F.act == "plans"))
async def cb_plans(callback: CallbackQuery, app: App, child: ChildBot) -> None:
    async with app.db.session() as s:
        plans = list((await s.scalars(select(Plan).where(Plan.bot_id == child.id, Plan.is_active.is_(True)).order_by(Plan.id))).all())
    sellable = [p for p in plans if app.payments.methods_for(child, p.price_irt, p.price_usd_cents)]
    if not sellable:
        await show(callback, "فعلاً پلن اشتراکی فعالی وجود ندارد.", kb([btn("🏠 منوی اصلی", "home")]))
    else:
        rows = [[btn(f"💎 {trunc(p.name, 24)} ({fmt_int(p.days)} روز) — {price_text(p.price_irt, p.price_usd_cents)}", "plan", p.id)] for p in sellable]
        rows.append([btn("🏠 منوی اصلی", "home")])
        await show(callback, "💎 <b>پلن‌های اشتراک</b>\nبا اشتراک، به همه محتواهای ویژه دسترسی دارید و نیازی به عضویت در کانال‌های اسپانسر نیست.", kb(*rows))
    await callback.answer()


@router.callback_query(A.filter(F.act == "plan"))
async def cb_plan(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    async with app.db.session() as s:
        plan = await s.get(Plan, callback_data.id)
    if plan is None or plan.bot_id != child.id or not plan.is_active:
        await callback.answer("این پلن در دسترس نیست.", show_alert=True)
        return
    methods, rows = _method_rows(app, child, plan.price_irt, plan.price_usd_cents, "p", plan.id, ("plans", 0))
    if not methods:
        await callback.answer("روش پرداختی برای این پلن فعال نیست.", show_alert=True)
        return
    await show(
        callback,
        f"💎 <b>{esc(plan.name)}</b>\n⏳ مدت: {fmt_int(plan.days)} روز\n💰 قیمت: {price_text(plan.price_irt, plan.price_usd_cents)}\n\nروش پرداخت را انتخاب کنید:",
        kb(*rows),
    )
    await callback.answer()


@router.callback_query(A.filter(F.act == "buyc"))
async def cb_buy_content(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    async with app.db.session() as s:
        content = await s.get(Content, callback_data.id)
    if content is None or content.bot_id != child.id or content.access != "premium":
        await callback.answer("این محتوا قابل خرید نیست.", show_alert=True)
        return
    methods, rows = _method_rows(app, child, content.price_irt, content.price_usd_cents, "c", content.id, ("get", content.id))
    if not methods:
        await callback.answer("روش پرداختی برای این محتوا فعال نیست.", show_alert=True)
        return
    await show(
        callback,
        f"🛒 <b>{esc(content.title)}</b>\n💰 قیمت: {price_text(content.price_irt, content.price_usd_cents)}\n\nروش پرداخت را انتخاب کنید:",
        kb(*rows),
    )
    await callback.answer()


@router.callback_query(A.filter(F.act == "pay"))
async def cb_pay(callback: CallbackQuery, callback_data: A, state: FSMContext, app: App, child: ChildBot) -> None:
    kind, _, method = callback_data.p.partition(".")
    user_id = callback.from_user.id
    async with app.db.session() as s:
        if kind == "c":
            obj = await s.get(Content, callback_data.id)
            valid = obj is not None and obj.bot_id == child.id and obj.access == "premium"
            target = Target("content", obj.id, obj.title, obj.price_irt, obj.price_usd_cents) if valid and obj else None
        elif kind == "p":
            obj = await s.get(Plan, callback_data.id)
            valid = obj is not None and obj.bot_id == child.id and obj.is_active
            target = Target("plan", obj.id, f"اشتراک {obj.name}", obj.price_irt, obj.price_usd_cents) if valid and obj else None
        elif kind == "o":
            obj = await s.get(Order, callback_data.id)
            valid = obj is not None and obj.bot_id == child.id and obj.user_id == user_id and obj.status == "pending"
            target = Target("order", obj.id, f"سفارش #{obj.id}", obj.total, None) if valid and obj else None
        else:
            target = None
    if target is None:
        await callback.answer("مورد انتخاب‌شده در دسترس نیست.", show_alert=True)
        return
    if method not in app.payments.methods_for(child, target.price_irt, target.price_usd_cents):
        await callback.answer("این روش پرداخت فعال نیست.", show_alert=True)
        return

    await callback.answer()
    try:
        payment = await app.payments.create(child, user_id, target, method)
        if method == "zarinpal":
            url = await app.payments.start_zarinpal(child, payment, f"{child.username} - {target.title}")
        elif method == "crypto":
            url = await app.payments.start_crypto(child, payment)
        else:
            await state.set_state(Receipt.waiting)
            await state.update_data(payment_id=payment.id)
            lines = [f"🧾 <b>پرداخت دستی — {esc(target.title)}</b>", f"💰 مبلغ: <b>{fmt_money(payment.amount, payment.currency)}</b>", ""]
            if child.card_number:
                lines.append(f"💳 شماره کارت: <code>{esc(child.card_number)}</code>")
            if child.card_holder:
                lines.append(f"👤 به نام: {esc(child.card_holder)}")
            if child.manual_note:
                lines += ["", esc(child.manual_note)]
            lines += ["", "پس از واریز، <b>عکس رسید</b> یا شماره پیگیری را همین‌جا بفرستید."]
            await show(callback, "\n".join(lines), kb([btn("❌ انصراف", "cancel")]))
            return
    except PaymentError as exc:
        await show(callback, f"❌ {esc(exc)}", kb([btn("🏠 منوی اصلی", "home")]))
        return

    await show(
        callback,
        f"💳 <b>{esc(target.title)}</b>\n💰 مبلغ: {fmt_money(payment.amount, payment.currency)}\n\n"
        "روی دکمه زیر بزنید و پرداخت را کامل کنید. بعد از پرداخت موفق، "
        + {"content": "فایل به‌صورت خودکار برای شما ارسال", "plan": "اشتراک به‌صورت خودکار برای شما فعال", "order": "سفارش شما ثبت و پیگیری آن از «سفارش‌های من» ممکن"}[target.kind]
        + " می‌شود.",
        kb([url_btn("💳 رفتن به صفحه پرداخت", url)], [btn("🏠 منوی اصلی", "home")]),
    )


@router.callback_query(A.filter(F.act == "cancel"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext, app: App, child: ChildBot, is_owner: bool) -> None:
    await state.clear()
    await send_home(callback, app, child, is_owner)
    await callback.answer()


@router.message(Receipt.waiting)
async def on_receipt(message: Message, state: FSMContext, app: App, child: ChildBot) -> None:
    data = await state.get_data()
    payment_id = int(data.get("payment_id", 0))
    file_id: str | None = None
    text: str | None = None
    if message.photo:
        file_id, text = message.photo[-1].file_id, message.caption
    elif message.text and not message.text.startswith("/"):
        text = message.text
    else:
        await message.answer("لطفاً عکس رسید یا شماره پیگیری را به‌صورت متن بفرستید.")
        return
    ok = await app.payments.submit_receipt(payment_id, message.from_user.id, file_id=file_id, text=(text or None))  # type: ignore[union-attr]
    await state.clear()
    if not ok:
        await message.answer("این پرداخت دیگر قابل ثبت رسید نیست.", reply_markup=await home_markup(app, child, False))
        return
    app.spawn(app.payments.notify_review(payment_id))
    await message.answer(
        "✅ رسید شما ثبت شد و برای مدیر ربات ارسال گردید. پس از تأیید، به‌صورت خودکار پیام می‌گیرید.",
        reply_markup=kb([btn("🏠 منوی اصلی", "home")]),
    )


# ----------------------------------------------------------------------------- account
@router.callback_query(A.filter(F.act == "me"))
async def cb_me(callback: CallbackQuery, app: App, child: ChildBot, bot_user: BotUser) -> None:
    uid = callback.from_user.id
    async with app.db.session() as s:
        until = await subscription_until(s, child.id, uid)
        bought = int(
            await s.scalar(select(func.count()).select_from(Purchase).where(Purchase.bot_id == child.id, Purchase.user_id == uid))
            or 0
        )
    sub = f"✅ فعال تا {jdate(until)}" if until else "ندارید"
    rows = [[btn("💎 تهیه اشتراک", "plans")]] if not until else []
    rows.append([btn("🏠 منوی اصلی", "home")])
    await show(
        callback,
        f"👤 <b>حساب من</b>\n\n🆔 <code>{uid}</code>\n💎 اشتراک: {sub}\n🛒 محتوای خریداری‌شده: {fmt_int(bought)}\n📅 عضویت از: {jdate(bot_user.joined_at)}",
        kb(*rows),
    )
    await callback.answer()


# ----------------------------------------------------------------------------- search
@router.message(F.text, ~F.text.startswith("/"), StateFilter(None))
async def on_search(message: Message, app: App, child: ChildBot) -> None:
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("برای جستجو حداقل ۲ حرف بنویسید.")
        return
    like = "%" + query.replace("%", "").replace("_", "") + "%"
    async with app.db.session() as s:
        items = list(
            (
                await s.scalars(
                    select(Content)
                    .where(Content.bot_id == child.id, Content.is_active.is_(True), Content.title.ilike(like))
                    .order_by(Content.id.desc())
                    .limit(10)
                )
            ).all()
        )
    products = []
    if child.shop_enabled:
        async with app.db.session() as s:
            products = (
                await s.execute(
                    select(Product.id, Product.title, Product.price_irt)
                    .where(Product.bot_id == child.id, Product.is_active.is_(True), Product.title.ilike(like))
                    .order_by(Product.id.desc())
                    .limit(10)
                )
            ).all()
    if not items and not products:
        await message.answer("🔎 نتیجه‌ای پیدا نشد.", reply_markup=kb([btn("🛍 فروشگاه" if child.shop_enabled else "📚 همه محتواها", "shop" if child.shop_enabled else "list", 0, "" if child.shop_enabled else "c")]))
        return
    rows = [[btn(f"🛍 {trunc(t, 26)} — {fmt_int(pr)}", "shp", pid, "0")] for pid, t, pr in products]
    rows += [[btn(f"{'💎' if c.access == 'premium' else KIND_ICON.get(c.kind, '📦')} {trunc(c.title)}", "get", c.id)] for c in items]
    rows.append([btn("🏠 منوی اصلی", "home")])
    await message.answer(f"🔎 {fmt_int(len(items) + len(products))} نتیجه:", reply_markup=kb(*rows))
