"""Shop — owner side: products, categories, orders, shipping settings."""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, func, select, update

from ..callbacks import A
from ..db.models import CartItem, ChildBot, Content, Order, Product, ProductVariant, ShopCategory
from ..services.shop import STATUS_LABEL, order_text, stock_text
from ..ui import btn, kb, safe_delete, show
from ..util import esc, fmt_int, fmt_toman, parse_int
from .common import PAGE, IsOwner, trunc
from .states import OrderShip, ProductAdd, ProductEdit, ShopCatS, ShopSet, VariantAdd, VariantEdit

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger(__name__)

router = Router(name="child_shop_admin")
router.message.filter(F.chat.type == ChatType.PRIVATE, IsOwner())
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE, IsOwner())

BACK_MENU = [btn("🔙 فروشگاه", "sam")]
CANCEL = [btn("❌ انصراف", "sam")]
FIELDS = {
    "title": "نام کالا",
    "price": "قیمت (تومان)",
    "compare": "قیمت قبل از تخفیف (تومان؛ «-» برای حذف)",
    "stock": "موجودی (عدد؛ «-» یعنی نامحدود)",
    "desc": "توضیحات (یا «-» برای حذف)",
    "photo": "عکس کالا را بفرستید",
}


# ----------------------------------------------------------------------------- menu
async def menu_view(app: App, child: ChildBot):
    async with app.db.session() as s:
        products = int(await s.scalar(select(func.count()).select_from(Product).where(Product.bot_id == child.id)) or 0)
        new_orders = int(await s.scalar(select(func.count()).select_from(Order).where(Order.bot_id == child.id, Order.status == "paid")) or 0)
    text = (
        "🛍 <b>مدیریت فروشگاه</b>\n\n"
        f"وضعیت: {'✅ فعال' if child.shop_enabled else '⛔️ غیرفعال (برای مشتریان دیده نمی‌شود)'}\n"
        f"🏷 کالاها: {fmt_int(products)}\n"
        f"📦 سفارش‌های در انتظار ارسال: {fmt_int(new_orders)}\n"
        f"🚚 هزینه ارسال: {fmt_toman(child.shop_shipping_irt) if child.shop_shipping_irt else 'رایگان'}"
        + (f" (رایگان از {fmt_toman(child.shop_free_over_irt)})" if child.shop_free_over_irt and child.shop_shipping_irt else "")
        + "\n\n💳 پرداخت‌ها از همان تنظیمات «پلن‌ها و پرداخت» (درگاه یا کارت به کارت) استفاده می‌کند."
    )
    return text, kb(
        [btn("⛔️ غیرفعال کردن فروشگاه" if child.shop_enabled else "✅ فعال کردن فروشگاه", "sat")],
        [btn("➕ کالای جدید", "sapa"), btn("🏷 کالاها", "sapl", 0)],
        [btn(f"📦 سفارش‌ها{f' ({fmt_int(new_orders)})' if new_orders else ''}", "sao", 0, "paid"), btn("🗂 دسته‌ها", "sac")],
        [btn("🚚 هزینه ارسال", "sas")],
        [btn("🔙 پنل مدیریت", "panel")],
    )


@router.callback_query(A.filter(F.act == "sam"))
async def cb_menu(callback: CallbackQuery, state: FSMContext, app: App, child: ChildBot) -> None:
    await state.clear()
    text, markup = await menu_view(app, child)
    await show(callback, text, markup)
    await callback.answer()


@router.callback_query(A.filter(F.act == "sat"))
async def cb_toggle(callback: CallbackQuery, app: App, child: ChildBot) -> None:
    async with app.db.session() as s:
        await s.execute(update(ChildBot).where(ChildBot.id == child.id).values(shop_enabled=not child.shop_enabled))
        await s.commit()
        fresh = await s.get(ChildBot, child.id)
    assert fresh is not None
    text, markup = await menu_view(app, fresh)
    await show(callback, text, markup)
    await callback.answer("انجام شد.")


# ----------------------------------------------------------------------------- products: list / detail
@router.callback_query(A.filter(F.act == "sapl"))
async def cb_products(callback: CallbackQuery, callback_data: A, state: FSMContext, app: App, child: ChildBot) -> None:
    await state.clear()
    page = callback_data.id
    async with app.db.session() as s:
        total = int(await s.scalar(select(func.count()).select_from(Product).where(Product.bot_id == child.id)) or 0)
        rows = (
            await s.execute(
                select(Product.id, Product.title, Product.price_irt, Product.stock, Product.is_active)
                .where(Product.bot_id == child.id)
                .order_by(Product.id.desc())
                .offset(page * PAGE)
                .limit(PAGE)
            )
        ).all()
    buttons = [
        [btn(f"{'✅' if act else '🚫'} {trunc(t, 24)} — {fmt_int(pr)}{'' if st is None else f' · {fmt_int(st)} عدد'}", "sap", pid)]
        for pid, t, pr, st, act in rows
    ]
    nav = []
    if page > 0:
        nav.append(btn("◀️ قبلی", "sapl", page - 1))
    if (page + 1) * PAGE < total:
        nav.append(btn("بعدی ▶️", "sapl", page + 1))
    if nav:
        buttons.append(nav)
    buttons.append([btn("➕ کالای جدید", "sapa")])
    buttons.append(BACK_MENU)
    await show(callback, f"🏷 <b>کالاها</b> ({fmt_int(total)})", kb(*buttons))
    await callback.answer()


