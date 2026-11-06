"""Shop module: products, cart, checkout, stock, payments, order lifecycle."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import func, select

from botfactory.db import CartItem, ChildBot, Order, OrderItem, Payment, Product, ShopCategory
from botfactory.services.billing import Billing  # noqa: F401  (import check)
from botfactory.services.botops import purge_bot
from botfactory.services.shop import ShopError, quote
from botfactory.util import utcnow

from .fake_tg import msg_update
from .test_flows import BOT, MERCHANT, OWNER, USER, add_content, buttons, configure_zarinpal, db_all, db_one, has_text, pack

USER2 = 701
PHOTO = [{"file_id": "PH1", "file_unique_id": "u", "width": 10, "height": 10}]
CARD = "6037 9971 2345 6789"


async def shop_bot(env, **fields):
    child = await env.make_child(BOT, owner=OWNER, shop_enabled=True, card_number=CARD, **fields)
    return child


async def new_product(env, title="کفش", price=100000, stock=5, **fields) -> Product:
    async with env.app.db.session() as s:
        p = Product(bot_id=BOT, title=title, price_irt=price, stock=stock, **fields)
        s.add(p)
        await s.commit()
    return p


async def get_product(env, pid) -> Product:
    return await db_one(env, select(Product).where(Product.id == pid))


async def last_order(env) -> Order:
    return await db_one(env, select(Order).order_by(Order.id.desc()))


async def checkout(env, uid=USER, name="علی رضایی", phone="۰۹۱۲۳۴۵۶۷۸۹", address="مشهد، بلوار وکیل‌آباد، پلاک ۱۲"):
    await env.tap(BOT, uid, pack("sho"))
    await env.say(BOT, uid, name)
    await env.say(BOT, uid, phone)
    await env.say(BOT, uid, address)
    return await last_order(env)


# ------------------------------------------------------------------------------ owner side
async def test_owner_adds_product_through_the_flow(env):
    await env.make_child(BOT, owner=OWNER)
    await env.tap(BOT, OWNER, pack("sat"))  # enable
    assert (await db_one(env, select(ChildBot))).shop_enabled
    await env.tap(BOT, OWNER, pack("sapa"))
    await env.say(BOT, OWNER, "کفش ورزشی")
    await env.say(BOT, OWNER, "abc")  # rejected
    await env.say(BOT, OWNER, "۴۵۰٬۰۰۰")
    await env.say(BOT, OWNER, "۱۲")
    await env.feed(BOT, msg_update(OWNER, None, photo=PHOTO))
    await env.say(BOT, OWNER, "سایز ۴۲ تا ۴۵")
    p = await db_one(env, select(Product))
    assert (p.title, p.price_irt, p.stock, p.photo_file_id, p.description, p.is_active, p.needs_shipping) == (
        "کفش ورزشی", 450000, 12, "PH1", "سایز ۴۲ تا ۴۵", True, True,
    )
    assert has_text(env, BOT, "کالا ثبت شد", OWNER)

    # edits
    await env.tap(BOT, OWNER, pack("sape", p.id, "price"))
    await env.say(BOT, OWNER, "400000")
    await env.tap(BOT, OWNER, pack("sape", p.id, "compare"))
    await env.say(BOT, OWNER, "300000")  # must exceed the price → rejected
    await env.say(BOT, OWNER, "500000")
    await env.tap(BOT, OWNER, pack("sape", p.id, "stock"))
    await env.say(BOT, OWNER, "-")
    p = await get_product(env, p.id)
    assert (p.price_irt, p.compare_price_irt, p.stock) == (400000, 500000, None)
    await env.tap(BOT, OWNER, pack("sapt", p.id))
    assert not (await get_product(env, p.id)).is_active


async def test_owner_skips_optional_steps_and_categories(env):
    await env.make_child(BOT, owner=OWNER)
    await env.tap(BOT, OWNER, pack("sacn"))
    await env.say(BOT, OWNER, "کفش")
    cat = await db_one(env, select(ShopCategory))
    await env.tap(BOT, OWNER, pack("sapa"))
    await env.say(BOT, OWNER, "صندل")
    await env.say(BOT, OWNER, "90000")
    await env.say(BOT, OWNER, "0")  # zero stock is valid (sold out)
    await env.tap(BOT, OWNER, pack("sapskip", 0, "photo"))
    await env.tap(BOT, OWNER, pack("sapskip", 0, "desc"))
    p = await db_one(env, select(Product))
    assert (p.stock, p.photo_file_id, p.description) == (0, None, None)
    await env.tap(BOT, OWNER, pack("sapcs", p.id, str(cat.id)))
    assert (await get_product(env, p.id)).category_id == cat.id
    await env.tap(BOT, OWNER, pack("sacd", cat.id))  # deleting a category keeps products
    assert (await get_product(env, p.id)).category_id is None


async def test_shop_screens_are_owner_only(env):
    await shop_bot(env)
    for act in ("sam", "sapa", "sapl", "sac", "sas", "sao", "sat"):
        await env.tap(BOT, USER, pack(act))
    assert not await db_all(env, select(Product))
    assert (await db_one(env, select(ChildBot))).shop_enabled  # 'sat' by a stranger changed nothing
    p = await new_product(env)
    await env.tap(BOT, USER, pack("sapdy", p.id))
    await env.tap(BOT, USER, pack("sape", p.id, "price"))
    await env.say(BOT, USER, "1")
    assert (await get_product(env, p.id)).price_irt == 100000


async def test_owner_cannot_touch_another_bots_data(env):
    await shop_bot(env)
    await env.make_child(20002, owner=OWNER + 1)
    async with env.app.db.session() as s:
        other = Product(bot_id=20002, title="foreign", price_irt=5, stock=1)
        s.add(other)
        await s.commit()
    await env.tap(BOT, OWNER, pack("sapdy", other.id))
    await env.tap(BOT, OWNER, pack("sapt", other.id))
    assert (await get_product(env, other.id)).is_active
    await env.tap(BOT, USER, pack("sha", other.id))  # a buyer of bot 1 can't add bot 2's product
    assert not await db_all(env, select(CartItem))


# ------------------------------------------------------------------------------ browsing
async def test_buyer_browses_and_sees_product_page(env):
    await shop_bot(env)
    cat = ShopCategory(bot_id=BOT, name="کفش")
    async with env.app.db.session() as s:
        s.add(cat)
        await s.commit()
    p = await new_product(env, price=300000, compare_price_irt=400000, stock=3, photo_file_id="PH1", description="راحت و سبک", category_id=cat.id)
    hidden = await new_product(env, title="مخفی", is_active=False)

    await env.say(BOT, USER, "/start")
    labels = [b["text"] for b in buttons(env.fake.last("SendMessage", BOT))]
    assert any("فروشگاه" in t for t in labels) and any("سبد" in t for t in labels)
    assert not any("محتوا" in t for t in labels)  # a pure shop doesn't show the empty content section

    await env.tap(BOT, USER, pack("shop"))
    await env.tap(BOT, USER, pack("shl", cat.id, "0"))
    items = [b for b in buttons(env.fake.last("EditMessageText", BOT)) if b.get("callback_data", "").startswith(pack("shp", p.id)[:6])]
    assert len(items) == 1 and "کفش" in items[0]["text"]
    assert all(pack("shp", hidden.id, "0") != b.get("callback_data") for b in buttons(env.fake.last("EditMessageText", BOT)))

    await env.tap(BOT, USER, pack("shp", p.id, str(cat.id)))
    photo = env.fake.last("SendPhoto", BOT)
    assert photo.data["photo"] == "PH1"
    cap = photo.data["caption"]
    assert "راحت و سبک" in cap and "تخفیف" in cap and "فقط ۳ عدد" in cap

    await env.tap(BOT, USER, pack("shp", hidden.id, "0"))
    assert has_text(env, BOT, "در دسترس نیست", USER)


async def test_search_includes_products_and_deep_link(env):
    await shop_bot(env)
    p = await new_product(env, title="کوله پشتی")
    await env.say(BOT, USER, "کوله")
    res = [b["callback_data"] for b in buttons(env.fake.last("SendMessage", BOT))]
    assert pack("shp", p.id, "0") in res
    env.fake.clear()
    await env.say(BOT, USER, f"/start s_{p.id}")
    assert has_text(env, BOT, "کوله پشتی", USER)


async def test_shop_disabled_hides_everything(env):
    await env.make_child(BOT, owner=OWNER, card_number=CARD)
    p = await new_product(env)
    await env.tap(BOT, USER, pack("sha", p.id))
    await env.tap(BOT, USER, pack("shop"))
    assert not await db_all(env, select(CartItem))
    await env.say(BOT, USER, "/start")
    assert not any("فروشگاه" in b["text"] for b in buttons(env.fake.last("SendMessage", BOT)))


# ------------------------------------------------------------------------------ cart
async def test_cart_quantities_limits_and_totals(env):
    await shop_bot(env, shop_shipping_irt=30000, shop_free_over_irt=500000)
    a = await new_product(env, title="A", price=100000, stock=2)
    b = await new_product(env, title="B", price=50000, stock=None)
    sold_out = await new_product(env, title="C", stock=0)

    for _ in range(3):  # the third add exceeds the stock of 2
        await env.tap(BOT, USER, pack("sha", a.id))
    assert (await db_one(env, select(CartItem.qty).where(CartItem.product_id == a.id))) == 2
    await env.tap(BOT, USER, pack("sha", sold_out.id))
    assert not await db_all(env, select(CartItem).where(CartItem.product_id == sold_out.id))
    await env.tap(BOT, USER, pack("sha", b.id))
    await env.tap(BOT, USER, pack("shcart"))
    text = env.fake.last("EditMessageText", BOT).data["text"]
    assert "۲۵۰٬۰۰۰ تومان" in text and "۳۰٬۰۰۰ تومان" in text and "۲۸۰٬۰۰۰ تومان" in text  # 2*100k + 50k, +30k shipping

    await env.tap(BOT, USER, pack("shq", b.id, "+"))
    await env.tap(BOT, USER, pack("shq", a.id, "-"))
    qty = {r.product_id: r.qty for r in await db_all(env, select(CartItem))}
    assert qty == {a.id: 1, b.id: 2}
    await env.tap(BOT, USER, pack("shq", a.id, "x"))
    await env.tap(BOT, USER, pack("shq", b.id, "-"))
    await env.tap(BOT, USER, pack("shq", b.id, "-"))
    assert not await db_all(env, select(CartItem))
    await env.tap(BOT, USER, pack("shcart"))
    assert "خالی" in env.fake.last("EditMessageText", BOT).data["text"]


async def test_quote_free_shipping_and_digital(env):
    child = await shop_bot(env, shop_shipping_irt=20000, shop_free_over_irt=300000)
    from botfactory.services.shop import Line

    phys = Product(id=1, bot_id=BOT, title="x", price_irt=100000, needs_shipping=True)
    dig = Product(id=2, bot_id=BOT, title="d", price_irt=50000, needs_shipping=False)
    async with env.app.db.session() as s:
        child = await s.get(ChildBot, BOT)
    assert quote(child, [Line(phys, 1)]).shipping == 20000
    assert quote(child, [Line(phys, 3)]).shipping == 0  # reaches the free-shipping threshold
    q = quote(child, [Line(dig, 1)])
    assert (q.shipping, q.needs_shipping, q.total) == (0, False, 50000)


# ------------------------------------------------------------------------------ checkout + manual payment
async def test_full_order_lifecycle_with_card_to_card(env):
    await shop_bot(env, shop_shipping_irt=25000)
    p = await new_product(env, price=100000, stock=5)
    await env.tap(BOT, USER, pack("sha", p.id))
    await env.tap(BOT, USER, pack("sha", p.id))

    await env.tap(BOT, USER, pack("sho"))
    await env.say(BOT, USER, "ab")  # too short
    await env.say(BOT, USER, "علی رضایی")
    await env.say(BOT, USER, "12")  # bad phone
    await env.say(BOT, USER, "۰۹۱۲۳۴۵۶۷۸۹")
    await env.say(BOT, USER, "کوتاه")  # bad address
    await env.say(BOT, USER, "مشهد، بلوار وکیل‌آباد، پلاک ۱۲")

    order = await last_order(env)
    assert (order.status, order.subtotal, order.shipping, order.total, order.needs_shipping) == ("pending", 200000, 25000, 225000, True)
    assert (order.name, order.phone) == ("علی رضایی", "09123456789")
    assert (await get_product(env, p.id)).stock == 3  # reserved
    assert not await db_all(env, select(CartItem))
    items = await db_all(env, select(OrderItem))
    assert [(i.title, i.unit_price, i.qty) for i in items] == [("کفش", 100000, 2)]
    acts = [b.get("callback_data") for b in buttons(env.fake.last("SendMessage", BOT))]
    assert pack("pay", order.id, "o.manual") in acts and pack("pay", order.id, "o.zarinpal") not in acts

    await env.tap(BOT, USER, pack("pay", order.id, "o.manual"))
    pay = await db_one(env, select(Payment))
    assert (pay.kind, pay.order_id, pay.amount, pay.currency, pay.method) == ("order", order.id, 225000, "IRT", "manual")
    assert has_text(env, BOT, CARD, USER)
    await env.feed(BOT, msg_update(USER, None, photo=PHOTO, caption="واریز شد"))
    await env.drain()
    review = env.fake.last("SendPhoto", BOT)
    assert review.data["chat_id"] == OWNER and "سفارش #" in review.data["caption"]

    await env.tap(BOT, USER, pack("mp", pay.id, "ok"))  # buyer can't approve
    assert (await last_order(env)).status == "pending"
    await env.tap(BOT, OWNER, pack("mp", pay.id, "ok"))
    await env.drain()
    order = await last_order(env)
    assert order.status == "paid" and order.paid_at is not None
    assert (await get_product(env, p.id)).sold == 2
    assert has_text(env, BOT, "پرداخت سفارش", USER)
    notif = [c for c in env.fake.find("SendMessage", BOT) if c.data.get("chat_id") == OWNER and "سفارش جدید" in c.data.get("text", "")]
    assert notif and "مشهد" in notif[0].data["text"]
    assert pack("saos", order.id) in [b["callback_data"] for b in buttons(notif[0])]

    await env.tap(BOT, OWNER, pack("mp", pay.id, "ok"))  # double approval changes nothing
    assert (await get_product(env, p.id)).sold == 2

    # owner ships with a tracking code
    await env.tap(BOT, OWNER, pack("saos", order.id))
    await env.say(BOT, OWNER, "RR123456789IR")
    order = await last_order(env)
    assert (order.status, order.tracking) == ("shipped", "RR123456789IR")
    assert has_text(env, BOT, "RR123456789IR", USER)
    await env.tap(BOT, OWNER, pack("saodl", order.id))
    assert (await last_order(env)).status == "delivered"
    assert has_text(env, BOT, "تحویل داده شد", USER)

    # buyer sees it, and the address is reusable next time
    await env.tap(BOT, USER, pack("shmo", 0))
    assert any(b["callback_data"] == pack("shod", order.id) for b in buttons(env.fake.last("EditMessageText", BOT)))
    await env.tap(BOT, USER, pack("sha", p.id))
    await env.tap(BOT, USER, pack("sho"))
    assert pack("shla") in [b.get("callback_data") for b in buttons(env.fake.last("EditMessageText", BOT))]
    await env.tap(BOT, USER, pack("shla"))
    second = await last_order(env)
    assert second.id != order.id and second.address == order.address and second.status == "pending"


async def test_orders_are_private_to_their_buyer(env):
    await shop_bot(env)
    p = await new_product(env)
    await env.tap(BOT, USER, pack("sha", p.id))
    order = await checkout(env)
    await env.tap(BOT, USER2, pack("shod", order.id))
    await env.tap(BOT, USER2, pack("shoc", order.id))
    await env.tap(BOT, USER2, pack("pay", order.id, "o.manual"))
    assert (await last_order(env)).status == "pending"
    assert not await db_all(env, select(Payment))


# ------------------------------------------------------------------------------ online payment
async def test_zarinpal_order_payment_is_idempotent(env):
    await shop_bot(env)
    await configure_zarinpal(env)
    p = await new_product(env, price=80000, stock=4)
    await env.tap(BOT, USER, pack("sha", p.id))
    order = await checkout(env)
    await env.tap(BOT, USER, pack("pay", order.id, "o.zarinpal"))
    pay = await db_one(env, select(Payment))
    assert env.gateway.requests[0]["amount"] == 800000 and env.gateway.requests[0]["merchant_id"] == MERCHANT

    r = await env.web.get(f"/pay/zarinpal/{pay.id}", params={"Authority": "BAD", "Status": "OK"})
    assert r.status == 400 and (await last_order(env)).status == "pending"
    for _ in range(2):
        r = await env.web.get(f"/pay/zarinpal/{pay.id}", params={"Authority": pay.authority, "Status": "OK"})
        assert r.status == 200
        await env.drain()
    order = await last_order(env)
    assert order.status == "paid"
    p = await get_product(env, p.id)
    assert (p.stock, p.sold) == (3, 1)
    assert len([c for c in env.fake.find("SendMessage", BOT) if "سفارش جدید" in c.data.get("text", "")]) == 1


# ------------------------------------------------------------------------------ stock
async def test_last_item_cannot_be_oversold(env):
    child = await shop_bot(env)
    p = await new_product(env, stock=1)
    await env.tap(BOT, USER, pack("sha", p.id))
    await env.tap(BOT, USER2, pack("sha", p.id))
    results = await asyncio.gather(
        *[env.app.shop.create_order(child, uid, name="n", phone="09120000000", address="addr addr addr") for uid in (USER, USER2)],
        return_exceptions=True,
    )
    assert sum(isinstance(r, Order) for r in results) == 1
    assert sum(isinstance(r, ShopError) for r in results) == 1
    assert (await get_product(env, p.id)).stock == 0
    assert len(await db_all(env, select(Order))) == 1


async def test_failed_checkout_reserves_nothing(env):
    child = await shop_bot(env)
    a = await new_product(env, title="A", stock=5)
    b = await new_product(env, title="B", stock=1)
    await env.tap(BOT, USER, pack("sha", a.id))
    await env.tap(BOT, USER, pack("sha", b.id))
    async with env.app.db.session() as s:  # someone bought B meanwhile
        (await s.get(Product, b.id)).stock = 0
        await s.commit()
    try:
        await env.app.shop.create_order(child, USER, name="n", phone="09120000000", address="addr addr addr")
        raise AssertionError("expected ShopError")
    except ShopError as exc:
        assert "B" in str(exc)
    assert (await get_product(env, a.id)).stock == 5  # A's reservation was rolled back
    assert not await db_all(env, select(Order))
    assert len(await db_all(env, select(CartItem))) == 2  # cart kept so the buyer can adjust


async def test_cancelling_releases_stock_exactly_once(env):
    await shop_bot(env)
    p = await new_product(env, stock=5)
    await env.tap(BOT, USER, pack("sha", p.id))
    await env.tap(BOT, USER, pack("sha", p.id))
    order = await checkout(env)
    assert (await get_product(env, p.id)).stock == 3
    await env.tap(BOT, USER, pack("shoc", order.id))
    await env.tap(BOT, USER, pack("shoc", order.id))
    assert (await last_order(env)).status == "cancelled"
    assert (await get_product(env, p.id)).stock == 5


async def test_unpaid_orders_expire_but_receipts_under_review_are_kept(env):
    await shop_bot(env)
    p = await new_product(env, stock=10)
    orders = []
    for uid in (USER, USER2, 702):
        await env.tap(BOT, uid, pack("sha", p.id))
        orders.append(await checkout(env, uid))
    abandoned, reviewing, fresh = orders
    await env.tap(BOT, USER2, pack("pay", reviewing.id, "o.manual"))
    await env.say(BOT, USER2, "شماره پیگیری ۱۲۳")
    assert (await get_product(env, p.id)).stock == 7

    async with env.app.db.session() as s:
        for o in (abandoned, reviewing):
            (await s.get(Order, o.id)).created_at = utcnow() - timedelta(hours=2)
        await s.commit()
    stats = await env.app.shop.gc(force=True)
    assert stats["orders"] == 1
    statuses = {o.id: o.status for o in await db_all(env, select(Order))}
    assert statuses == {abandoned.id: "cancelled", reviewing.id: "pending", fresh.id: "pending"}
    assert (await get_product(env, p.id)).stock == 8
    assert (await env.app.shop.gc(force=True))["orders"] == 0  # idempotent


async def test_late_payment_revives_an_expired_order(env):
    await shop_bot(env)
    p = await new_product(env, stock=1)
    await env.tap(BOT, USER, pack("sha", p.id))
    order = await checkout(env)
    await env.tap(BOT, USER, pack("pay", order.id, "o.manual"))
    pay = await db_one(env, select(Payment))
    await env.app.shop.cancel(order.id)  # stock released while the buyer was transferring money
    await env.tap(BOT, USER2, pack("sha", p.id))
    other = await checkout(env, USER2)  # someone else took the last unit
    assert (await get_product(env, p.id)).stock == 0
    await env.say(BOT, USER, "رسید")
    await env.tap(BOT, OWNER, pack("mp", pay.id, "ok"))
    await env.drain()
    revived = await db_one(env, select(Order).where(Order.id == order.id))
    assert (revived.status, revived.flag) == ("paid", "oversold")
    assert has_text(env, BOT, "بعد از آزاد شدن موجودی", OWNER)
    assert other.status == "pending"


async def test_carts_expire_after_a_month(env):
    await shop_bot(env)
    p = await new_product(env)
    await env.tap(BOT, USER, pack("sha", p.id))
    async with env.app.db.session() as s:
        (await s.scalar(select(CartItem))).updated_at = utcnow() - timedelta(days=31)
        await s.commit()
    assert (await env.app.shop.gc(force=True))["carts"] == 1


# ------------------------------------------------------------------------------ digital goods + owner order actions
async def test_digital_product_is_delivered_after_payment(env):
    await shop_bot(env, shop_shipping_irt=30000)
    cid = await add_content(env, title="دوره آموزشی", file_id="FID-COURSE")
    p = await new_product(env, title="دوره", price=200000, stock=None)
    await env.tap(BOT, OWNER, pack("sapcf", p.id, str(cid)))
    p = await get_product(env, p.id)
    assert p.content_id == cid and p.needs_shipping is False

    await env.tap(BOT, USER, pack("sha", p.id))
    await env.tap(BOT, USER, pack("sho"))  # no address questions for digital goods
    order = await last_order(env)
    assert (order.shipping, order.total, order.needs_shipping, order.address) == (0, 200000, False, "")
    await env.tap(BOT, USER, pack("pay", order.id, "o.manual"))
    await env.say(BOT, USER, "رسید")
    pay = await db_one(env, select(Payment))
    await env.tap(BOT, OWNER, pack("mp", pay.id, "ok"))
    await env.drain()
    assert env.fake.find("SendAudio", BOT, chat_id=USER) or env.fake.find("SendDocument", BOT, chat_id=USER)
    assert (await last_order(env)).status == "delivered"
    assert (await get_product(env, p.id)).stock is None  # unlimited stock stays unlimited


async def test_owner_can_cancel_a_paid_order_and_buyer_is_told(env):
    await shop_bot(env)
    p = await new_product(env, stock=4)
    await env.tap(BOT, USER, pack("sha", p.id))
    order = await checkout(env)
    await env.tap(BOT, USER, pack("pay", order.id, "o.manual"))
    await env.say(BOT, USER, "رسید")
    pay = await db_one(env, select(Payment))
    await env.tap(BOT, OWNER, pack("mp", pay.id, "ok"))
    await env.drain()
    assert (await get_product(env, p.id)).stock == 3

    await env.tap(BOT, OWNER, pack("sao", 0, "paid"))
    assert any(b["callback_data"] == pack("saod", order.id) for b in buttons(env.fake.last("EditMessageText", BOT)))
    await env.tap(BOT, OWNER, pack("saoc", order.id))
    await env.tap(BOT, OWNER, pack("saocy", order.id))
    assert (await last_order(env)).status == "cancelled"
    assert (await get_product(env, p.id)).stock == 4
    assert has_text(env, BOT, "بازگردانده می‌شود", USER)
    await env.tap(BOT, OWNER, pack("saocy", order.id))  # repeat is harmless
    assert (await get_product(env, p.id)).stock == 4
    await env.tap(BOT, OWNER, pack("saos", order.id))  # a cancelled order cannot be shipped
    await env.say(BOT, OWNER, "TRACK")
    assert (await last_order(env)).status == "cancelled"


# ------------------------------------------------------------------------------ housekeeping
async def test_deleting_a_product_clears_carts_but_keeps_order_history(env):
    await shop_bot(env)
    p = await new_product(env)
    q = await new_product(env, title="دیگر")
    await env.tap(BOT, USER, pack("sha", p.id))
    order = await checkout(env)
    await env.tap(BOT, USER2, pack("sha", p.id))
    await env.tap(BOT, USER2, pack("sha", q.id))
    await env.tap(BOT, OWNER, pack("sapdy", p.id))
    assert await db_one(env, select(func.count()).select_from(Product)) == 1
    assert [c.product_id for c in await db_all(env, select(CartItem))] == [q.id]
    items = await db_all(env, select(OrderItem).where(OrderItem.order_id == order.id))
    assert items and items[0].title == "کفش"


async def test_deleting_a_bot_removes_its_shop_data(env):
    await shop_bot(env)
    p = await new_product(env)
    await env.tap(BOT, USER, pack("sha", p.id))
    await checkout(env)
    await env.tap(BOT, USER2, pack("sha", p.id))
    async with env.app.db.session() as s:
        await purge_bot(s, BOT)
        await s.commit()
    for model in (Product, Order, OrderItem, CartItem):
        assert await db_one(env, select(func.count()).select_from(model)) == 0


async def test_shop_deep_link_and_mother_button(env):
    from .conftest import MOTHER_ID, token_for

    await env.tap(MOTHER_ID, OWNER, pack("new"))
    await env.say(MOTHER_ID, OWNER, token_for(BOT))
    urls = [b["url"] for b in buttons(env.fake.last("SendMessage", MOTHER_ID)) if "url" in b]
    assert f"https://t.me/bot{BOT}_bot?start=shop" in urls
    env.fake.clear()
    await env.say(BOT, OWNER, "/start shop")
    assert has_text(env, BOT, "مدیریت فروشگاه", OWNER)
    env.fake.clear()
    await env.say(BOT, USER, "/start shop")  # strangers get the normal home instead
    assert not has_text(env, BOT, "مدیریت فروشگاه", USER)
