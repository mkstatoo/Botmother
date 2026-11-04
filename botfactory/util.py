"""Small helpers: time, Persian digits, number parsing, formatting."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import jdatetime

_FA = "۰۱۲۳۴۵۶۷۸۹"
_AR = "٠١٢٣٤٥٦٧٨٩"
_TO_EN = {ord(c): str(i) for i, c in enumerate(_FA)} | {ord(c): str(i) for i, c in enumerate(_AR)}
_TO_FA = {ord(str(i)): c for i, c in enumerate(_FA)}


def utcnow() -> datetime:
    """Naive UTC now (all DB datetimes are naive UTC)."""
    return datetime.now(UTC).replace(tzinfo=None)


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def to_en(text: str) -> str:
    return text.translate(_TO_EN)


def to_fa(text: object) -> str:
    return str(text).translate(_TO_FA)


def fmt_int(n: int) -> str:
    return to_fa(f"{n:,}".replace(",", "٬"))


def fmt_toman(n: int) -> str:
    return f"{fmt_int(n)} تومان"


def fmt_usd(cents: int) -> str:
    whole, frac = divmod(int(cents), 100)
    s = f"{whole:,}" + (f".{frac:02d}" if frac else "")
    return f"{to_fa(s)} دلار"


def fmt_money(amount: int, currency: str) -> str:
    return fmt_toman(amount) if currency == "IRT" else fmt_usd(amount)


def parse_int(text: str) -> int | None:
    s = to_en(text or "").strip()
    s = re.sub(r"[,٬،\s_]", "", s)
    return int(s) if s.isdigit() else None


def parse_cents(text: str) -> int | None:
    s = to_en(text or "").strip().replace("٫", ".").replace("٬", "").replace(",", "")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", s):
        return None
    try:
        return int((Decimal(s) * 100).to_integral_value())
    except InvalidOperation:  # pragma: no cover
        return None


def jdate(dt: datetime, with_time: bool = False) -> str:
    j = jdatetime.datetime.fromgregorian(datetime=dt)
    return to_fa(j.strftime("%Y/%m/%d %H:%M" if with_time else "%Y/%m/%d"))


def human_size(size: int | None) -> str:
    if not size:
        return ""
    value = float(size)
    for unit in ("بایت", "کیلوبایت", "مگابایت", "گیگابایت"):
        if value < 1024 or unit == "گیگابایت":
            return to_fa(f"{value:.1f}".rstrip("0").rstrip(".")) + " " + unit
        value /= 1024
    return ""  # pragma: no cover


def human_duration(seconds: int) -> str:
    if seconds <= 0:
        return "غیرفعال"
    if seconds < 60:
        return f"{to_fa(seconds)} ثانیه"
    if seconds < 3600:
        return f"{to_fa(seconds // 60)} دقیقه"
    return f"{to_fa(seconds // 3600)} ساعت"
