"""Bot-level operations shared by the mother bot handlers."""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    BotUser,
    CartItem,
    Category,
    ChildBot,
    Content,
    Delivery,
    Order,
    OrderItem,
    Payment,
    Plan,
    Product,
    Purchase,
    ScheduledDelete,
    ShopCategory,
    Sponsor,
    Subscription,
)

_BOT_SCOPED = (CartItem, ShopCategory, Product, Sponsor, Category, Content, Plan, Subscription, Purchase, Payment, Delivery, ScheduledDelete, BotUser)


async def purge_bot(session: AsyncSession, bot_id: int) -> None:
    """Delete a child bot and everything that belongs to it."""
    order_ids = select(Order.id).where(Order.bot_id == bot_id)
    await session.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
    await session.execute(delete(Order).where(Order.bot_id == bot_id))
    for model in _BOT_SCOPED:
        await session.execute(delete(model).where(model.bot_id == bot_id))
    await session.execute(delete(ChildBot).where(ChildBot.id == bot_id))


async def bot_counts(session: AsyncSession, bot_id: int) -> dict[str, int]:
    async def count(model) -> int:
        return int(await session.scalar(select(func.count()).select_from(model).where(model.bot_id == bot_id)) or 0)

    return {
        "users": await count(BotUser),
        "contents": await count(Content),
        "deliveries": await count(Delivery),
        "sponsors": await count(Sponsor),
        "plans": await count(Plan),
    }
