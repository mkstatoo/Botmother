"""Mother bot: the platform main channel (admin menu) + the "✅ عضو شدم" button."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, MessageOriginChannel

from ..callbacks import A
from ..gate import on_joined_pressed
from ..services.sponsors import SponsorError, normalize_chat_ref, resolve_channel
from ..ui import btn, kb, show
from ..util import esc

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger(__name__)

channel_router = Router(name="mother-channel")
channel_router.message.filter(F.chat.type == ChatType.PRIVATE)
channel_router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

DENIED = "دسترسی ندارید."


class MainChannelSet(StatesGroup):
    ref = State()


@channel_router.callback_query(A.filter(F.act == "mchk"))
async def cb_joined(callback: CallbackQuery, app: App) -> None:
    await on_joined_pressed(app, callback, "creators", app.mother_username)


async def view(app: App):
    cfg = await app.mainchan.config()
    if not cfg.active:
        text = (
            "📢 <b>کانال اصلی پلتفرم</b>\n\n"
            "هنوز کانالی تعریف نشده است.\n\n"
            "با تعریف کانال اصلی، <b>سازندگان</b> (در ربات مادر) و <b>کاربرانِ همهٔ ربات‌های فرزند</b> "
            "ابتدا باید در آن عضو شوند و بعد می‌توانند از ربات‌ها استفاده کنند.\n\n"
            "ℹ️ فقط <b>ربات مادر</b> باید در آن کانال ادمین باشد؛ لازم نیست رباتی دیگر را ادمین کنید."
        )
        return text, kb([btn("➕ تعریف کانال اصلی", "admmcs")], [btn("🔙 بازگشت", "adm")])
    adm = await app.mainchan.mother_is_admin()
    state = {True: "✅ ربات مادر ادمین است", False: "⚠️ ربات مادر در کانال ادمین نیست؛ تا اصلاح نشود هیچ‌کس ملزم به عضویت نمی‌شود", None: "—"}[adm]

    def flag(on: bool) -> str:
        return "✅ اجباری" if on else "⛔️ غیراجباری"

    text = (
        "📢 <b>کانال اصلی پلتفرم</b>\n\n"
        f"کانال: {cfg.label()}\n"
        f"وضعیت: {state}\n\n"
        f"👥 کاربران ربات‌های فرزند: {flag(cfg.gate_children)}\n"
        f"👤 سازندگان (ربات مادر): {flag(cfg.gate_creators)}\n\n"
        "ادمین‌های پلتفرم از شرط عضویت معاف‌اند."
    )
    return text, kb(
        [btn(("⛔️ لغو اجبار: " if cfg.gate_children else "✅ اجباری کردن: ") + "کاربران ربات‌ها", "admmct", 0, "children")],
        [btn(("⛔️ لغو اجبار: " if cfg.gate_creators else "✅ اجباری کردن: ") + "سازندگان", "admmct", 0, "creators")],
        [btn("🔄 بررسی دوباره", "admmc"), btn("✏️ تغییر کانال", "admmcs")],
        [btn("🗑 حذف کانال", "admmcx")],
        [btn("🔙 بازگشت", "adm")],
    )


@channel_router.callback_query(A.filter(F.act == "admmc"))
async def cb_menu(callback: CallbackQuery, app: App, is_admin: bool, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer(DENIED, show_alert=True)
        return
    await state.clear()
    text, markup = await view(app)
    await show(callback, text, markup)
    await callback.answer()


@channel_router.callback_query(A.filter(F.act == "admmcs"))
async def cb_set(callback: CallbackQuery, is_admin: bool, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer(DENIED, show_alert=True)
        return
    await state.set_state(MainChannelSet.ref)
    await show(
        callback,
        "📢 ابتدا <b>ربات مادر</b> را ادمین کانال کنید، بعد آیدی کانال را بفرستید (مثل <code>@mychannel</code>) "
        "یا یک پیام از آن را فوروارد کنید.",
        kb([btn("❌ انصراف", "admmc")]),
    )
    await callback.answer()


@channel_router.message(MainChannelSet.ref)
async def on_ref(message: Message, app: App, is_admin: bool, state: FSMContext) -> None:
    if not is_admin:
        return
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("لغو شد.")
        return
    ref: str | int | None = None
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        ref = origin.chat.id
    elif message.text:
        ref = normalize_chat_ref(message.text)
    retry = kb([btn("❌ انصراف", "admmc")])
    if ref is None or app.mother_bot is None:
        await message.answer("❌ متوجه نشدم. آیدی کانال را بفرستید (مثل <code>@mychannel</code>) یا یک پیام از آن را فوروارد کنید.", reply_markup=retry)
        return
    try:
        info = await resolve_channel(app.mother_bot, ref)
    except SponsorError as exc:
        await message.answer(f"❌ {esc(exc)}\n\nربات مادر را در کانال ادمین کنید و دوباره آیدی را بفرستید.", reply_markup=retry)
        return
    await app.mainchan.set_channel(info)
    await state.clear()
    await message.answer(f"✅ کانال اصلی روی «{esc(info.title)}» تنظیم شد.")
    text, markup = await view(app)
    await message.answer(text, reply_markup=markup)


@channel_router.callback_query(A.filter(F.act == "admmct"))
async def cb_toggle(callback: CallbackQuery, callback_data: A, app: App, is_admin: bool) -> None:
    scope = callback_data.p
    if not is_admin or scope not in ("children", "creators"):
        await callback.answer(DENIED, show_alert=True)
        return
    cfg = await app.mainchan.config()
    if not cfg.active:
        await callback.answer("ابتدا کانال را تعریف کنید.", show_alert=True)
        return
    await app.mainchan.set_gate(scope, not (cfg.gate_children if scope == "children" else cfg.gate_creators))
    text, markup = await view(app)
    await show(callback, text, markup)
    await callback.answer("انجام شد.")


@channel_router.callback_query(A.filter(F.act == "admmcx"))
async def cb_remove(callback: CallbackQuery, callback_data: A, app: App, is_admin: bool) -> None:
    if not is_admin:
        await callback.answer(DENIED, show_alert=True)
        return
    if callback_data.p != "y":
        await show(
            callback,
            "🗑 کانال اصلی حذف شود؟ بعد از آن عضویت اجباری نیست.",
            kb([btn("✅ بله، حذف شود", "admmcx", 0, "y")], [btn("↩️ انصراف", "admmc")]),
        )
        await callback.answer()
        return
    await app.mainchan.clear()
    text, markup = await view(app)
    await show(callback, text, markup)
    await callback.answer("حذف شد.")