async def _product(app: App, child: ChildBot, pid: int) -> Product | None:
    async with app.db.session() as s:
        p = await s.get(Product, pid)
    return p if p is not None and p.bot_id == child.id else None


async def product_view(app: App, child: ChildBot, p: Product):
    async with app.db.session() as s:
        vrows = (await s.execute(select(ProductVariant.stock, ProductVariant.is_active).where(ProductVariant.product_id == p.id))).all()
    cat = None
    if p.category_id:
        async with app.db.session() as s:
            c = await s.get(ShopCategory, p.category_id)
            cat = c.name if c else None
    lines = [
        f"🏷 <b>{esc(p.title)}</b>",
        f"وضعیت: {'✅ فعال' if p.is_active else '🚫 غیرفعال'}",
        f"💰 قیمت: {fmt_toman(p.price_irt)}" + (f" (قبل: {fmt_toman(p.compare_price_irt)})" if p.compare_price_irt else ""),
        (
            f"🎨 تنوع‌ها: {fmt_int(len(vrows))} گزینه (موجودی هر گزینه جدا حساب می‌شود)"
            if vrows
            else f"📦 موجودی: {'نامحدود' if p.stock is None else fmt_int(p.stock)} {stock_text(p.stock)}"
        ),
        f"🗂 دسته: {esc(cat) if cat else '—'}",
        f"🚚 نیاز به ارسال: {'بله' if p.needs_shipping else 'خیر (دیجیتال)'}" + (" · 📎 فایل متصل است" if p.content_id else ""),
        f"🔥 فروش: {fmt_int(p.sold)}",
        f"🖼 عکس: {'دارد' if p.photo_file_id else 'ندارد'}",
        f"🔗 لینک اشتراک: https://t.me/{child.username}?start=s_{p.id}",
    ]
    if p.description:
        lines += ["", esc(trunc(p.description, 300))]
    markup = kb(
        [btn("✏️ نام", "sape", p.id, "title"), btn("✏️ قیمت", "sape", p.id, "price"), btn("✏️ قبل از تخفیف", "sape", p.id, "compare")],
        [btn("✏️ موجودی", "sape", p.id, "stock"), btn("✏️ توضیحات", "sape", p.id, "desc"), btn("🖼 عکس", "sape", p.id, "photo")],
        [btn("🎨 تنوع (سایز/رنگ)", "sav", p.id), btn("🗂 دسته", "sapcat", p.id)],
        [btn("📎 فایل دیجیتال", "sapc", p.id, "0")],
        [btn("🚚 ارسال: " + ("فیزیکی" if p.needs_shipping else "دیجیتال"), "sapn", p.id)],
        [btn("🚫 غیرفعال" if p.is_active else "✅ فعال", "sapt", p.id), btn("🗑 حذف", "sapd", p.id), btn("👁 دید مشتری", "shp", p.id)],
        [btn("🔙 کالاها", "sapl", 0)],
    )
    return "\n".join(lines), markup


@router.callback_query(A.filter(F.act == "sap"))
async def cb_product(callback: CallbackQuery, callback_data: A, state: FSMContext, app: App, child: ChildBot) -> None:
    await state.clear()
    p = await _product(app, child, callback_data.id)
    if p is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    await show(callback, *await product_view(app, child, p))
    await callback.answer()


async def _refresh(callback: CallbackQuery, app: App, child: ChildBot, pid: int) -> None:
    p = await _product(app, child, pid)
    if p is not None:
        await show(callback, *await product_view(app, child, p))


