"""Platform billing: free trial → paid service period for every child bot.

Rules
-----
* ``ChildBot.service_until`` is the end of the paid/trial period. ``None`` = exempt (unlimited).
* After the end there is a grace period (bot still works, owner is warned); after that the bot
  becomes ``expired`` (end users see "unavailable") until the creator renews.
* Enforcement is active only when billing is enabled **and** a price is configured, so a bot is
  never locked while there is no way to pay.
* Price / trial / grace / period / payment details live in ``platform_settings`` (editable from the
  admin panel); the environment only provides defaults.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError
from sqlalchemy import func, select, update

from ..db.models import ChildBot, PlatformSetting, ServicePayment
from ..ui import btn, kb
from ..util import esc, fmt_int, fmt_toman, jdate, utcnow
from .payments import CallbackResult, PaymentError

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger(__name__)

TICK_SECONDS = 600
PERIOD_CHOICES = (1, 3, 6, 12)
KEYS = ("billing_enabled", "trial_days", "grace_days", "price_irt", "period_days", "card_number", "card_holder", "zarinpal_merchant", "note")


@dataclass(frozen=True)
class Config:
    enabled: bool
    trial_days: int
    grace_days: int
    price_irt: int
    period_days: int
    card_number: str
    card_holder: str
    zarinpal_merchant: str
    note: str

    @property
    def enforced(self) -> bool:
        return self.enabled and self.price_irt > 0

    def price_for(self, periods: int) -> int:
        return self.price_irt * periods

    def days_for(self, periods: int) -> int:
        return self.period_days * periods


def days_left(until: datetime, now: datetime) -> int:
    return math.ceil((until - now).total_seconds() / 86400)


def phase(child: ChildBot, cfg: Config, now: datetime) -> str:
    """free | trial | active | grace | expired."""
    until = child.service_until
    if until is None:
        return "free"
    if now <= until:
        return "active" if child.service_paid else "trial"
    if now <= until + timedelta(days=cfg.grace_days):
        return "grace"
    return "expired"


def describe(child: ChildBot, cfg: Config, now: datetime) -> str:
    """Short Persian description of the service state for UI."""
    ph = phase(child, cfg, now)
    until = child.service_until
    if ph == "free" or until is None:
        return "♾ سرویس: نامحدود"
    if ph in ("trial", "active"):
        label = "🎁 دوره آزمایشی" if ph == "trial" else "✅ سرویس فعال"
        return f"{label} — {fmt_int(max(days_left(until, now), 0))} روز مانده (تا {jdate(until)})"
    if ph == "grace":
        end = until + timedelta(days=cfg.grace_days)
        return f"⚠️ سرویس منقضی شده؛ مهلت تمدید تا {jdate(end)}"
    return f"💤 سرویس منقضی شده (از {jdate(until)})"


class Billing:
    def __init__(self, app: App) -> None:
        self.app = app

    # ------------------------------------------------------------------ settings
    async def get_raw(self, key: str) -> str | None:
        async with self.app.db.session() as s:
            row = await s.get(PlatformSetting, key)
        return row.value if row else None

    async def set(self, key: str, value: str) -> None:
        if key not in KEYS:
            raise KeyError(key)
        async with self.app.db.session() as s:
            row = await s.get(PlatformSetting, key)
            if row is None:
                s.add(PlatformSetting(key=key, value=value))
            else:
                row.value = value
            await s.commit()

    async def config(self) -> Config:
        async with self.app.db.session() as s:
            rows = {r.key: r.value for r in (await s.scalars(select(PlatformSetting))).all()}
        st = self.app.settings

        def num(key: str, default: int, lo: int = 0) -> int:
            try:
                return max(lo, int(rows[key])) if key in rows else default
            except ValueError:
                return default

        merchant = ""
        if rows.get("zarinpal_merchant"):
            try:
                merchant = self.app.box.decrypt(rows["zarinpal_merchant"])
            except Exception:  # noqa: BLE001 - corrupted/rotated key: treat as unset
                merchant = ""
        enabled = rows["billing_enabled"] == "1" if "billing_enabled" in rows else st.billing_enabled
        return Config(
            enabled=enabled,
            trial_days=num("trial_days", st.service_trial_days),
            grace_days=num("grace_days", st.service_grace_days),
            price_irt=num("price_irt", st.service_price_irt),
            period_days=num("period_days", st.service_period_days, 1),
            card_number=rows.get("card_number", ""),
            card_holder=rows.get("card_holder", ""),
            zarinpal_merchant=merchant,
            note=rows.get("note", ""),
        )

    def methods(self, cfg: Config) -> list[str]:
        out = []
        if cfg.zarinpal_merchant and self.app.settings.payments_enabled:
            out.append("zarinpal")
        if cfg.card_number:
            out.append("manual")
        return out

    # ------------------------------------------------------------------ period maths
    async def trial_end(self, now: datetime | None = None) -> datetime:
        cfg = await self.config()
        return (now or utcnow()) + timedelta(days=cfg.trial_days)

    async def extend(self, bot_id: int, days: int, *, paid: bool = False) -> ChildBot | None:
        """Add ``days`` after the later of now / current end. Re-activates an expired bot."""
        now = utcnow()
        async with self.app.db.session() as s:
            child = await s.get(ChildBot, bot_id)
            if child is None:
                return None
            base = child.service_until if child.service_until and child.service_until > now else now
            child.service_until = base + timedelta(days=days)
            child.service_notice = 0
            if paid:
                child.service_paid = True
            if child.status == "expired":
                child.status = "active"
            await s.commit()
        return child

    async def set_unlimited(self, bot_id: int) -> ChildBot | None:
        async with self.app.db.session() as s:
            child = await s.get(ChildBot, bot_id)
            if child is None:
                return None
            child.service_until = None
            child.service_notice = 0
            if child.status == "expired":
                child.status = "active"
            await s.commit()
        return child

    async def expire_now(self, bot_id: int) -> ChildBot | None:
        """Admin: end the service immediately (no grace)."""
        cfg = await self.config()
        now = utcnow()
        async with self.app.db.session() as s:
            child = await s.get(ChildBot, bot_id)
            if child is None:
                return None
            child.service_until = now - timedelta(days=cfg.grace_days + 1)
            if child.status in ("active", "paused"):
                child.status = "expired"
            child.service_notice = 4
            await s.commit()
        return child

    async def backfill_legacy(self) -> int:
        """Bots created before billing existed get a fresh trial (once)."""
        end = await self.trial_end()
        async with self.app.db.session() as s:
            ids = list((await s.scalars(select(ChildBot.id).where(ChildBot.service_until.is_(None)))).all())
            marker = await s.get(PlatformSetting, "legacy_backfilled")
            if marker is not None or not ids:
                if marker is None:
                    s.add(PlatformSetting(key="legacy_backfilled", value="1"))
                    await s.commit()
                return 0
            await s.execute(update(ChildBot).where(ChildBot.id.in_(ids)).values(service_until=end, service_notice=0))
            s.add(PlatformSetting(key="legacy_backfilled", value="1"))
            await s.commit()
        return len(ids)

    # ------------------------------------------------------------------ periodic job
    async def tick(self, now: datetime | None = None) -> dict[str, int]:
        """Send reminders and expire lapsed bots. Idempotent; returns counters."""
        now = now or utcnow()
        cfg = await self.config()
        stats = {"reminded": 0, "expired": 0}
        if not cfg.enforced:
            return stats
        async with self.app.db.session() as s:
            rows = list(
                (
                    await s.scalars(
                        select(ChildBot).where(
                            ChildBot.service_until.is_not(None),
                            ChildBot.status.in_(("active", "paused", "expired")),
                        )
                    )
                ).all()
            )
        for child in rows:
            until = child.service_until
            assert until is not None
            left = until - now
            grace_end = until + timedelta(days=cfg.grace_days)
            if left > timedelta(days=3):
                continue
            if left > timedelta(days=1):
                stage = 1
            elif left >= timedelta(0):
                stage = 2
            elif now <= grace_end:
                stage = 3
            else:
                stage = 4
                async with self.app.db.session() as s:
                    res = await s.execute(
                        update(ChildBot)
                        .where(ChildBot.id == child.id, ChildBot.status.in_(("active", "paused")))
                        .values(status="expired")
                    )
                    await s.commit()
                if res.rowcount:
                    stats["expired"] += 1
            if await self._claim_stage(child.id, stage):
                stats["reminded"] += 1
                await self._remind(child, stage, cfg, now)
        return stats

    async def _claim_stage(self, bot_id: int, stage: int) -> bool:
        async with self.app.db.session() as s:
            res = await s.execute(
                update(ChildBot).where(ChildBot.id == bot_id, ChildBot.service_notice < stage).values(service_notice=stage)
            )
            await s.commit()
        return bool(res.rowcount)

    async def _remind(self, child: ChildBot, stage: int, cfg: Config, now: datetime) -> None:
        assert child.service_until is not None
        name = f"@{esc(child.username)}"
        if stage == 1:
            text = f"⏳ سرویس ربات {name} حدود {fmt_int(max(days_left(child.service_until, now), 0))} روز دیگر (تا {jdate(child.service_until)}) تمام می‌شود."
        elif stage == 2:
            text = f"⏳ کمتر از ۲۴ ساعت به پایان سرویس ربات {name} مانده است."
        elif stage == 3:
            end = child.service_until + timedelta(days=cfg.grace_days)
            text = (
                f"⚠️ سرویس ربات {name} منقضی شد. ربات تا {jdate(end)} هنوز کار می‌کند؛ "
                "بعد از آن برای کاربرانتان در دسترس نخواهد بود."
            )
        else:
            text = f"💤 سرویس ربات {name} منقضی شد و ربات برای کاربران غیرفعال است. با تمدید، بلافاصله فعال می‌شود."
        await self.app.notify_creator(child.owner_id, text, kb([btn("💳 تمدید سرویس", "sv", child.id)]))

    async def loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("billing tick failed")
            await asyncio.sleep(TICK_SECONDS)

    # ------------------------------------------------------------------ payments
    async def create_payment(self, child: ChildBot, creator_id: int, periods: int, method: str) -> ServicePayment:
        cfg = await self.config()
        if periods not in PERIOD_CHOICES:
            raise PaymentError("مدت نامعتبر است.")
        if not cfg.price_irt:
            raise PaymentError("قیمت سرویس هنوز تعیین نشده است.")
        if method not in self.methods(cfg):
            raise PaymentError("این روش پرداخت فعال نیست.")
        sp = ServicePayment(
            bot_id=child.id,
            creator_id=creator_id,
            days=cfg.days_for(periods),
            amount=cfg.price_for(periods),
            method=method,
        )
        async with self.app.db.session() as s:
            s.add(sp)
            await s.commit()
        return sp

    async def start_zarinpal(self, sp: ServicePayment, title: str) -> str:
        cfg = await self.config()
        zbase = self.app.settings.zarinpal_base
        body = {
            "merchant_id": cfg.zarinpal_merchant,
            "amount": sp.amount * 10,
            "callback_url": f"{self.app.settings.base_url}/pay/service/{sp.id}",
            "description": title[:200],
        }
        _st, data = await self.app.payments._post_json(f"{zbase}/pg/v4/payment/request.json", body)
        d = data.get("data")
        if isinstance(d, dict) and d.get("code") == 100 and d.get("authority"):
            authority = str(d["authority"])
            async with self.app.db.session() as s:
                await s.execute(update(ServicePayment).where(ServicePayment.id == sp.id).values(authority=authority))
                await s.commit()
            return f"{zbase}/pg/StartPay/{authority}"
        errors = data.get("errors")
        detail = errors.get("message") if isinstance(errors, dict) else None
        log.warning("service zarinpal request rejected: %s", errors)
        raise PaymentError("درگاه پرداخت درخواست را نپذیرفت" + (f" ({detail})" if detail else "") + ".")

    async def handle_zarinpal_callback(self, sp_id: int, authority: str, status: str) -> CallbackResult:
        async with self.app.db.session() as s:
            sp = await s.get(ServicePayment, sp_id)
        if sp is None or sp.method != "zarinpal" or not sp.authority or not hmac.compare_digest(sp.authority, authority or ""):
            return CallbackResult(False, "پرداخت نامعتبر است.")
        mother = self.app.mother_username
        if sp.status == "paid":
            return CallbackResult(True, "این پرداخت قبلاً ثبت شده است.", mother)
        if status != "OK":
            await self._set_status(sp.id, "failed", ("pending",))
            return CallbackResult(False, "پرداخت انجام نشد یا لغو شد.", mother)
        cfg = await self.config()
        body = {"merchant_id": cfg.zarinpal_merchant, "amount": sp.amount * 10, "authority": sp.authority}
        try:
            _st, data = await self.app.payments._post_json(f"{self.app.settings.zarinpal_base}/pg/v4/payment/verify.json", body)
        except PaymentError:
            return CallbackResult(False, "تأیید پرداخت با خطا روبه‌رو شد. اگر مبلغ کسر شده، با پشتیبانی تماس بگیرید.", mother)
        d = data.get("data")
        if isinstance(d, dict) and d.get("code") in (100, 101):
            ref = str(d.get("ref_id") or "")
            await self.fulfill(sp.id, ref)
            return CallbackResult(True, f"پرداخت موفق بود و سرویس تمدید شد. کد پیگیری: {ref}" if ref else "پرداخت موفق بود و سرویس تمدید شد.", mother)
        await self._set_status(sp.id, "failed", ("pending",))
        return CallbackResult(False, "پرداخت تأیید نشد.", mother)

    async def _set_status(self, sp_id: int, status: str, allowed: tuple[str, ...]) -> bool:
        async with self.app.db.session() as s:
            res = await s.execute(
                update(ServicePayment).where(ServicePayment.id == sp_id, ServicePayment.status.in_(allowed)).values(status=status)
            )
            await s.commit()
        return bool(res.rowcount)

    async def fulfill(self, sp_id: int, ref: str | None = None) -> bool:
        """Mark paid and extend — exactly once (conditional update)."""
        async with self.app.db.session() as s:
            res = await s.execute(
                update(ServicePayment)
                .where(ServicePayment.id == sp_id, ServicePayment.status.in_(("pending", "review")))
                .values(status="paid", paid_at=utcnow(), ref=ref)
            )
            await s.commit()
            if not res.rowcount:
                return False
            sp = await s.get(ServicePayment, sp_id)
        assert sp is not None
        child = await self.extend(sp.bot_id, sp.days, paid=True)
        if child is not None:
            assert child.service_until is not None
            self.app.spawn(
                self.app.notify_creator(
                    sp.creator_id,
                    f"✅ سرویس ربات @{esc(child.username)} به مدت {fmt_int(sp.days)} روز تمدید شد.\n"
                    f"پایان سرویس: {jdate(child.service_until)}",
                )
            )
        return True

    async def submit_receipt(self, sp_id: int, creator_id: int, file_id: str | None, text: str | None) -> bool:
        async with self.app.db.session() as s:
            res = await s.execute(
                update(ServicePayment)
                .where(ServicePayment.id == sp_id, ServicePayment.creator_id == creator_id, ServicePayment.status == "pending")
                .values(status="review", receipt_file_id=file_id, receipt_text=text)
            )
            await s.commit()
        return bool(res.rowcount)

    async def notify_admins_receipt(self, sp_id: int, only: int | None = None) -> None:
        bot = self.app.mother_bot
        if bot is None:
            return
        async with self.app.db.session() as s:
            sp = await s.get(ServicePayment, sp_id)
            child = await s.get(ChildBot, sp.bot_id) if sp else None
        if sp is None or child is None:
            return
        caption = (
            "🧾 <b>رسید تمدید سرویس</b>\n"
            f"ربات: @{esc(child.username)} · سازنده: <code>{sp.creator_id}</code>\n"
            f"مبلغ: {fmt_toman(sp.amount)} · مدت: {fmt_int(sp.days)} روز\n"
            + (f"توضیح: {esc(sp.receipt_text)}" if sp.receipt_text else "")
        )
        markup = kb([btn("✅ تأیید", "svok", sp.id), btn("❌ رد", "svno", sp.id)])
        for admin in [only] if only else self.app.settings.admin_ids:
            try:
                if sp.receipt_file_id:
                    await bot.send_photo(admin, sp.receipt_file_id, caption=caption, reply_markup=markup)
                else:
                    await bot.send_message(admin, caption, reply_markup=markup)
            except TelegramAPIError as exc:
                log.warning("cannot send receipt to admin %s: %s", admin, exc)

    async def approve(self, sp_id: int) -> str:
        if await self.fulfill(sp_id, "manual"):
            return "✅ تأیید شد و سرویس تمدید گردید."
        return "این رسید قبلاً رسیدگی شده است."

    async def reject(self, sp_id: int) -> str:
        async with self.app.db.session() as s:
            sp = await s.get(ServicePayment, sp_id)
        if sp is not None and await self._set_status(sp_id, "rejected", ("review",)):
            await self.app.notify_creator(sp.creator_id, "❌ رسید پرداخت سرویس شما تأیید نشد. در صورت اشتباه، دوباره تمدید کنید یا با پشتیبانی تماس بگیرید.")
            return "❌ رد شد."
        return "این رسید قبلاً رسیدگی شده است."

    async def grant(self, bot_id: int, days: int, admin_id: int) -> ChildBot | None:
        """Admin gift of free days (audited)."""
        child = await self.extend(bot_id, days)
        if child is not None:
            async with self.app.db.session() as s:
                s.add(
                    ServicePayment(
                        bot_id=bot_id, creator_id=child.owner_id, days=days, amount=0, method="admin", status="paid", paid_at=utcnow(), ref=f"admin:{admin_id}"
                    )
                )
                await s.commit()
        return child

    # ------------------------------------------------------------------ stats
    async def stats(self) -> dict[str, int]:
        now = utcnow()
        cfg = await self.config()
        async with self.app.db.session() as s:
            children = list((await s.scalars(select(ChildBot))).all())
            review = int(await s.scalar(select(func.count()).select_from(ServicePayment).where(ServicePayment.status == "review")) or 0)
            since = now - timedelta(days=30)
            revenue = int(
                await s.scalar(
                    select(func.coalesce(func.sum(ServicePayment.amount), 0)).where(
                        ServicePayment.status == "paid", ServicePayment.method != "admin", ServicePayment.paid_at >= since
                    )
                )
                or 0
            )
        out = {"free": 0, "trial": 0, "active": 0, "grace": 0, "expired": 0, "review": review, "revenue30": revenue}
        for c in children:
            out[phase(c, cfg, now)] += 1
        return out
