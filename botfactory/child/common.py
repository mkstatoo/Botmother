"""Shared helpers for the child-bot handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.filters import BaseFilter
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, select

from ..db.models import ChildBot, Content, Plan
from ..ui import btn, kb
from ..util import esc, fmt_toman, fmt_usd

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

PAGE = 8


class IsOwner(BaseFilter):
    async def __call__(self, event: object, is_owner: bool = False) -> bool:
        return is_owner


def deep_link(child: ChildBot, content_id: int) -> str:
    return f"https://t.me/{child.username}?start=c_{content_id}"


def trunc(text: str, n: int = 40) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def price_text(price_irt: int | None, price_usd_cents: int | None) -> str:
    parts = []
    if price_irt:
        parts.append(fmt_toman(price_irt))
    if price_usd_cents:
        parts.append(fmt_usd(price_usd_cents))
    return " / ".join(parts)


def welcome_text(child: ChildBot) -> str:
    if child.welcome_text:
        return esc(child.welcome_text)
    name = esc(child.name or child.username)
    return (
        f"👋 به ربات <b>{name}</b> خوش آمدید!\n\n"
        "برای دیدن محتوا روی «📚 محتوا» بزنید، یا نام محتوا را بنویسید تا جستجو شود."
    )


async def home_markup(app: App, child: ChildBot, is_owner: bool, user_id: int | None = None) -> InlineKeyboardMarkup:
    """One round-trip: counts of active plans / contents / cart items."""
    plans_q = select(func.count()).select_from(Plan).where(Plan.bot_id == child.id, Plan.is_active.is_(True)).scalar_subquery()
    contents_q = select(func.count()).select_from(Content).where(Content.bot_id == child.id, Content.is_active.is_(True)).scalar_subquery()
    async with app.db.session() as s:
        plans, contents = (await s.execute(select(plans_q, contents_q))).one()
    rows = []
    if child.shop_enabled:
        cart = await app.shop.cart_count(child.id, user_id) if user_id else 0
        rows.append([btn("🛍 فروشگاه", "shop"), btn(f"🛒 سبد خرید{f' ({cart})' if cart else ''}", "shcart")])
        rows.append([btn("📦 سفارش‌های من", "shmo", 0)])
    if contents or not child.shop_enabled:
        rows.append([btn("📚 محتوا", "list", 0, "c")])
    second = []
    if plans:
        second.append(btn("💎 اشتراک", "plans"))
    second.append(btn("👤 حساب من", "me"))
    rows.append(second)
    if is_owner:
        rows.append([btn("⚙️ پنل مدیریت", "panel")])
    return kb(*rows)