@router.callback_query(A.filter(F.act.in_({"sapt", "sapn"})))
async def cb_product_toggle(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    p = await _product(app, child, callback_data.id)
    if p is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    field = "is_active" if callback_data.act == "sapt" else "needs_shipping"
    async with app.db.session() as s:
        await s.execute(update(Product).where(Product.id == p.id).values({field: not getattr(p, field)}))
        await s.commit()
    await _refresh(callback, app, child, p.id)
    await callback.answer("انجام شد.")


@router.callback_query(A.filter(F.act == "sapd"))
async def cb_product_delete_ask(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    p = await _product(app, child, callback_data.id)
    if p is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    await show(
        callback,
        f"🗑 کالای «{esc(p.title)}» حذف شود؟ (سفارش‌های قبلی حفظ می‌شوند.)",
        kb([btn("🗑 بله، حذف شود", "sapdy", p.id)], [btn("❌ انصراف", "sap", p.id)]),
    )
    await callback.answer()


@router.callback_query(A.filter(F.act == "sapdy"))
async def cb_product_delete(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    p = await _product(app, child, callback_data.id)
    if p is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    async with app.db.session() as s:
        await s.execute(delete(CartItem).where(CartItem.bot_id == child.id, CartItem.product_id == p.id))
        await s.execute(delete(ProductVariant).where(ProductVariant.product_id == p.id, ProductVariant.bot_id == child.id))
        await s.execute(delete(Product).where(Product.id == p.id, Product.bot_id == child.id))
        await s.commit()
    await callback.answer("حذف شد.")
    await cb_products(callback, A(act="sapl", id=0), _NoState(), app, child)  # type: ignore[arg-type]


class _NoState:
    async def clear(self) -> None:
        return None


# ----------------------------------------------------------------------------- products: add
@router.callback_query(A.filter(F.act == "sapa"))
async def cb_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProductAdd.title)
    await show(callback, "➕ <b>کالای جدید — مرحله ۱ از ۵</b>\nنام کالا را بنویسید:", kb(CANCEL))
    await callback.answer()


def _ok_text(message: Message) -> str | None:
    t = (message.text or "").strip()
    return t if t and not t.startswith("/") else None


@router.message(ProductAdd.title, F.text)
async def add_title(message: Message, state: FSMContext) -> None:
    t = _ok_text(message)
    if not t or len(t) > 200:
        await message.answer("نام کالا را بنویسید (حداکثر ۲۰۰ حرف).")
        return
    await state.update_data(title=t)
    await state.set_state(ProductAdd.price)
    await message.answer("💰 <b>مرحله ۲ از ۵</b>\nقیمت را به تومان بنویسید (فقط عدد):", reply_markup=kb(CANCEL))


@router.message(ProductAdd.price, F.text)
async def add_price(message: Message, state: FSMContext) -> None:
    price = parse_int(message.text or "")
    if not price or price > 2_000_000_000:
        await message.answer("قیمت را فقط به‌صورت عدد (تومان) بنویسید.")
        return
    await state.update_data(price=price)
    await state.set_state(ProductAdd.stock)
    await message.answer("📦 <b>مرحله ۳ از ۵</b>\nموجودی را بنویسید. اگر محدودیتی ندارد «-» بفرستید:", reply_markup=kb(CANCEL))


def parse_stock(text: str) -> tuple[bool, int | None]:
    t = (text or "").strip()
    if t in ("-", "نامحدود"):
        return True, None
    n = parse_int(t)
    if n is None and t in ("0", "۰"):
        n = 0
    return (n is not None and n <= 10_000_000), n


@router.message(ProductAdd.stock, F.text)
async def add_stock(message: Message, state: FSMContext) -> None:
    ok, stock = parse_stock(message.text or "")
    if not ok:
        await message.answer("یک عدد بنویسید یا برای نامحدود «-» بفرستید.")
        return
    await state.update_data(stock=stock)
    await state.set_state(ProductAdd.photo)
    await message.answer(
        "🖼 <b>مرحله ۴ از ۵</b>\nعکس کالا را بفرستید (یا رد شوید):",
        reply_markup=kb([btn("⏭ بدون عکس", "sapskip", 0, "photo")], CANCEL),
    )


@router.message(ProductAdd.photo, F.photo)
async def add_photo(message: Message, state: FSMContext) -> None:
    await state.update_data(photo=message.photo[-1].file_id)  # type: ignore[index]
    await _ask_desc(message, state)


async def _ask_desc(ui: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProductAdd.desc)
    await show(ui, "📝 <b>مرحله ۵ از ۵</b>\nتوضیحات کالا را بنویسید (یا رد شوید):", kb([btn("⏭ بدون توضیح", "sapskip", 0, "desc")], CANCEL))


@router.callback_query(A.filter(F.act == "sapskip"))
async def cb_skip(callback: CallbackQuery, callback_data: A, state: FSMContext, app: App, child: ChildBot) -> None:
    cur = await state.get_state()
    if callback_data.p == "photo" and cur == ProductAdd.photo.state:
        await _ask_desc(callback, state)
    elif callback_data.p == "desc" and cur == ProductAdd.desc.state:
        await _save_new(callback, state, app, child, None)
    await callback.answer()


@router.message(ProductAdd.desc, F.text)
async def add_desc(message: Message, state: FSMContext, app: App, child: ChildBot) -> None:
    t = _ok_text(message)
    if not t:
        await message.answer("توضیحات را بنویسید یا رد شوید.")
        return
    await _save_new(message, state, app, child, t[:2000])


async def _save_new(ui: Message | CallbackQuery, state: FSMContext, app: App, child: ChildBot, desc: str | None) -> None:
    d = await state.get_data()
    await state.clear()
    async with app.db.session() as s:
        p = Product(
            bot_id=child.id, title=d["title"], price_irt=int(d["price"]), stock=d.get("stock"),
            photo_file_id=d.get("photo"), description=desc,
        )
        s.add(p)
        await s.commit()
    text, markup = await product_view(app, child, p)
    await show(ui, "✅ کالا ثبت شد.\n\n" + text, markup)


# ----------------------------------------------------------------------------- products: edit one field
@router.callback_query(A.filter(F.act == "sape"))
async def cb_edit(callback: CallbackQuery, callback_data: A, state: FSMContext, app: App, child: ChildBot) -> None:
    p = await _product(app, child, callback_data.id)
    field = callback_data.p
    if p is None or field not in FIELDS:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    await state.set_state(ProductEdit.value)
    await state.update_data(pid=p.id, field=field)
    await show(callback, f"✏️ {FIELDS[field]}:", kb([btn("❌ انصراف", "sap", p.id)]))
    await callback.answer()


@router.message(ProductEdit.value, F.text | F.photo)
async def on_edit(message: Message, state: FSMContext, app: App, child: ChildBot) -> None:
    d = await state.get_data()
    p = await _product(app, child, int(d.get("pid", 0)))
    field = d.get("field", "")
    if p is None or field not in FIELDS:
        await state.clear()
        return
    raw = (message.text or "").strip()
    values: dict[str, object]
    if field == "photo":
        if not message.photo:
            await message.answer("یک عکس بفرستید.")
            return
        values = {"photo_file_id": message.photo[-1].file_id}
    elif message.photo or raw.startswith("/"):
        await message.answer("متن را بنویسید.")
        return
    elif field == "title":
        if not raw or len(raw) > 200:
            await message.answer("نام معتبر بنویسید (حداکثر ۲۰۰ حرف).")
            return
        values = {"title": raw}
    elif field == "price":
        n = parse_int(raw)
        if not n or n > 2_000_000_000:
            await message.answer("قیمت را فقط به‌صورت عدد بنویسید.")
            return
        values = {"price_irt": n}
    elif field == "compare":
        n = None if raw == "-" else parse_int(raw)
        if raw != "-" and (not n or n <= p.price_irt or n > 2_000_000_000):
            await message.answer("قیمت قبل از تخفیف باید از قیمت فعلی بیشتر باشد (یا «-»).")
            return
        values = {"compare_price_irt": n}
    elif field == "stock":
        ok, stock = parse_stock(raw)
        if not ok:
            await message.answer("یک عدد بنویسید یا «-» برای نامحدود.")
            return
        values = {"stock": stock}
    else:  # desc
        values = {"description": None if raw == "-" else raw[:2000]}
    async with app.db.session() as s:
        await s.execute(update(Product).where(Product.id == p.id, Product.bot_id == child.id).values(**values))
        await s.commit()
    await state.clear()
    if field == "photo":
        await safe_delete(message)
    fresh = await _product(app, child, p.id)
    assert fresh is not None
    text, markup = await product_view(app, child, fresh)
    await message.answer("✅ ذخیره شد.\n\n" + text, reply_markup=markup)


# ----------------------------------------------------------------------------- products: category & digital file
@router.callback_query(A.filter(F.act == "sapcat"))
async def cb_pick_category(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    p = await _product(app, child, callback_data.id)
    if p is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    async with app.db.session() as s:
        cats = list((await s.scalars(select(ShopCategory).where(ShopCategory.bot_id == child.id).order_by(ShopCategory.id))).all())
    if not cats:
        await callback.answer("ابتدا از «🗂 دسته‌ها» یک دسته بسازید.", show_alert=True)
        return
    rows = [[btn(("✅ " if p.category_id == c.id else "") + trunc(c.name), "sapcs", p.id, str(c.id))] for c in cats]
    rows.append([btn("بدون دسته", "sapcs", p.id, "0")])
    rows.append([btn("🔙 بازگشت", "sap", p.id)])
    await show(callback, "🗂 دسته کالا را انتخاب کنید:", kb(*rows))
    await callback.answer()


@router.callback_query(A.filter(F.act == "sapcs"))
async def cb_set_category(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    p = await _product(app, child, callback_data.id)
    cid = int(callback_data.p) if callback_data.p.isdigit() else 0
    if p is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    async with app.db.session() as s:
        if cid and (c := await s.get(ShopCategory, cid)) is not None and c.bot_id == child.id:
            await s.execute(update(Product).where(Product.id == p.id).values(category_id=cid))
        elif not cid:
            await s.execute(update(Product).where(Product.id == p.id).values(category_id=None))
        await s.commit()
    await _refresh(callback, app, child, p.id)
    await callback.answer("ذخیره شد.")


@router.callback_query(A.filter(F.act == "sapc"))
async def cb_pick_content(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    p = await _product(app, child, callback_data.id)
    if p is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    page = int(callback_data.p) if callback_data.p.isdigit() else 0
    async with app.db.session() as s:
        total = int(await s.scalar(select(func.count()).select_from(Content).where(Content.bot_id == child.id)) or 0)
        rows = (await s.execute(select(Content.id, Content.title).where(Content.bot_id == child.id).order_by(Content.id.desc()).offset(page * PAGE).limit(PAGE))).all()
    if not rows and not p.content_id:
        await callback.answer("ابتدا فایلی را در «افزودن محتوا» آپلود کنید.", show_alert=True)
        return
    buttons = [[btn(("✅ " if p.content_id == cid else "📎 ") + trunc(t, 30), "sapcf", p.id, str(cid))] for cid, t in rows]
    nav = []
    if page > 0:
        nav.append(btn("◀️", "sapc", p.id, str(page - 1)))
    if (page + 1) * PAGE < total:
        nav.append(btn("▶️", "sapc", p.id, str(page + 1)))
    if nav:
        buttons.append(nav)
    if p.content_id:
        buttons.append([btn("✖️ جدا کردن فایل", "sapcf", p.id, "0")])
    buttons.append([btn("🔙 بازگشت", "sap", p.id)])
    await show(callback, "📎 فایلی که بعد از پرداخت خودکار برای خریدار ارسال می‌شود را انتخاب کنید (کالا دیجیتال می‌شود و نیاز به ارسال پستی ندارد):", kb(*buttons))
    await callback.answer()


@router.callback_query(A.filter(F.act == "sapcf"))
async def cb_set_content(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    p = await _product(app, child, callback_data.id)
    cid = int(callback_data.p) if callback_data.p.isdigit() else 0
    if p is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    async with app.db.session() as s:
        if cid:
            c = await s.get(Content, cid)
            if c is None or c.bot_id != child.id:
                await callback.answer("فایل پیدا نشد.", show_alert=True)
                return
            await s.execute(update(Product).where(Product.id == p.id).values(content_id=cid, needs_shipping=False))
        else:
            await s.execute(update(Product).where(Product.id == p.id).values(content_id=None))
        await s.commit()
    await _refresh(callback, app, child, p.id)
    await callback.answer("ذخیره شد.")


# ----------------------------------------------------------------------------- categories
@router.callback_query(A.filter(F.act == "sac"))
async def cb_cats(callback: CallbackQuery, state: FSMContext, app: App, child: ChildBot) -> None:
    await state.clear()
    async with app.db.session() as s:
        cats = list((await s.scalars(select(ShopCategory).where(ShopCategory.bot_id == child.id).order_by(ShopCategory.id))).all())
    rows = [[btn(f"🗑 {trunc(c.name)}", "sacd", c.id)] for c in cats]
    rows.append([btn("➕ دسته جدید", "sacn")])
    rows.append(BACK_MENU)
    await show(callback, "🗂 <b>دسته‌های فروشگاه</b>\nبرای حذف روی نام دسته بزنید (کالاها حذف نمی‌شوند).", kb(*rows))
    await callback.answer()


@router.callback_query(A.filter(F.act == "sacn"))
async def cb_cat_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ShopCatS.name)
    await show(callback, "نام دسته را بنویسید:", kb([btn("❌ انصراف", "sac")]))
    await callback.answer()


@router.message(ShopCatS.name, F.text)
async def on_cat_name(message: Message, state: FSMContext, app: App, child: ChildBot) -> None:
    t = _ok_text(message)
    if not t or len(t) > 100:
        await message.answer("نام دسته را بنویسید (حداکثر ۱۰۰ حرف).")
        return
    async with app.db.session() as s:
        s.add(ShopCategory(bot_id=child.id, name=t))
        await s.commit()
    await state.clear()
    await message.answer("✅ دسته ساخته شد.", reply_markup=kb([btn("🗂 دسته‌ها", "sac")], BACK_MENU))


@router.callback_query(A.filter(F.act == "sacd"))
async def cb_cat_delete(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    async with app.db.session() as s:
        await s.execute(update(Product).where(Product.bot_id == child.id, Product.category_id == callback_data.id).values(category_id=None))
        await s.execute(delete(ShopCategory).where(ShopCategory.id == callback_data.id, ShopCategory.bot_id == child.id))
        await s.commit()
    await callback.answer("حذف شد.")
    await cb_cats(callback, _NoState(), app, child)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- shipping settings
@router.callback_query(A.filter(F.act == "sas"))
async def cb_shipping(callback: CallbackQuery, state: FSMContext, child: ChildBot) -> None:
    await state.clear()
    await show(
        callback,
        "🚚 <b>هزینه ارسال</b>\n\n"
        f"هزینه ثابت هر سفارش: {fmt_toman(child.shop_shipping_irt) if child.shop_shipping_irt else 'رایگان'}\n"
        f"ارسال رایگان از مبلغ: {fmt_toman(child.shop_free_over_irt) if child.shop_free_over_irt else '—'}\n\n"
        "برای سفارش‌هایی که فقط کالای دیجیتال دارند هزینه ارسال محاسبه نمی‌شود.",
        kb([btn("✏️ هزینه ارسال", "sass", 0, "ship"), btn("✏️ ارسال رایگان از…", "sass", 0, "free")], BACK_MENU),
    )
    await callback.answer()


@router.callback_query(A.filter(F.act == "sass"))
async def cb_shipping_edit(callback: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    if callback_data.p == "ship":
        await state.set_state(ShopSet.shipping)
        text = "هزینه ارسال هر سفارش را به تومان بنویسید (۰ یعنی رایگان):"
    else:
        await state.set_state(ShopSet.free_over)
        text = "از چه مبلغ خرید به بالا ارسال رایگان باشد؟ (تومان؛ «-» یعنی هیچ‌وقت)"
    await show(callback, text, kb([btn("❌ انصراف", "sas")]))
    await callback.answer()


@router.message(ShopSet.shipping, F.text)
async def on_shipping(message: Message, state: FSMContext, app: App, child: ChildBot) -> None:
    t = (message.text or "").strip()
    n = 0 if t in ("0", "۰") else parse_int(t)
    if n is None or n > 100_000_000:
        await message.answer("عدد معتبر (تومان) بنویسید.")
        return
    async with app.db.session() as s:
        await s.execute(update(ChildBot).where(ChildBot.id == child.id).values(shop_shipping_irt=n))
        await s.commit()
    await state.clear()
    await message.answer("✅ ذخیره شد.", reply_markup=kb([btn("🚚 هزینه ارسال", "sas")], BACK_MENU))


@router.message(ShopSet.free_over, F.text)
async def on_free_over(message: Message, state: FSMContext, app: App, child: ChildBot) -> None:
    t = (message.text or "").strip()
    n = None if t == "-" else parse_int(t)
    if t != "-" and (not n or n > 2_000_000_000):
        await message.answer("عدد معتبر (تومان) بنویسید یا «-».")
        return
    async with app.db.session() as s:
        await s.execute(update(ChildBot).where(ChildBot.id == child.id).values(shop_free_over_irt=n))
        await s.commit()
    await state.clear()
    await message.answer("✅ ذخیره شد.", reply_markup=kb([btn("🚚 هزینه ارسال", "sas")], BACK_MENU))


# ----------------------------------------------------------------------------- orders
FILTERS = {"paid": "📦 در انتظار ارسال", "shipped": "🚚 ارسال‌شده", "all": "📋 همه"}


@router.callback_query(A.filter(F.act == "sao"))
async def cb_orders(callback: CallbackQuery, callback_data: A, state: FSMContext, app: App, child: ChildBot) -> None:
    await state.clear()
    page, flt = callback_data.id, callback_data.p if callback_data.p in FILTERS else "paid"
    cond = [Order.bot_id == child.id]
    if flt == "all":
        cond.append(Order.status != "pending")
    else:
        cond.append(Order.status == flt)
    async with app.db.session() as s:
        total = int(await s.scalar(select(func.count()).select_from(Order).where(*cond)) or 0)
        rows = (await s.execute(select(Order.id, Order.status, Order.total, Order.name).where(*cond).order_by(Order.id.desc()).offset(page * PAGE).limit(PAGE))).all()
    buttons = [[btn(f"#{oid} · {STATUS_LABEL[st].split()[0]} · {fmt_int(tot)} · {trunc(nm or 'دیجیتال', 14)}", "saod", oid)] for oid, st, tot, nm in rows]
    nav = []
    if page > 0:
        nav.append(btn("◀️", "sao", page - 1, flt))
    if (page + 1) * PAGE < total:
        nav.append(btn("▶️", "sao", page + 1, flt))
    if nav:
        buttons.append(nav)
    buttons.append([btn(("• " if k == flt else "") + v, "sao", 0, k) for k, v in FILTERS.items()])
    buttons.append(BACK_MENU)
    await show(callback, f"📦 <b>سفارش‌ها — {FILTERS[flt]}</b> ({fmt_int(total)})", kb(*buttons))
    await callback.answer()


async def _order(app: App, child: ChildBot, oid: int):
    order, items = await app.shop.get_order(oid)
    return (order, items) if order is not None and order.bot_id == child.id else (None, [])


def _order_markup(order: Order):
    rows = []
    if order.status == "paid" and order.needs_shipping:
        rows.append([btn("📦 ارسال شد", "saos", order.id)])
    if order.status == "shipped":
        rows.append([btn("✅ تحویل شد", "saodl", order.id)])
    if order.status in ("pending", "paid"):
        rows.append([btn("❌ لغو سفارش", "saoc", order.id)])
    rows.append([btn("🔙 سفارش‌ها", "sao", 0, "paid")])
    return kb(*rows)


@router.callback_query(A.filter(F.act == "saod"))
async def cb_order(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    order, items = await _order(app, child, callback_data.id)
    if order is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    await show(callback, order_text(order, items, owner=True), _order_markup(order))
    await callback.answer()


async def _tell_buyer(callback: CallbackQuery, order: Order, text: str) -> None:
    with suppress(TelegramAPIError):
        await callback.bot.send_message(order.user_id, text)  # type: ignore[union-attr]


@router.callback_query(A.filter(F.act == "saos"))
async def cb_ship_ask(callback: CallbackQuery, callback_data: A, state: FSMContext, app: App, child: ChildBot) -> None:
    order, _ = await _order(app, child, callback_data.id)
    if order is None or order.status != "paid":
        await callback.answer("این سفارش در وضعیت قابل ارسال نیست.", show_alert=True)
        return
    await state.set_state(OrderShip.tracking)
    await state.update_data(oid=order.id)
    await show(callback, f"📦 سفارش #{order.id}\nکد رهگیری مرسوله را بنویسید (اگر ندارید «-» بفرستید):", kb([btn("❌ انصراف", "saod", order.id)]))
    await callback.answer()


@router.message(OrderShip.tracking, F.text)
async def on_tracking(message: Message, state: FSMContext, app: App, child: ChildBot) -> None:
    t = (message.text or "").strip()
    if not t or t.startswith("/") or len(t) > 120:
        await message.answer("کد رهگیری را بنویسید یا «-».")
        return
    oid = int((await state.get_data()).get("oid", 0))
    await state.clear()
    order, items = await _order(app, child, oid)
    if order is None:
        return
    async with app.db.session() as s:
        res = await s.execute(
            update(Order).where(Order.id == oid, Order.bot_id == child.id, Order.status == "paid").values(status="shipped", tracking=None if t == "-" else t[:120])
        )
        await s.commit()
    if res.rowcount != 1:
        await message.answer("این سفارش دیگر در وضعیت «در انتظار ارسال» نیست.")
        return
    order, items = await _order(app, child, oid)
    assert order is not None
    with suppress(TelegramAPIError):
        await message.bot.send_message(  # type: ignore[union-attr]
            order.user_id,
            f"📦 سفارش #{order.id} شما ارسال شد." + (f"\n🔎 کد رهگیری: <code>{esc(order.tracking)}</code>" if order.tracking else ""),
        )
    await message.answer("✅ ثبت شد و به خریدار اطلاع داده شد.\n\n" + order_text(order, items, owner=True), reply_markup=_order_markup(order))


@router.callback_query(A.filter(F.act == "saodl"))
async def cb_delivered(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    async with app.db.session() as s:
        res = await s.execute(update(Order).where(Order.id == callback_data.id, Order.bot_id == child.id, Order.status == "shipped").values(status="delivered"))
        await s.commit()
    order, items = await _order(app, child, callback_data.id)
    if order is None or res.rowcount != 1:
        await callback.answer("این سفارش در وضعیت «ارسال‌شده» نیست.", show_alert=True)
        return
    await _tell_buyer(callback, order, f"✅ سفارش #{order.id} تحویل داده شد. از خرید شما سپاسگزاریم 🙏")
    await show(callback, order_text(order, items, owner=True), _order_markup(order))
    await callback.answer("ثبت شد.")


@router.callback_query(A.filter(F.act == "saoc"))
async def cb_cancel_ask(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    order, _ = await _order(app, child, callback_data.id)
    if order is None or order.status not in ("pending", "paid"):
        await callback.answer("این سفارش قابل لغو نیست.", show_alert=True)
        return
    warn = "\n\n⚠️ پرداخت انجام شده؛ بازگرداندن وجه به خریدار با خودتان است." if order.status == "paid" else ""
    await show(callback, f"❌ سفارش #{order.id} لغو و موجودی کالاها برگردانده شود؟{warn}", kb([btn("بله، لغو شود", "saocy", order.id)], [btn("انصراف", "saod", order.id)]))
    await callback.answer()


@router.callback_query(A.filter(F.act == "saocy"))
async def cb_cancel_do(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    order, items = await _order(app, child, callback_data.id)
    if order is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    was_paid = order.status == "paid"
    if not await app.shop.cancel(order.id, allowed=("pending", "paid")):
        await callback.answer("این سفارش دیگر قابل لغو نیست.", show_alert=True)
        return
    await _tell_buyer(callback, order, f"❌ سفارش #{order.id} توسط فروشگاه لغو شد." + (" مبلغ پرداختی از طرف فروشگاه بازگردانده می‌شود." if was_paid else ""))
    order, items = await _order(app, child, order.id)
    assert order is not None
    await show(callback, order_text(order, items, owner=True), _order_markup(order))
    await callback.answer("لغو شد.")


# ----------------------------------------------------------------------------- variants (size / colour / …)
def parse_variant_lines(text: str) -> tuple[list[tuple[str, int | None, int | None]], list[str]]:
    """Each line: ``label | stock | price`` (stock «-» = unlimited; price optional, «-» = product price)."""
    rows: list[tuple[str, int | None, int | None]] = []
    bad: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [x.strip() for x in line.split("|")]
        label = parts[0]
        ok = bool(label) and len(label) <= 100 and len(parts) <= 3 and not label.startswith("/")
        stock: int | None = None
        price: int | None = None
        if ok and len(parts) >= 2:
            ok, stock = parse_stock(parts[1])
        if ok and len(parts) == 3 and parts[2] not in ("-", ""):
            price = parse_int(parts[2])
            ok = price is not None and 0 < price <= 2_000_000_000
        if ok:
            rows.append((label, stock, price))
        else:
            bad.append(line)
    return rows, bad


async def _variant(app: App, child: ChildBot, vid: int) -> ProductVariant | None:
    async with app.db.session() as s:
        v = await s.get(ProductVariant, vid)
    return v if v is not None and v.bot_id == child.id else None


def _vlabel(v: ProductVariant) -> str:
    stock = "نامحدود" if v.stock is None else f"{fmt_int(v.stock)} عدد"
    price = f" · {fmt_toman(v.price_irt)}" if v.price_irt else ""
    return f"{'✅' if v.is_active else '🚫'} {trunc(v.label, 22)} · {stock}{price}"


async def variants_view(app: App, child: ChildBot, p: Product):
    async with app.db.session() as s:
        vs = list((await s.scalars(select(ProductVariant).where(ProductVariant.product_id == p.id).order_by(ProductVariant.id))).all())
    text = (
        f"🎨 <b>تنوع‌های «{esc(p.title)}»</b>\n\n"
        "هر گزینه (مثلاً سایز ۴۲ مشکی) موجودی جدا دارد و می‌تواند قیمت متفاوت داشته باشد؛ مشتری هنگام خرید یکی را انتخاب می‌کند.\n"
        "برای ویرایش روی هر گزینه بزنید."
        if vs
        else f"🎨 برای «{esc(p.title)}» هنوز گزینه‌ای تعریف نشده. اگر گزینه‌ای اضافه کنید، موجودی هر گزینه جدا حساب می‌شود."
    )
    rows = [[btn(_vlabel(v), "savd", v.id)] for v in vs]
    rows.append([btn("➕ افزودن گزینه‌ها", "sava", p.id)])
    rows.append([btn("🔙 کالا", "sap", p.id)])
    return text, kb(*rows)


@router.callback_query(A.filter(F.act == "sav"))
async def cb_variants(callback: CallbackQuery, callback_data: A, state: FSMContext, app: App, child: ChildBot) -> None:
    await state.clear()
    p = await _product(app, child, callback_data.id)
    if p is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    await show(callback, *await variants_view(app, child, p))
    await callback.answer()


@router.callback_query(A.filter(F.act == "sava"))
async def cb_variant_add(callback: CallbackQuery, callback_data: A, state: FSMContext, app: App, child: ChildBot) -> None:
    p = await _product(app, child, callback_data.id)
    if p is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    await state.set_state(VariantAdd.lines)
    await state.update_data(pid=p.id)
    await show(
        callback,
        "➕ <b>افزودن گزینه</b>\n\nهر گزینه را در یک خط بنویسید (می‌توانید چند خط را یک‌جا بفرستید):\n"
        "<code>نام گزینه | موجودی | قیمت</code>\n\n"
        "• موجودی: عدد، یا «-» برای نامحدود\n"
        "• قیمت اختیاری است؛ اگر ننویسید قیمت خود کالا اعمال می‌شود\n\n"
        "مثال:\n<code>۴۲ مشکی | ۵\n۴۳ مشکی | ۳\n۴۳ سفید | ۲ | ۴۹۰۰۰۰</code>",
        kb([btn("❌ انصراف", "sav", p.id)]),
    )
    await callback.answer()


@router.message(VariantAdd.lines, F.text)
async def on_variant_lines(message: Message, state: FSMContext, app: App, child: ChildBot) -> None:
    pid = int((await state.get_data()).get("pid", 0))
    p = await _product(app, child, pid)
    if p is None:
        await state.clear()
        return
    rows, bad = parse_variant_lines(message.text or "")
    if bad or not rows or len(rows) > 40:
        msg = "این خط‌ها معتبر نبودند:\n" + "\n".join(f"• {esc(trunc(b, 50))}" for b in bad[:10]) if bad else "حداکثر ۴۰ گزینه در هر بار و حداقل یکی."
        await message.answer(f"❌ {msg}\n\nقالب: <code>نام | موجودی | قیمت(اختیاری)</code> — دوباره بفرستید.", reply_markup=kb([btn("❌ انصراف", "sav", p.id)]))
        return
    async with app.db.session() as s:
        for label, stock, price in rows:
            s.add(ProductVariant(bot_id=child.id, product_id=p.id, label=label, stock=stock, price_irt=price))
        await s.commit()
    await state.clear()
    text, markup = await variants_view(app, child, p)
    await message.answer(f"✅ {fmt_int(len(rows))} گزینه اضافه شد.\n\n" + text, reply_markup=markup)


@router.callback_query(A.filter(F.act == "savd"))
async def cb_variant_detail(callback: CallbackQuery, callback_data: A, state: FSMContext, app: App, child: ChildBot) -> None:
    await state.clear()
    v = await _variant(app, child, callback_data.id)
    if v is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    await show(
        callback,
        f"🎨 <b>{esc(v.label)}</b>\nوضعیت: {'✅ فعال' if v.is_active else '🚫 غیرفعال'}\n"
        f"📦 موجودی: {'نامحدود' if v.stock is None else fmt_int(v.stock)}\n"
        f"💰 قیمت: {fmt_toman(v.price_irt) if v.price_irt else 'همان قیمت کالا'}",
        kb(
            [btn("✏️ نام", "save", v.id, "label"), btn("✏️ موجودی", "save", v.id, "stock"), btn("✏️ قیمت", "save", v.id, "price")],
            [btn("🚫 غیرفعال" if v.is_active else "✅ فعال", "savt", v.id), btn("🗑 حذف", "savx", v.id)],
            [btn("🔙 تنوع‌ها", "sav", v.product_id)],
        ),
    )
    await callback.answer()


@router.callback_query(A.filter(F.act.in_({"savt", "savx"})))
async def cb_variant_toggle_delete(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    v = await _variant(app, child, callback_data.id)
    if v is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    async with app.db.session() as s:
        if callback_data.act == "savt":
            await s.execute(update(ProductVariant).where(ProductVariant.id == v.id).values(is_active=not v.is_active))
        else:
            await s.execute(delete(CartItem).where(CartItem.bot_id == child.id, CartItem.variant_id == v.id))
            await s.execute(delete(ProductVariant).where(ProductVariant.id == v.id, ProductVariant.bot_id == child.id))
        await s.commit()
    await callback.answer("انجام شد.")
    if callback_data.act == "savt":
        await cb_variant_detail(callback, A(act="savd", id=v.id), _NoState(), app, child)  # type: ignore[arg-type]
    else:
        p = await _product(app, child, v.product_id)
        if p is not None:
            await show(callback, *await variants_view(app, child, p))


@router.callback_query(A.filter(F.act == "save"))
async def cb_variant_edit(callback: CallbackQuery, callback_data: A, state: FSMContext, app: App, child: ChildBot) -> None:
    v = await _variant(app, child, callback_data.id)
    field = callback_data.p
    prompts = {
        "label": "نام جدید گزینه را بنویسید:",
        "stock": "موجودی را بنویسید (عدد؛ «-» یعنی نامحدود):",
        "price": "قیمت این گزینه را به تومان بنویسید («-» یعنی همان قیمت کالا):",
    }
    if v is None or field not in prompts:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    await state.set_state(VariantEdit.value)
    await state.update_data(vid=v.id, field=field)
    await show(callback, "✏️ " + prompts[field], kb([btn("❌ انصراف", "savd", v.id)]))
    await callback.answer()


@router.message(VariantEdit.value, F.text)
async def on_variant_edit(message: Message, state: FSMContext, app: App, child: ChildBot) -> None:
    d = await state.get_data()
    v = await _variant(app, child, int(d.get("vid", 0)))
    field = d.get("field", "")
    raw = (message.text or "").strip()
    if v is None:
        await state.clear()
        return
    values: dict[str, object]
    if field == "label":
        if not raw or len(raw) > 100 or raw.startswith("/"):
            await message.answer("نام معتبر بنویسید (حداکثر ۱۰۰ حرف).")
            return
        values = {"label": raw}
    elif field == "stock":
        ok, stock = parse_stock(raw)
        if not ok:
            await message.answer("یک عدد بنویسید یا «-» برای نامحدود.")
            return
        values = {"stock": stock}
    elif field == "price":
        price = None if raw == "-" else parse_int(raw)
        if raw != "-" and (not price or price > 2_000_000_000):
            await message.answer("قیمت را به تومان (عدد) بنویسید یا «-».")
            return
        values = {"price_irt": price}
    else:
        await state.clear()
        return
    async with app.db.session() as s:
        await s.execute(update(ProductVariant).where(ProductVariant.id == v.id, ProductVariant.bot_id == child.id).values(**values))
        await s.commit()
    await state.clear()
    await message.answer("✅ ذخیره شد.", reply_markup=kb([btn("🎨 گزینه", "savd", v.id)], [btn("🔙 تنوع‌ها", "sav", v.product_id)]))
