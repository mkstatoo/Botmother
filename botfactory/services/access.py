"""Access rules: who may receive which content."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ChildBot, Content, Purchase, Sponsor, Subscription
from ..util import utcnow
from .sponsors import missing_sponsors


class Decision(str, Enum):
    OK = "ok"
    SPONSORS = "sponsors"
    PAYMENT = "payment"


@dataclass
class AccessResult:
    decision: Decision
    missing: list[Sponsor] = field(default_factory=list)


async def subscription_until(
    session: AsyncSession, bot_id: int, user_id: int, now: datetime | None = None
) -> datetime | None:
    """Expiry of the user's subscription, or None when there is no active one.

    Subscriptions are stacked contiguously (a new one starts when the previous ends),
    so ``max(expires_at) > now`` means the user is currently subscribed.
    """
    now = now or utcnow()
    latest = await session.scalar(
        select(func.max(Subscription.expires_at)).where(
            Subscription.bot_id == bot_id, Subscription.user_id == user_id
        )
    )
    return latest if latest and latest > now else None


async def has_purchased(session: AsyncSession, bot_id: int, user_id: int, content_id: int) -> bool:
    row = await session.scalar(
        select(Purchase.id).where(
            Purchase.bot_id == bot_id, Purchase.user_id == user_id, Purchase.content_id == content_id
        )
    )
    return row is not None


async def check_access(
    session: AsyncSession,
    bot: Bot,
    child: ChildBot,
    user_id: int,
    content: Content,
    *,
    is_owner: bool,
) -> AccessResult:
    if is_owner:
        return AccessResult(Decision.OK)

    subscribed = await subscription_until(session, child.id, user_id) is not None

    if content.access == "free":
        # subscribers skip the sponsor gate
        if child.sponsor_gate and not subscribed:
            sponsors = list(
                (await session.scalars(select(Sponsor).where(Sponsor.bot_id == child.id))).all()
            )
            if sponsors:
                missing, _unverifiable = await missing_sponsors(bot, sponsors, user_id)
                if missing:
                    return AccessResult(Decision.SPONSORS, missing)
        return AccessResult(Decision.OK)

    # premium
    if subscribed or await has_purchased(session, child.id, user_id, content.id):
        return AccessResult(Decision.OK)
    return AccessResult(Decision.PAYMENT)
