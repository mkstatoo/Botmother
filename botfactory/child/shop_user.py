"""Shop — buyer side: browse, product page, cart, checkout, my orders."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from ..callbacks import A
from ..db.models import ChildBot, Order, Product, ShopCategory
from ..services.payments import METHOD_LABEL
from ..services.shop import MAX_QTY, ShopError, order_text, price_line, quote, stock_text
from ..ui import btn, kb, safe_delete, show
from ..util import esc, fmt_int, fmt_toman, to_en
from .common import PAGE, trunc
from .states import Checkout

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger(__name__)

router = Router(name="child_shop_user")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

HOME = [btn("🏠 منوی اصلی", "home")]


def _guard(child: ChildBot) -> bool:
    return bool(child.shop_enabled)


# ----------------------------------------------------------------------------- browsing
@router.callback_query(A.filter(F.act == "shop"))
async def cb_shop(callback: CallbackQuery, state: FSMContext, app: App, child: ChildBot) -> None:
    await state.clear()
    if not _guard(child):
        await callback.answer("فروشگاه فعال نیست.", show_alert=True)
        return
    async with app.db.session() as s:
        cats = list((await s.scalars(select(ShopCategory).where(ShopCategory.bot_id == child.id).order_by(ShopCategory.id))).all())
    if not cats:
        await _list_products(callback, app, child, 0, 0)
        return
    rows = [[btn(f"🗂 {trunc(c.name)}", "shl", c.id, "0")] for c in cats]
    rows.append([btn("📂 همه کالاها", "shl", 0, "0")])
    rows.append(HOME)
    await show(callback, "🛍 <b>فروشگاه</b>\nیک دسته را انتخاب کنید:", kb(*rows))
    await callback.answer()


async def _list_products(callback: CallbackQuery, app: App, child: ChildBot, category_id: int, page: int) -> None:
    cond = [Product.bot_id == child.id, Product.is_active.is_(True)]
    if category_id:
        cond.append(Product.category_id == category_id)
    async with app.db.session() as s:
        total = int(await s.scalar(select(func.count()).select_from(Product).where(*cond)) or 0)
        items = (
            await s.execute(
                select(Product.id, Product.title, Product.price_irt, Product.stock)
                .where(*cond)
                .order_by(Product.id.desc())
                .offset(page * PAGE)
                .limit(PAGE)
            )
        ).all()
        has_cats = bool(await s.scalar(select(ShopCategory.id).where(ShopCategory.bot_id == child.id).limit(1)))
    if not items:
        await show(callback, "هنوز کالایی در این بخش نیست.", kb([btn("🔙 بازگشت", "shop")] if has_cats else HOME))
        await callback.answer()
        return
    rows = [
        [btn(f"{'❌ ' if (stock is not None and stock <= 0) else ''}{trunc(title, 28)} — {fmt_int(price)}", "shp", pid, str(category_id))]
        for pid, title, price, stock in items
    ]
    nav = []
    if page > 0:
        nav.append(btn("◀️ قبلی", "shl", category_id, str(page - 1)))
    if (page + 1) * PAGE < total:
        nav.append(btn("بعدی ▶️", "shl", category_id, str(page + 1)))
    if nav:
        rows.append(nav)
    rows.append([btn("🛒 سبد خرید", "shcart"), btn("🔙 دسته‌ها" if has_cats else "🏠 منوی اصلی", "shop" if has_cats else "home")])
    await show(callback, f"🛍 <b>کالاها</b> ({fmt_int(total)}) — قیمت‌ها به تومان", kb(*rows))
    await callback.answer()


@router.callback_query(A.filter(F.act == "shl"))
async def cb_products(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    if not _guard(child):
        await callback.answer("فروشگاه فعال نیست.", show_alert=True)
        return
    page = int(callback_data.p) if callback_data.p.isdigit() else 0
    await _list_products(callback, app, child, callback_data.id, page)


async def show_product(ui: Message | CallbackQuery, app: App, child: ChildBot, product_id: int, back: int = 0, is_owner: bool = False) -> None:
    async with app.db.session() as s:
        p = await s.get(Product, product_id)
    if p is None or p.bot_id != child.id or (not p.is_active and not is_owner) or not child.shop_enabled:
        await show(ui, "❌ این کالا در دسترس نیست.", kb(HOME))
        return
    desc = (p.description or "").strip()
    text = f"🛍 <b>{esc(p.title)}</b>\n\n" + (esc(desc[:700]) + ("…" if len(desc) > 700 else "") + "\n\n" if desc else "") + price_line(p)
    note = stock_text(p.stock)
    if note:
        text += f"\n{note}"
    if not p.needs_shipping:
        text += "\n📥 کالای دیجیتال — بعد از پرداخت همین‌جا تحویل داده می‌شود."
    sold_out = p.stock is not None and p.stock <= 0
    markup = kb(
        [btn("❌ ناموجود", "shno") if sold_out else btn("➕ افزودن به سبد", "sha", p.id)],
        [btn("🛒 سبد خرید", "shcart"), btn("🔙 بازگشت", "shl" if back else "shop", back, "0")],
    )
    chat_msg = ui.message if isinstance(ui, CallbackQuery) else ui
    if p.photo_file_id and isinstance(chat_msg, Message):
        try:
            await chat_msg.answer_photo(p.photo_file_id, caption=text[:1024], reply_markup=markup)
            if isinstance(ui, CallbackQuery):
                await safe_delete(chat_msg)
            return
        except TelegramAPIError as exc:
            log.warning("product photo failed (product=%s): %s", p.id, exc)
    await show(ui, text, markup)


@router.callback_query(A.filter(F.act == "shp"))
async def cb_product(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot, is_owner: bool) -> None:
    back = int(callback_data.p) if callback_data.p.isdigit() else 0
    await callback.answer()
    await show_product(callback, app, child, callback_data.id, back, is_owner)


@router.callback_query(A.filter(F.act == "shno"))
async def cb_soldout(callback: CallbackQuery) -> None:
    await callback.answer("این کالا فعلاً موجود نیست.", show_alert=True)


# ----------------------------------------------------------------------------- cart
@router.callback_query(A.filter(F.act == "sha"))
async def cb_add(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    if not _guard(child):
        await callback.answer("فروشگاه فعال نیست.", show_alert=True)
        return
    result = await app.shop.change_qty(child.id, callback.from_user.id, callback_data.id, +1)
    msg = {
        "ok": "✅ به سبد خرید اضافه شد.",
        "soldout": "این کالا فعلاً موجود نیست.",
        "max": f"حداکثر تعداد مجاز (موجودی یا {fmt_int(MAX_QTY)} عدد) در سبد است.",
        "missing": "این کالا دیگر در دسترس نیست.",
    }[result]
    await callback.answer(msg, show_alert=result != "ok")


async def _cart_view(app: App, child: ChildBot, user_id: int):
    lines = await app.shop.cart_lines(child.id, user_id)
    if not lines:
        return "🛒 سبد خرید شما خالی است.", kb([btn("🛍 رفتن به فروشگاه", "shop")], HOME)
    q = quote(child, lines)
    out = ["🛒 <b>سبد خرید</b>", ""]
    rows = []
    for line in lines:
        p = line.product
        warn = " ⚠️ موجودی کم" if (p.stock is not None and p.stock < line.qty) else ""
        out.append(f"• {esc(p.title)} × {fmt_int(line.qty)} — {fmt_toman(line.amount)}{warn}")
        rows.append([btn("➖", "shq", p.id, "-"), btn(f"{trunc(p.title, 16)} · {fmt_int(line.qty)}", "shp", p.id, "0"), btn("➕", "shq", p.id, "+"), btn("🗑", "shq", p.id, "x")])
    out += ["", f"جمع کالاها: {fmt_toman(q.subtotal)}"]
    if q.needs_shipping:
        out.append(f"هزینه ارسال: {fmt_toman(q.shipping) if q.shipping else 'رایگان'}")
        if child.shop_free_over_irt and q.shipping:
            out.append(f"💡 با خرید بالای {fmt_toman(child.shop_free_over_irt)} ارسال رایگان است.")
    out.append(f"💵 <b>مبلغ قابل پرداخت: {fmt_toman(q.total)}</b>")
    rows.append([btn("✅ ثبت سفارش و پرداخت", "sho")])
    rows.append([btn("🛍 ادامه خرید", "shop"), btn("🗑 خالی کردن", "shclr")])
    return "\n".join(out), kb(*rows)


@router.callback_query(A.filter(F.act == "shcart"))
async def cb_cart(callback: CallbackQuery, state: FSMContext, app: App, child: ChildBot) -> None:
    await state.clear()
    if not _guard(child):
        await callback.answer("فروشگاه فعال نیست.", show_alert=True)
        return
    text, markup = await _cart_view(app, child, callback.from_user.id)
    await show(callback, text, markup)
    await callback.answer()


@router.callback_query(A.filter(F.act == "shq"))
async def cb_qty(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    delta = {"+": 1, "-": -1, "x": 0}.get(callback_data.p)
    if delta is None:
        await callback.answer()
        return
    result = await app.shop.change_qty(child.id, callback.from_user.id, callback_data.id, delta)
    if result in ("soldout", "max"):
        await callback.answer("تعداد بیشتر از موجودی/حد مجاز است.", show_alert=True)
        return
    text, markup = await _cart_view(app, child, callback.from_user.id)
    await show(callback, text, markup)
    await callback.answer()


@router.callback_query(A.filter(F.act == "shclr"))
async def cb_clear(callback: CallbackQuery, app: App, child: ChildBot) -> None:
    await app.shop.clear_cart(child.id, callback.from_user.id)
    text, markup = await _cart_view(app, child, callback.from_user.id)
    await show(callback, text, markup)
    await callback.answer("سبد خالی شد.")


# ----------------------------------------------------------------------------- checkout
async def _payment_step(ui: Message | CallbackQuery, app: App, child: ChildBot, order: Order) -> None:
    order, items = await app.shop.get_order(order.id)
    assert order is not None
    methods = app.payments.methods_for(child, order.total, None)
    rows = [[btn(METHOD_LABEL[m], "pay", order.id, f"o.{m}")] for m in methods]
    rows.append([btn("❌ لغو سفارش", "shoc", order.id)])
    head = order_text(order, items)
    if not methods:
        head += "\n\n⚠️ فعلاً روش پرداختی برای این فروشگاه فعال نیست؛ لطفاً به مدیر ربات اطلاع دهید."
    else:
        head += "\n\nروش پرداخت را انتخاب کنید (موجودی کالاها برای شما نگه داشته شده است):"
    await show(ui, head, kb(*rows))


async def _finish_checkout(ui: Message | CallbackQuery, app: App, child: ChildBot, user_id: int, *, name: str, phone: str, address: str) -> None:
    try:
        order = await app.shop.create_order(child, user_id, name=name, phone=phone, address=address)
    except ShopError as exc:
        await show(ui, f"❌ {esc(exc)}", kb([btn("🛒 سبد خرید", "shcart")], HOME))
        return
    await _payment_step(ui, app, child, order)


@router.callback_query(A.filter(F.act == "sho"))
async def cb_checkout(callback: CallbackQuery, state: FSMContext, app: App, child: ChildBot) -> None:
    if not _guard(child):
        await callback.answer("فروشگاه فعال نیست.", show_alert=True)
        return
    uid = callback.from_user.id
    lines = await app.shop.cart_lines(child.id, uid)
    if not lines:
        await callback.answer("سبد خرید خالی است.", show_alert=True)
        return
    bad = next((ln for ln in lines if ln.product.stock is not None and ln.product.stock < ln.qty), None)
    if bad is not None:
        await callback.answer(f"موجودی «{bad.product.title}» کافی نیست.", show_alert=True)
        return
    await callback.answer()
    if not quote(child, lines).needs_shipping:
        await _finish_checkout(callback, app, child, uid, name="", phone="", address="")
        return
    last = await app.shop.last_address(child.id, uid)
    await state.set_state(Checkout.name)
    rows = [[btn("❌ انصراف", "shcart")]]
    text = "📍 <b>مشخصات ارسال</b>\n\nنام و نام خانوادگی گیرنده را بنویسید:"
    if last:
        rows.insert(0, [btn("📍 استفاده از آدرس قبلی", "shla")])
        text += f"\n\nیا آدرس قبلی را استفاده کنید:\n{esc(last[0])} · {esc(last[1])}\n{esc(trunc(last[2], 80))}"
    await show(callback, text, kb(*rows))


@router.callback_query(A.filter(F.act == "shla"))
async def cb_last_address(callback: CallbackQuery, state: FSMContext, app: App, child: ChildBot) -> None:
    last = await app.shop.last_address(child.id, callback.from_user.id)
    if last is None:
        await callback.answer("آدرس قبلی پیدا نشد.", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await _finish_checkout(callback, app, child, callback.from_user.id, name=last[0], phone=last[1], address=last[2])


@router.message(Checkout.name, F.text)
async def on_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 3 or len(name) > 100 or name.startswith("/"):
        await message.answer("نام کامل گیرنده را بنویسید (حداقل ۳ حرف).")
        return
    await state.update_data(name=name)
    await state.set_state(Checkout.phone)
    await message.answer("📞 شماره تماس گیرنده را بنویسید (مثلاً ۰۹۱۲۳۴۵۶۷۸۹):", reply_markup=kb([btn("❌ انصراف", "shcart")]))


_PHONE = re.compile(r"^\+?\d{10,13}$")


@router.message(Checkout.phone, F.text)
async def on_phone(message: Message, state: FSMContext) -> None:
    phone = re.sub(r"[\s\-()]", "", to_en(message.text or ""))
    if not _PHONE.match(phone):
        await message.answer("شماره معتبر نیست؛ فقط رقم‌ها را بنویسید (مثلاً ۰۹۱۲۳۴۵۶۷۸۹).")
        return
    await state.update_data(phone=phone)
    await state.set_state(Checkout.address)
    await message.answer("🏠 آدرس کامل پستی (شهر، خیابان، پلاک، کدپستی) را بنویسید:", reply_markup=kb([btn("❌ انصراف", "shcart")]))


@router.message(Checkout.address, F.text)
async def on_address(message: Message, state: FSMContext, app: App, child: ChildBot) -> None:
    address = (message.text or "").strip()
    if len(address) < 10 or len(address) > 500 or address.startswith("/"):
        await message.answer("آدرس کامل‌تری بنویسید (حداقل ۱۰ حرف).")
        return
    data = await state.get_data()
    await state.clear()
    await _finish_checkout(message, app, child, message.from_user.id, name=data.get("name", ""), phone=data.get("phone", ""), address=address)  # type: ignore[union-attr]


@router.callback_query(A.filter(F.act == "shoc"))
async def cb_cancel_order(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    order, _ = await app.shop.get_order(callback_data.id)
    if order is None or order.bot_id != child.id or order.user_id != callback.from_user.id:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    if await app.shop.cancel(order.id):
        await show(callback, f"❌ سفارش #{order.id} لغو شد.", kb([btn("🛍 فروشگاه", "shop")], HOME))
        await callback.answer()
    else:
        await callback.answer("این سفارش دیگر قابل لغو نیست.", show_alert=True)


# ----------------------------------------------------------------------------- my orders
@router.callback_query(A.filter(F.act == "shmo"))
async def cb_my_orders(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    page = callback_data.id
    uid = callback.from_user.id
    async with app.db.session() as s:
        total = int(await s.scalar(select(func.count()).select_from(Order).where(Order.bot_id == child.id, Order.user_id == uid)) or 0)
        rows = (
            await s.execute(
                select(Order.id, Order.status, Order.total)
                .where(Order.bot_id == child.id, Order.user_id == uid)
                .order_by(Order.id.desc())
                .offset(page * PAGE)
                .limit(PAGE)
            )
        ).all()
    if not rows:
        await show(callback, "هنوز سفارشی ثبت نکرده‌اید.", kb([btn("🛍 فروشگاه", "shop")], HOME))
        await callback.answer()
        return
    from ..services.shop import STATUS_LABEL

    buttons = [[btn(f"#{oid} · {STATUS_LABEL.get(st, st).split()[0]} · {fmt_int(tot)}", "shod", oid)] for oid, st, tot in rows]
    nav = []
    if page > 0:
        nav.append(btn("◀️ قبلی", "shmo", page - 1))
    if (page + 1) * PAGE < total:
        nav.append(btn("بعدی ▶️", "shmo", page + 1))
    if nav:
        buttons.append(nav)
    buttons.append(HOME)
    await show(callback, f"📦 <b>سفارش‌های من</b> ({fmt_int(total)})", kb(*buttons))
    await callback.answer()


@router.callback_query(A.filter(F.act == "shod"))
async def cb_order_detail(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    order, items = await app.shop.get_order(callback_data.id)
    if order is None or order.bot_id != child.id or order.user_id != callback.from_user.id:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    rows = []
    if order.status == "pending":
        rows.append([btn("💳 پرداخت", "shopay", order.id), btn("❌ لغو سفارش", "shoc", order.id)])
    rows.append([btn("🔙 سفارش‌های من", "shmo", 0)])
    await show(callback, order_text(order, items), kb(*rows))
    await callback.answer()


@router.callback_query(A.filter(F.act == "shopay"))
async def cb_pay_pending(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    order, _ = await app.shop.get_order(callback_data.id)
    if order is None or order.bot_id != child.id or order.user_id != callback.from_user.id or order.status != "pending":
        await callback.answer("این سفارش دیگر در انتظار پرداخت نیست.", show_alert=True)
        return
    await callback.answer()
    await _payment_step(callback, app, child, order)


