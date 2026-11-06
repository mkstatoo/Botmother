"""Shop module: cart, orders, atomic stock reservation, payment hooks, housekeeping.

Resource notes
--------------
* Product photos are Telegram ``file_id``s: nothing is stored or processed locally.
* The cart lives in the DB (survives restarts) as one tiny row per product.
* Stock is reserved with a single conditional ``UPDATE`` per line (no locks, no races) when the
  order is created and released when the order is cancelled or never paid.
* Stale unpaid orders are released by :meth:`ShopService.gc`, which the existing cleanup loop calls
  at most every few minutes, so the shop adds no background task of its own.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError
from sqlalchemy import and_, delete, func, or_, select, update

from ..db.models import CartItem, ChildBot, Content, Order, OrderItem, Payment, Product
from ..ui import btn, kb
from ..util import esc, fmt_int, fmt_toman, jdate, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger(__name__)

MAX_QTY = 20
UNPAID_HOLD = timedelta(minutes=30)  # online payment window
MANUAL_HOLD = timedelta(hours=24)  # card-to-card: the buyer may still send a receipt
CART_TTL = timedelta(days=30)
GC_EVERY = timedelta(minutes=5)

STATUS_LABEL = {
    "pending": "⌛️ در انتظار پرداخت",
    "paid": "💰 پرداخت‌شده — در انتظار ارسال",
    "shipped": "📦 ارسال‌شده",
    "delivered": "✅ تحویل‌شده",
    "cancelled": "❌ لغو شده",
}


class ShopError(Exception):
    """Carries a user-facing Persian message."""


@dataclass
class Quote:
    subtotal: int
    shipping: int
    needs_shipping: bool

    @property
    def total(self) -> int:
        return self.subtotal + self.shipping


@dataclass
class Line:
    product: Product
    qty: int

    @property
    def amount(self) -> int:
        return self.product.price_irt * self.qty


def quote(child: ChildBot, lines: list[Line]) -> Quote:
    subtotal = sum(line.amount for line in lines)
    needs = any(line.product.needs_shipping for line in lines)
    shipping = 0
    if needs and child.shop_shipping_irt:
        free = child.shop_free_over_irt
        shipping = 0 if (free and subtotal >= free) else int(child.shop_shipping_irt)
    return Quote(subtotal, shipping, needs)


def stock_text(stock: int | None) -> str:
    if stock is None:
        return ""
    return "❌ ناموجود" if stock <= 0 else (f"⚠️ فقط {fmt_int(stock)} عدد مانده" if stock <= 5 else "")


def price_line(p: Product) -> str:
    if p.compare_price_irt and p.compare_price_irt > p.price_irt:
        off = round(100 - p.price_irt * 100 / p.compare_price_irt)
        return f"<s>{fmt_toman(p.compare_price_irt)}</s> ← <b>{fmt_toman(p.price_irt)}</b> ({fmt_int(off)}٪ تخفیف)"
    return f"<b>{fmt_toman(p.price_irt)}</b>"


def order_text(order: Order, items: list[OrderItem], *, owner: bool = False) -> str:
    lines = [f"🧾 <b>سفارش #{order.id}</b> — {STATUS_LABEL.get(order.status, order.status)}", f"📅 {jdate(order.created_at, True)}", ""]
    for it in items:
        lines.append(f"• {esc(it.title)} × {fmt_int(it.qty)} — {fmt_toman(it.unit_price * it.qty)}")
    lines.append("")
    lines.append(f"جمع کالاها: {fmt_toman(order.subtotal)}")
    if order.needs_shipping:
        lines.append(f"هزینه ارسال: {fmt_toman(order.shipping) if order.shipping else 'رایگان'}")
    lines.append(f"💵 <b>مبلغ کل: {fmt_toman(order.total)}</b>")
    if order.needs_shipping and order.name:
        lines += ["", f"👤 {esc(order.name)} · 📞 {esc(order.phone)}", f"📍 {esc(order.address)}"]
    if order.tracking:
        lines.append(f"\n🔎 کد رهگیری: <code>{esc(order.tracking)}</code>")
    if owner and order.flag == "oversold":
        lines.append("\n⚠️ این سفارش بعد از آزاد شدن موجودی پرداخت شده؛ موجودی را بررسی کنید.")
    return "\n".join(lines)


class ShopService:
    def __init__(self, app: App) -> None:
        self.app = app
        self._last_gc: datetime | None = None

    # ------------------------------------------------------------------ cart
    async def cart_lines(self, bot_id: int, user_id: int) -> list[Line]:
        async with self.app.db.session() as s:
            rows = (
                await s.execute(
                    select(CartItem.qty, Product)
                    .join(Product, Product.id == CartItem.product_id)
                    .where(CartItem.bot_id == bot_id, CartItem.user_id == user_id, Product.bot_id == bot_id, Product.is_active.is_(True))
                    .order_by(CartItem.updated_at)
                )
            ).all()
        return [Line(p, qty) for qty, p in rows]

    async def cart_count(self, bot_id: int, user_id: int) -> int:
        async with self.app.db.session() as s:
            return int(await s.scalar(select(func.coalesce(func.sum(CartItem.qty), 0)).where(CartItem.bot_id == bot_id, CartItem.user_id == user_id)) or 0)

    @staticmethod
    def _cap(product: Product) -> int:
        return MAX_QTY if product.stock is None else max(0, min(MAX_QTY, product.stock))

    async def change_qty(self, bot_id: int, user_id: int, product_id: int, delta: int) -> str:
        """delta>0 add, <0 remove one, 0 delete the line. Returns ok|soldout|max|missing."""
        async with self.app.db.session() as s:
            product = await s.get(Product, product_id)
            if product is None or product.bot_id != bot_id or not product.is_active:
                return "missing"
            item = await s.get(CartItem, (bot_id, user_id, product_id))
            now = utcnow()
            if delta == 0:
                if item is not None:
                    await s.delete(item)
                await s.commit()
                return "ok"
            cap = self._cap(product)
            qty = (item.qty if item else 0) + delta
            if delta > 0 and cap <= 0:
                return "soldout"
            if delta > 0 and qty > cap:
                return "max"
            if qty <= 0:
                if item is not None:
                    await s.delete(item)
            elif item is None:
                s.add(CartItem(bot_id=bot_id, user_id=user_id, product_id=product_id, qty=qty, updated_at=now))
            else:
                item.qty, item.updated_at = qty, now
            await s.commit()
        return "ok"

    async def clear_cart(self, bot_id: int, user_id: int) -> None:
        async with self.app.db.session() as s:
            await s.execute(delete(CartItem).where(CartItem.bot_id == bot_id, CartItem.user_id == user_id))
            await s.commit()

    # ------------------------------------------------------------------ orders
    async def create_order(self, child: ChildBot, user_id: int, *, name: str, phone: str, address: str) -> Order:
        """Turn the cart into a pending order, reserving stock atomically (all or nothing)."""
        async with self.app.db.session() as s:
            rows = (
                await s.execute(
                    select(CartItem.qty, Product)
                    .join(Product, Product.id == CartItem.product_id)
                    .where(CartItem.bot_id == child.id, CartItem.user_id == user_id, Product.bot_id == child.id)
                    .order_by(CartItem.updated_at)
                )
            ).all()
            lines = [Line(p, qty) for qty, p in rows]
            if not lines:
                raise ShopError("سبد خرید شما خالی است.")
            for line in lines:
                if not line.product.is_active:
                    raise ShopError(f"«{line.product.title}» دیگر موجود نیست. آن را از سبد حذف کنید.")
                res = await s.execute(
                    update(Product)
                    .where(
                        Product.id == line.product.id,
                        Product.is_active.is_(True),
                        or_(Product.stock.is_(None), Product.stock >= line.qty),
                    )
                    .values(stock=Product.stock - line.qty)  # NULL (unlimited) stays NULL
                )
                if res.rowcount != 1:
                    title = line.product.title  # read before rollback expires the instance
                    await s.rollback()
                    raise ShopError(f"موجودی «{title}» کافی نیست. تعداد را کم کنید یا آن را از سبد حذف کنید.")
            q = quote(child, lines)
            order = Order(
                bot_id=child.id,
                user_id=user_id,
                subtotal=q.subtotal,
                shipping=q.shipping,
                total=q.total,
                needs_shipping=q.needs_shipping,
                name=name if q.needs_shipping else "",
                phone=phone if q.needs_shipping else "",
                address=address if q.needs_shipping else "",
            )
            s.add(order)
            await s.flush()
            for line in lines:
                s.add(OrderItem(order_id=order.id, product_id=line.product.id, title=line.product.title, unit_price=line.product.price_irt, qty=line.qty))
            await s.execute(delete(CartItem).where(CartItem.bot_id == child.id, CartItem.user_id == user_id))
            await s.commit()
        return order

    async def get_order(self, order_id: int) -> tuple[Order | None, list[OrderItem]]:
        async with self.app.db.session() as s:
            order = await s.get(Order, order_id)
            items = list((await s.scalars(select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.id))).all()) if order else []
        return order, items

    async def last_address(self, bot_id: int, user_id: int) -> tuple[str, str, str] | None:
        async with self.app.db.session() as s:
            row = (
                await s.execute(
                    select(Order.name, Order.phone, Order.address)
                    .where(Order.bot_id == bot_id, Order.user_id == user_id, Order.address != "")
                    .order_by(Order.id.desc())
                    .limit(1)
                )
            ).first()
        return (row[0], row[1], row[2]) if row else None

    async def _restock(self, s, order_id: int) -> None:
        items = (await s.scalars(select(OrderItem).where(OrderItem.order_id == order_id))).all()
        for it in items:
            await s.execute(update(Product).where(Product.id == it.product_id).values(stock=Product.stock + it.qty))

    async def cancel(self, order_id: int, *, allowed: tuple[str, ...] = ("pending",)) -> bool:
        """Cancel and release stock — exactly once (conditional status change)."""
        async with self.app.db.session() as s:
            res = await s.execute(update(Order).where(Order.id == order_id, Order.status.in_(allowed)).values(status="cancelled"))
            if res.rowcount != 1:
                await s.rollback()
                return False
            await self._restock(s, order_id)
            await s.commit()
        return True

    # ------------------------------------------------------------------ payment hooks
    async def mark_paid(self, s, payment: Payment) -> None:
        """Called inside ``PaymentService.fulfill``'s transaction for ``kind == 'order'``."""
        if not payment.order_id:
            return
        order = await s.get(Order, payment.order_id)
        if order is None or order.bot_id != payment.bot_id:
            return
        was = order.status
        res = await s.execute(
            update(Order).where(Order.id == order.id, Order.status.in_(("pending", "cancelled"))).values(status="paid", paid_at=utcnow())
        )
        if res.rowcount != 1:
            return
        items = (await s.scalars(select(OrderItem).where(OrderItem.order_id == order.id))).all()
        if was == "cancelled":  # the stock was released while the buyer was still paying
            for it in items:
                r = await s.execute(
                    update(Product).where(Product.id == it.product_id, or_(Product.stock.is_(None), Product.stock >= it.qty)).values(stock=Product.stock - it.qty)
                )
                if r.rowcount != 1:
                    await s.execute(update(Order).where(Order.id == order.id).values(flag="oversold"))
        for it in items:
            await s.execute(update(Product).where(Product.id == it.product_id).values(sold=Product.sold + it.qty))

    async def after_paid(self, payment: Payment) -> None:
        """Buyer confirmation, digital delivery, owner notification."""
        from .delivery import ContentUnavailable, deliver

        order, items = await self.get_order(payment.order_id or 0)
        if order is None:
            return
        async with self.app.db.session() as s:
            child = await s.get(ChildBot, order.bot_id)
            pids = [it.product_id for it in items]
            products = {p.id: p for p in (await s.scalars(select(Product).where(Product.id.in_(pids)))).all()} if pids else {}
            content_ids = [p.content_id for p in products.values() if p.content_id]
            contents = {c.id: c for c in (await s.scalars(select(Content).where(Content.id.in_(content_ids)))).all()} if content_ids else {}
        bot = self.app.manager.get_bot(order.bot_id) if child else None
        if bot is None or child is None:
            return

        try:
            await bot.send_message(
                order.user_id,
                f"✅ پرداخت سفارش #{order.id} انجام شد.\n\n"
                + ("سفارش شما آماده‌سازی و ارسال می‌شود؛ هر تغییر وضعیت را همین‌جا اطلاع می‌دهیم." if order.needs_shipping else "فایل‌های دیجیتال شما در ادامه ارسال می‌شود."),
            )
            failed = False
            for it in items:
                cid = products[it.product_id].content_id if it.product_id in products else None
                if cid and cid in contents:
                    try:
                        await deliver(self.app, bot, child, contents[cid], order.user_id, order.user_id)
                    except ContentUnavailable:
                        failed = True
            if failed:
                await bot.send_message(order.user_id, "⚠️ ارسال بعضی فایل‌ها با مشکل روبه‌رو شد؛ با مدیر ربات تماس بگیرید.")
        except TelegramAPIError as exc:
            log.warning("cannot message order buyer %s: %s", order.user_id, exc)

        if not order.needs_shipping:
            async with self.app.db.session() as s:
                await s.execute(update(Order).where(Order.id == order.id, Order.status == "paid").values(status="delivered"))
                await s.commit()
            order, items = await self.get_order(order.id)
            assert order is not None

        markup = (
            kb([btn("📦 ارسال شد", "saos", order.id), btn("❌ لغو سفارش", "saoc", order.id)])
            if order.needs_shipping
            else None
        )
        try:
            await bot.send_message(child.owner_id, "🛍 <b>سفارش جدید</b>\n\n" + order_text(order, items, owner=True), reply_markup=markup)
        except TelegramAPIError as exc:
            log.warning("cannot notify shop owner of bot %s: %s", child.id, exc)
            await self.app.notify_creator(child.owner_id, f"سفارش جدیدی در @{esc(child.username)} ثبت شد؛ داخل خود ربات /start بزنید.")
        for it in items:
            p = products.get(it.product_id)
            if p is not None and p.stock is not None and p.stock <= 0:
                with suppress(TelegramAPIError):
                    await bot.send_message(child.owner_id, f"📉 موجودی «{esc(p.title)}» تمام شد.")

    # ------------------------------------------------------------------ housekeeping
    async def gc(self, now: datetime | None = None, *, force: bool = False) -> dict[str, int]:
        """Release stale unpaid orders and old carts. Rate-limited to once per ``GC_EVERY``."""
        now = now or utcnow()
        if not force and self._last_gc and now - self._last_gc < GC_EVERY:
            return {"orders": 0, "carts": 0}
        self._last_gc = now
        awaiting = (
            select(Payment.id)
            .where(
                Payment.order_id == Order.id,
                or_(
                    Payment.status == "review",
                    and_(Payment.method == "manual", Payment.status == "pending", Payment.created_at > now - MANUAL_HOLD),
                ),
            )
            .exists()
        )
        async with self.app.db.session() as s:
            stale = list(
                (await s.scalars(select(Order.id).where(Order.status == "pending", Order.created_at < now - UNPAID_HOLD, ~awaiting).limit(200))).all()
            )
        released = 0
        for oid in stale:
            if await self.cancel(oid):
                released += 1
        async with self.app.db.session() as s:
            res = await s.execute(delete(CartItem).where(CartItem.updated_at < now - CART_TTL))
            await s.commit()
        return {"orders": released, "carts": res.rowcount or 0}

