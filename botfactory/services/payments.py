"""Payments: Zarinpal (Iranian gateway), NOWPayments (crypto) and manual receipts.

Money flows straight to the content creator's own gateway account — the credentials are
configured per child bot and stored encrypted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import aiohttp
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select, update

from ..callbacks import A
from ..db.models import (
    BotUser,
    ChildBot,
    Content,
    Payment,
    Plan,
    Purchase,
    Subscription,
)
from ..util import esc, fmt_money, jdate, utcnow
from .access import subscription_until
from .delivery import ContentUnavailable, deliver

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger(__name__)

METHOD_LABEL = {"zarinpal": "💳 درگاه بانکی (زرین‌پال)", "crypto": "🪙 ارز دیجیتال", "manual": "🧾 کارت به کارت / واریز دستی"}
METHOD_NAME = {"zarinpal": "درگاه بانکی", "crypto": "ارز دیجیتال", "manual": "واریز دستی"}


class PaymentError(Exception):
    """Raised with a user-facing (Persian) message."""


@dataclass
class Target:
    kind: str  # "content" | "plan" | "order"
    id: int
    title: str
    price_irt: int | None
    price_usd_cents: int | None


@dataclass
class CallbackResult:
    ok: bool
    message: str
    bot_username: str | None = None


# ---------------------------------------------------------------------------
# NOWPayments IPN signature
# ---------------------------------------------------------------------------
def _normalize_numbers(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _normalize_numbers(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_numbers(v) for v in obj]
    if isinstance(obj, float) and obj.is_integer():
        return int(obj)
    return obj


def _canonical_variants(raw: bytes) -> list[str]:
    """Sorted-key compact JSON in the few serialisations a sender may have signed."""
    obj = json.loads(raw)
    out: list[str] = []
    for candidate in (obj, _normalize_numbers(obj)):
        for ensure_ascii in (True, False):
            s = json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=ensure_ascii)
            if s not in out:
                out.append(s)
    return out


def verify_ipn_signature(raw: bytes, signature: str, secret: str) -> bool:
    if not signature or not secret:
        return False
    try:
        variants = _canonical_variants(raw)
    except ValueError:
        return False
    signature = signature.strip().lower()
    for canonical in variants:
        digest = hmac.new(secret.encode(), canonical.encode(), hashlib.sha512).hexdigest()
        if hmac.compare_digest(digest, signature):
            return True
    return False


# ---------------------------------------------------------------------------
class PaymentService:
    def __init__(self, app: App) -> None:
        self.app = app

    # ------------------------------------------------------------------ helpers
    def methods_for(self, child: ChildBot, price_irt: int | None, price_usd_cents: int | None) -> list[str]:
        methods: list[str] = []
        gateways = self.app.settings.payments_enabled
        if gateways and price_irt and child.zarinpal_merchant_enc:
            methods.append("zarinpal")
        if gateways and price_usd_cents and child.nowpayments_key_enc and child.nowpayments_ipn_enc:
            methods.append("crypto")
        if (price_irt or price_usd_cents) and (child.card_number or child.manual_note):
            methods.append("manual")
        return methods

    async def _post_json(self, url: str, body: dict, headers: dict | None = None) -> tuple[int, dict]:
        http = self.app.http
        if http is None:  # pragma: no cover - app always sets it
            raise PaymentError("سرویس پرداخت آماده نیست.")
        try:
            async with http.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except ValueError:
                    data = {}
                return resp.status, data if isinstance(data, dict) else {}
        except (TimeoutError, aiohttp.ClientError) as exc:
            log.warning("gateway request failed: %s", exc)
            raise PaymentError("ارتباط با درگاه پرداخت برقرار نشد. کمی بعد دوباره تلاش کنید.") from exc

    def _callback_url(self, kind: str, payment_id: int) -> str:
        return f"{self.app.settings.base_url}/pay/{kind}/{payment_id}"

    # ------------------------------------------------------------------ creation
    async def create(self, child: ChildBot, user_id: int, target: Target, method: str) -> Payment:
        if method == "zarinpal":
            amount, currency = target.price_irt, "IRT"
        elif method == "crypto":
            amount, currency = target.price_usd_cents, "USD"
        elif method == "manual":
            amount, currency = (target.price_irt, "IRT") if target.price_irt else (target.price_usd_cents, "USD")
        else:
            raise PaymentError("روش پرداخت نامعتبر است.")
        if not amount:
            raise PaymentError("برای این روش پرداخت قیمتی تعیین نشده است.")
        payment = Payment(
            bot_id=child.id,
            user_id=user_id,
            kind=target.kind,
            content_id=target.id if target.kind == "content" else None,
            plan_id=target.id if target.kind == "plan" else None,
            order_id=target.id if target.kind == "order" else None,
            method=method,
            amount=int(amount),
            currency=currency,
        )
        async with self.app.db.session() as s:
            s.add(payment)
            await s.commit()
        return payment

    async def _set_authority(self, payment_id: int, authority: str) -> None:
        async with self.app.db.session() as s:
            await s.execute(update(Payment).where(Payment.id == payment_id).values(authority=authority))
            await s.commit()

    # ------------------------------------------------------------------ zarinpal
    async def start_zarinpal(self, child: ChildBot, payment: Payment, title: str) -> str:
        merchant = self.app.box.decrypt(child.zarinpal_merchant_enc or "")
        zbase = self.app.settings.zarinpal_base
        body = {
            "merchant_id": merchant,
            "amount": payment.amount * 10,  # Toman → Rial
            "callback_url": self._callback_url("zarinpal", payment.id),
            "description": title[:200] or "خرید",
        }
        _status, data = await self._post_json(f"{zbase}/pg/v4/payment/request.json", body)
        d = data.get("data")
        if isinstance(d, dict) and d.get("code") == 100 and d.get("authority"):
            authority = str(d["authority"])
            await self._set_authority(payment.id, authority)
            return f"{zbase}/pg/StartPay/{authority}"
        errors = data.get("errors")
        detail = errors.get("message") if isinstance(errors, dict) else None
        log.warning("zarinpal request rejected: %s", errors)
        raise PaymentError("درگاه پرداخت درخواست را نپذیرفت" + (f" ({detail})" if detail else "") + ".")

    async def handle_zarinpal_callback(self, payment_id: int, authority: str, status: str) -> CallbackResult:
        async with self.app.db.session() as s:
            p = await s.get(Payment, payment_id)
            if p is None or p.method != "zarinpal" or not p.authority:
                return CallbackResult(False, "پرداخت نامعتبر است.")
            child = await s.get(ChildBot, p.bot_id)
        if child is None or not hmac.compare_digest(p.authority, authority or ""):
            return CallbackResult(False, "پرداخت نامعتبر است.")
        if p.status == "paid":
            return CallbackResult(True, "این پرداخت قبلاً ثبت شده است.", child.username)
        if status != "OK":
            await self._mark(p.id, "failed")
            return CallbackResult(False, "پرداخت انجام نشد یا توسط شما لغو شد.", child.username)

        merchant = self.app.box.decrypt(child.zarinpal_merchant_enc or "")
        body = {"merchant_id": merchant, "amount": p.amount * 10, "authority": p.authority}
        try:
            _st, data = await self._post_json(f"{self.app.settings.zarinpal_base}/pg/v4/payment/verify.json", body)
        except PaymentError:
            return CallbackResult(False, "تأیید پرداخت با خطا روبه‌رو شد. اگر مبلغ کسر شده، با پشتیبانی تماس بگیرید.", child.username)
        d = data.get("data")
        if isinstance(d, dict) and d.get("code") in (100, 101):
            ref = str(d.get("ref_id") or "")
            if await self.fulfill(p.id, ref):
                self.app.spawn(self.after_paid(p.id))
            return CallbackResult(True, f"پرداخت با موفقیت انجام شد. کد پیگیری: {ref}" if ref else "پرداخت با موفقیت انجام شد.", child.username)
        await self._mark(p.id, "failed")
        return CallbackResult(False, "پرداخت تأیید نشد.", child.username)

    # ------------------------------------------------------------------ crypto
    async def start_crypto(self, child: ChildBot, payment: Payment) -> str:
        key = self.app.box.decrypt(child.nowpayments_key_enc or "")
        back = f"https://t.me/{child.username}"
        body = {
            "price_amount": round(payment.amount / 100, 2),
            "price_currency": "usd",
            "order_id": f"bf-{payment.id}",
            "order_description": f"bf order {payment.id}",
            "ipn_callback_url": self._callback_url("nowpayments", payment.id),
            "success_url": back,
            "cancel_url": back,
        }
        status, data = await self._post_json(
            f"{self.app.settings.nowpayments_base}/invoice", body, headers={"x-api-key": key}
        )
        url, inv_id = data.get("invoice_url"), data.get("id")
        if status < 300 and url and inv_id is not None:
            await self._set_authority(payment.id, str(inv_id))
            return str(url)
        log.warning("nowpayments invoice rejected: status=%s body=%s", status, data)
        raise PaymentError("ساخت فاکتور ارز دیجیتال ناموفق بود. تنظیمات کلید API را بررسی کنید.")

    async def handle_nowpayments_ipn(self, payment_id: int, raw: bytes, signature: str) -> tuple[int, str]:
        async with self.app.db.session() as s:
            p = await s.get(Payment, payment_id)
            child = await s.get(ChildBot, p.bot_id) if p else None
        if p is None or child is None or p.method != "crypto" or not child.nowpayments_ipn_enc:
            return 404, "not found"
        secret = self.app.box.decrypt(child.nowpayments_ipn_enc)
        if not verify_ipn_signature(raw, signature, secret):
            return 403, "bad signature"
        try:
            data = json.loads(raw)
        except ValueError:
            return 400, "bad json"
        if str(data.get("order_id")) != f"bf-{p.id}":
            return 400, "order mismatch"

        status = str(data.get("payment_status", ""))
        if status == "finished":
            try:
                price = Decimal(str(data.get("price_amount")))
            except InvalidOperation:
                return 400, "bad amount"
            if str(data.get("price_currency", "")).lower() != "usd" or price * 100 != p.amount:
                log.error("nowpayments amount mismatch payment=%s data=%s", p.id, data)
                return 400, "amount mismatch"
            if await self.fulfill(p.id, str(data.get("payment_id") or "")):
                self.app.spawn(self.after_paid(p.id))
        elif status == "partially_paid":
            async with self.app.db.session() as s:
                res = await s.execute(
                    update(Payment).where(Payment.id == p.id, Payment.status == "pending").values(status="review")
                )
                await s.commit()
            if res.rowcount == 1:
                self.app.spawn(
                    self.notify_review(
                        p.id,
                        "⚠️ پرداخت ارز دیجیتال <b>ناقص</b> انجام شده است. پس از بررسی در پنل NOWPayments تصمیم بگیرید.",
                    )
                )
        elif status in ("failed", "expired", "refunded"):
            await self._mark(p.id, "expired" if status == "expired" else "failed")
        return 200, "ok"

    # ------------------------------------------------------------------ manual
    async def submit_receipt(
        self, payment_id: int, user_id: int, *, file_id: str | None = None, text: str | None = None
    ) -> bool:
        async with self.app.db.session() as s:
            res = await s.execute(
                update(Payment)
                .where(
                    Payment.id == payment_id,
                    Payment.user_id == user_id,
                    Payment.method == "manual",
                    Payment.status.in_(("pending", "review")),
                )
                .values(status="review", receipt_file_id=file_id, receipt_text=text)
            )
            await s.commit()
        return res.rowcount == 1

    async def notify_review(self, payment_id: int, header: str = "🧾 <b>رسید پرداخت جدید</b>") -> None:
        async with self.app.db.session() as s:
            p = await s.get(Payment, payment_id)
            if p is None:
                return
            child = await s.get(ChildBot, p.bot_id)
            buyer = await s.get(BotUser, (p.bot_id, p.user_id))
            title = await self._target_title(s, p)
        bot = self.app.manager.get_bot(child.id) if child else None
        if bot is None or child is None:
            return
        name = esc(buyer.full_name) if buyer else str(p.user_id)
        lines = [
            header,
            "",
            f"👤 <a href=\"tg://user?id={p.user_id}\">{name}</a> (<code>{p.user_id}</code>)",
            f"🛒 {esc(title)}",
            f"💰 {fmt_money(p.amount, p.currency)}",
            f"🔢 شناسه: <code>{p.id}</code>",
        ]
        if p.receipt_text:
            lines += ["", f"📝 {esc(p.receipt_text)}"]
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ تأیید", callback_data=A(act="mp", id=p.id, p="ok").pack()),
                    InlineKeyboardButton(text="❌ رد", callback_data=A(act="mp", id=p.id, p="no").pack()),
                ]
            ]
        )
        text = "\n".join(lines)
        try:
            if p.receipt_file_id:
                await bot.send_photo(child.owner_id, p.receipt_file_id, caption=text[:1024], reply_markup=kb)
            else:
                await bot.send_message(child.owner_id, text, reply_markup=kb)
        except TelegramAPIError as exc:
            log.warning("cannot notify owner of bot %s: %s", child.id, exc)
            await self.app.notify_creator(
                child.owner_id,
                f"یک رسید پرداخت در ربات @{esc(child.username)} منتظر تأیید شماست، اما ربات فرزند نتوانست پیام بدهد. "
                "ابتدا داخل خود آن ربات /start را بزنید.",
            )

    async def approve(self, payment_id: int, owner_id: int) -> str:
        """Returns 'ok' | 'already' | 'forbidden' | 'missing'."""
        async with self.app.db.session() as s:
            p = await s.get(Payment, payment_id)
            child = await s.get(ChildBot, p.bot_id) if p else None
        if p is None or child is None:
            return "missing"
        if child.owner_id != owner_id:
            return "forbidden"
        if await self.fulfill(p.id, ref="manual"):
            self.app.spawn(self.after_paid(p.id))
            return "ok"
        return "already"

    async def reject(self, payment_id: int, owner_id: int) -> str:
        async with self.app.db.session() as s:
            p = await s.get(Payment, payment_id)
            child = await s.get(ChildBot, p.bot_id) if p else None
            if p is None or child is None:
                return "missing"
            if child.owner_id != owner_id:
                return "forbidden"
            res = await s.execute(
                update(Payment).where(Payment.id == p.id, Payment.status.in_(("pending", "review"))).values(status="rejected")
            )
            await s.commit()
        if res.rowcount != 1:
            return "already"
        bot = self.app.manager.get_bot(child.id)
        if bot:
            try:
                await bot.send_message(p.user_id, "❌ رسید پرداخت شما تأیید نشد. در صورت اشتباه با پشتیبانی تماس بگیرید.")
            except TelegramAPIError:
                pass
        return "ok"

    # ------------------------------------------------------------------ fulfilment
    async def _mark(self, payment_id: int, status: str) -> None:
        async with self.app.db.session() as s:
            await s.execute(update(Payment).where(Payment.id == payment_id, Payment.status == "pending").values(status=status))
            await s.commit()

    async def _target_title(self, s, p: Payment) -> str:
        if p.kind == "content" and p.content_id:
            c = await s.get(Content, p.content_id)
            return c.title if c else "محتوا"
        if p.kind == "plan" and p.plan_id:
            pl = await s.get(Plan, p.plan_id)
            return f"اشتراک {pl.name}" if pl else "اشتراک"
        if p.kind == "order":
            return f"سفارش #{p.order_id}"
        return "خرید"

    async def fulfill(self, payment_id: int, ref: str | None = None) -> bool:
        """Mark paid and grant the entitlement. Idempotent: True only for the first caller."""
        async with self.app.db.session() as s:
            values: dict[str, Any] = {"status": "paid", "paid_at": utcnow()}
            if ref:
                values["ref"] = ref
            res = await s.execute(
                update(Payment).where(Payment.id == payment_id, Payment.status.in_(("pending", "review"))).values(**values)
            )
            if res.rowcount != 1:
                await s.rollback()
                return False
            p = await s.get(Payment, payment_id)
            assert p is not None
            if p.kind == "content" and p.content_id:
                exists = await s.scalar(
                    select(Purchase.id).where(
                        Purchase.bot_id == p.bot_id, Purchase.user_id == p.user_id, Purchase.content_id == p.content_id
                    )
                )
                if exists is None:
                    s.add(Purchase(bot_id=p.bot_id, user_id=p.user_id, content_id=p.content_id, payment_id=p.id))
            elif p.kind == "order":
                await self.app.shop.mark_paid(s, p)
            elif p.kind == "plan" and p.plan_id:
                plan = await s.get(Plan, p.plan_id)
                days = plan.days if plan else 30
                now = utcnow()
                latest = await s.scalar(
                    select(func.max(Subscription.expires_at)).where(
                        Subscription.bot_id == p.bot_id, Subscription.user_id == p.user_id
                    )
                )
                start = max(now, latest) if latest else now
                s.add(
                    Subscription(
                        bot_id=p.bot_id,
                        user_id=p.user_id,
                        plan_id=p.plan_id,
                        payment_id=p.id,
                        starts_at=start,
                        expires_at=start + timedelta(days=days),
                    )
                )
            await s.commit()
        return True

    async def after_paid(self, payment_id: int) -> None:
        """Tell the buyer (and deliver the content / confirm the plan) and notify the owner."""
        try:
            async with self.app.db.session() as s:
                p = await s.get(Payment, payment_id)
                if p is None:
                    return
                is_order = p.kind == "order"
                child = await s.get(ChildBot, p.bot_id)
                content = await s.get(Content, p.content_id) if p.content_id else None
                title = await self._target_title(s, p)
                until = await subscription_until(s, p.bot_id, p.user_id) if p.kind == "plan" else None
                buyer = await s.get(BotUser, (p.bot_id, p.user_id))
            if is_order:  # outside the session: it talks to Telegram
                await self.app.shop.after_paid(p)
                return
            bot = self.app.manager.get_bot(child.id) if child else None
            if bot is None or child is None:
                return

            ref = f"\n🔢 کد پیگیری: <code>{esc(p.ref)}</code>" if p.ref and p.ref != "manual" else ""
            try:
                if p.kind == "plan":
                    exp = f"\n📅 اعتبار تا: {jdate(until)}" if until else ""
                    await bot.send_message(p.user_id, f"✅ پرداخت موفق بود و اشتراک شما فعال شد.{exp}{ref}")
                else:
                    await bot.send_message(p.user_id, f"✅ پرداخت موفق بود.{ref}")
                    if content is not None:
                        await deliver(self.app, bot, child, content, p.user_id, p.user_id)
            except ContentUnavailable:
                await bot.send_message(p.user_id, "⚠️ ارسال فایل با مشکل روبه‌رو شد؛ با مدیر ربات تماس بگیرید.")
            except TelegramAPIError as exc:
                log.warning("cannot message buyer %s: %s", p.user_id, exc)

            name = esc(buyer.full_name) if buyer else str(p.user_id)
            try:
                await bot.send_message(
                    child.owner_id,
                    f"💰 <b>فروش جدید</b>\n🛒 {esc(title)}\n💵 {fmt_money(p.amount, p.currency)} "
                    f"({METHOD_NAME.get(p.method, p.method)})\n👤 <a href=\"tg://user?id={p.user_id}\">{name}</a>",
                )
            except TelegramAPIError:
                pass
        except Exception:
            log.exception("after_paid failed for payment %s", payment_id)
