"""Database models (SQLAlchemy 2.0, async-friendly).

All datetimes are naive UTC. Money: ``IRT`` amounts are Toman (integer), ``USD`` amounts
are integer cents.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..util import utcnow


class Base(DeclarativeBase):
    pass


class Creator(Base):
    """A content provider who uses the mother bot."""

    __tablename__ = "creators"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(256), default="")
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ChildBot(Base):
    """A bot generated for a creator."""

    __tablename__ = "child_bots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(256), default="")
    token_enc: Mapped[str] = mapped_column(Text)
    # active | paused | suspended (by platform admin) | invalid (token revoked)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    webhook_secret: Mapped[str] = mapped_column(String(64))
    welcome_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # storage
    storage_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    default_storage: Mapped[str] = mapped_column(String(16), default="telegram")  # telegram|channel

    # behaviour
    sponsor_gate: Mapped[bool] = mapped_column(Boolean, default=True)
    protect_content: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_delete_seconds: Mapped[int] = mapped_column(Integer, default=0)

    # payment settings (secrets encrypted)
    zarinpal_merchant_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    nowpayments_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    nowpayments_ipn_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    card_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    card_holder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    manual_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BotUser(Base):
    """An end user of a child bot."""

    __tablename__ = "bot_users"

    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(256), default="")
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)  # user blocked the bot
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)  # owner banned the user


class Sponsor(Base):
    """A channel the user must join before receiving free content."""

    __tablename__ = "sponsors"
    __table_args__ = (UniqueConstraint("bot_id", "chat_id", name="uq_sponsor"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(256), default="")
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    invite_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Content(Base):
    __tablename__ = "contents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # audio | video | voice | document | video_note | link
    kind: Mapped[str] = mapped_column(String(16))
    # telegram | channel | external
    storage: Mapped[str] = mapped_column(String(16), default="telegram")
    file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_unique_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    storage_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    category_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    access: Mapped[str] = mapped_column(String(8), default="free")  # free | premium
    price_irt: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Toman
    price_usd_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    downloads: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Plan(Base):
    """Subscription plan: unlocks every premium content for ``days`` days."""

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(128))
    days: Mapped[int] = mapped_column(Integer)
    price_irt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_usd_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (Index("ix_sub_user", "bot_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    plan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class Purchase(Base):
    """One-off purchase of a single premium content."""

    __tablename__ = "purchases"
    __table_args__ = (UniqueConstraint("bot_id", "user_id", "content_id", name="uq_purchase"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    content_id: Mapped[int] = mapped_column(Integer)
    payment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(8))  # content | plan
    content_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    method: Mapped[str] = mapped_column(String(10))  # zarinpal | crypto | manual
    amount: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))  # IRT | USD
    # pending | review | paid | failed | rejected | expired
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)
    authority: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    receipt_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    receipt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (Index("ix_delivery_bot_time", "bot_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    content_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ScheduledDelete(Base):
    """Delivered messages that must be deleted later (auto-delete feature)."""

    __tablename__ = "scheduled_deletes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(Integer)
    delete_at: Mapped[datetime] = mapped_column(DateTime, index=True)
