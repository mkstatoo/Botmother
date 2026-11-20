"""Mother bot (platform admin): "🩺 سلامت سرویس" — shows where the time goes when bots feel slow."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery

from ..callbacks import A
from ..services.metrics import system_snapshot
from ..ui import btn, kb, show
from ..util import fmt_int

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

health_router = Router(name="mother-health")
health_router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

DENIED = "دسترسی ندارید."


def _s(x: float) -> str:
    return f"{x * 1000:,.0f} ms" if x < 1 else f"{x:.1f} s"


def verdict(api_p50: float, api_n: int, poll_errors: int, load_ratio: float | None, mem_avail: float | None, api_failed: int = 0) -> list[str]:
    """Plain-language findings, most important first."""
    out: list[str] = []
    if api_n >= 5:
        if api_p50 > 1.5:
            out.append("🔴 مسیر شبکه تا تلگرام (پروکسی / سرور V2Ray) خیلی کند است؛ علت اصلی کندی همین است.")
        elif api_p50 > 0.6:
            out.append("🟡 تأخیر مسیر تلگرام بالاست؛ هر کلیک حدود ۲ رفت‌وبرگشت دارد، پس کندی محسوس است.")
        else:
            out.append("🟢 تأخیر مسیر تلگرام خوب است.")
    else:
        out.append("ℹ️ هنوز تعداد کافی درخواست ثبت نشده؛ چند بار ربات را امتحان کنید و دوباره بزنید.")
    total = api_n + api_failed
    if total >= 5 and api_failed / total > 0.15:
        out.insert(0, f"🔴 {api_failed / total * 100:.0f}٪ درخواست‌ها به تلگرام ناموفق بوده‌اند؛ اتصال گاهی کاملاً می‌ایستد (حتی اگر سرعتِ بقیه خوب باشد).")
    if poll_errors > 10:
        out.append(f"🔴 {fmt_int(poll_errors)} خطای اتصال در یک ساعت اخیر؛ پروکسی ناپایدار است (واچ‌داگ باید سرور را عوض کند).")
    elif poll_errors > 2:
        out.append(f"🟡 {fmt_int(poll_errors)} خطای اتصال در یک ساعت اخیر.")
    if load_ratio is not None and load_ratio > 1.0:
        out.append("🔴 پردازنده سرور شلوغ است (load بیشتر از تعداد هسته).")
    if mem_avail is not None and mem_avail < 200:
        out.append("🔴 حافظهٔ آزاد سرور کم است.")
    return out


def view(app: App, probe: list[float] | None = None, probe_err: str | None = None):
    api, upd = app.metrics.api_summary(), app.metrics.update_summary()
    poll_err = app.metrics.poll_errors()
    sysi = system_snapshot()
    load_ratio = (sysi["load1"] / sysi["cpus"]) if sysi["load1"] is not None else None
    lines = ["🩺 <b>سلامت سرویس</b> (ساعت اخیر؛ بعد از هر ری‌استارت از صفر شروع می‌شود)\n"]
    lines += verdict(api.p50, api.n, poll_err, load_ratio, sysi["mem_avail_mb"], api.errors)
    lines.append("")
    lines.append("<b>درخواست‌های ربات‌ها به تلگرام</b>")
    if api.n:
        lines.append(f"تعداد: {fmt_int(api.n)} · میانگین {_s(api.avg)} · وسط {_s(api.p50)} · ۹۵٪ {_s(api.p95)} · بدترین {_s(api.worst)}")
    else:
        lines.append("هنوز موردی ثبت نشده.")
    if api.errors:
        lines.append(f"ناموفق: {fmt_int(api.errors)}")
    lines.append(f"خطای اتصال دریافت پیام‌ها: {fmt_int(poll_err)}")
    lines.append("")
    lines.append("<b>پردازش هر پیام/کلیک (از دریافت تا تمام شدن پاسخ)</b>")
    if upd.n:
        lines.append(f"تعداد: {fmt_int(upd.n)} · میانگین {_s(upd.avg)} · ۹۵٪ {_s(upd.p95)} · بدترین {_s(upd.worst)}")
        if api.n and upd.avg:
            share = min(1.0, (api.avg * 2) / upd.avg) if upd.avg else 0
            lines.append(f"سهم تقریبی انتظار برای تلگرام: {share * 100:.0f}٪")
    else:
        lines.append("هنوز موردی ثبت نشده.")
    lines.append("")
    lines.append("<b>سرور</b>")
    if sysi["load1"] is not None:
        lines.append(f"بار پردازنده: {sysi['load1']:.2f} (هسته‌ها: {sysi['cpus']:.0f})")
    if sysi["mem_total_mb"]:
        lines.append(f"حافظه: {sysi['mem_avail_mb']:,.0f} از {sysi['mem_total_mb']:,.0f} مگابایت آزاد")
    if probe is not None:
        lines.append("")
        lines.append("<b>تست زندهٔ همین لحظه (۳ بار getMe)</b>")
        lines.append(" · ".join(_s(x) for x in probe) if probe else "—")
    if probe_err:
        lines.append(f"\n❌ تست زنده ناموفق بود: {probe_err}")
    return "\n".join(lines), kb([btn("🧪 تست زنده", "admhlt"), btn("🔄 تازه‌سازی", "admhl")], [btn("🔙 بازگشت", "adm")])


@health_router.callback_query(A.filter(F.act == "admhl"))
async def cb_health(callback: CallbackQuery, app: App, is_admin: bool) -> None:
    if not is_admin:
        await callback.answer(DENIED, show_alert=True)
        return
    text, markup = view(app)
    await show(callback, text, markup)
    await callback.answer()


@health_router.callback_query(A.filter(F.act == "admhlt"))
async def cb_probe(callback: CallbackQuery, app: App, is_admin: bool) -> None:
    if not is_admin or app.mother_bot is None:
        await callback.answer(DENIED, show_alert=True)
        return
    await callback.answer("در حال تست…")
    times: list[float] = []
    err: str | None = None
    for _ in range(3):
        t = time.perf_counter()
        try:
            await app.mother_bot.get_me()
        except (TelegramAPIError, TimeoutError, OSError) as exc:
            err = str(exc)[:120]
            break
        times.append(time.perf_counter() - t)
    text, markup = view(app, times, err)
    await show(callback, text, markup)
