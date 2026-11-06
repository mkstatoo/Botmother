"""Owner panel — adding and managing content."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, func, select

from ..callbacks import A
from ..db.models import Category, ChildBot, Content
from ..services.delivery import KIND_ICON, KIND_LABEL, ContentUnavailable, send_content
from ..ui import btn, kb, show
from ..util import esc, fmt_int, human_size, jdate, parse_cents, parse_int
from .common import PAGE, IsOwner, deep_link, price_text, trunc
from .states import EditContent, Upload

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger(__name__)

router = Router(name="child_admin_content")
router.message.filter(F.chat.type == ChatType.PRIVATE, IsOwner())
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE, IsOwner())

MEDIA = F.audio | F.video | F.voice | F.document | F.video_note
CANCEL_ROW = [btn("❌ انصراف", "ucancel")]


# ----------------------------------------------------------------------------- panel
PANEL_TEXT = (
    "⚙️ <b>پنل مدیریت</b> @{username}\n\n"
    "💡 برای ثبت محتوا کافی است هر <b>فایل صوتی، ویدئو، ویس یا فایل</b> را همین‌جا برای ربات بفرستید."
)


def panel_markup():
    return kb(
        [btn("📤 افزودن محتوا", "uadd"), btn("🔗 افزودن لینک", "alink")],
        [btn("📚 مدیریت محتوا", "cl", 0), btn("🗂 دسته‌بندی‌ها", "cats")],
        [btn("📢 کانال‌های اسپانسر", "sp"), btn("💎 پلن‌ها و پرداخت", "pp")],
        [btn("📊 آمار", "stat"), btn("📨 ارسال همگانی", "bc")],
        [btn("🛍 فروشگاه", "sam")],
        [btn("⚙️ تنظیمات", "st"), btn("🏠 منوی کاربر", "home")],
    )


@router.message(Command("panel"))
@router.callback_query(A.filter(F.act == "panel"))
async def show_panel(event: Message | CallbackQuery, state: FSMContext, child: ChildBot) -> None:
    await state.clear()
    await show(event, PANEL_TEXT.format(username=esc(child.username)), panel_markup())
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.callback_query(A.filter(F.act == "ucancel"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext, child: ChildBot) -> None:
    await state.clear()
    await show(callback, PANEL_TEXT.format(username=esc(child.username)), panel_markup())
    await callback.answer("لغو شد.")


# ----------------------------------------------------------------------------- upload flow
def _strip_ext(name: str | None) -> str:
    return os.path.splitext(name)[0] if name else ""


def extract_media(m: Message) -> dict[str, Any] | None:
    caption = (m.caption or "").strip().split("\n")[0]
    if m.audio:
        a = m.audio
        title = a.title or _strip_ext(a.file_name)
        if a.performer and a.title:
            title = f"{a.performer} - {a.title}"
        return dict(kind="audio", file_id=a.file_id, file_unique_id=a.file_unique_id, duration=a.duration, file_size=a.file_size, default_title=title or caption)
    if m.video:
        v = m.video
        return dict(kind="video", file_id=v.file_id, file_unique_id=v.file_unique_id, duration=v.duration, file_size=v.file_size, default_title=_strip_ext(v.file_name) or caption)
    if m.voice:
        v = m.voice
        return dict(kind="voice", file_id=v.file_id, file_unique_id=v.file_unique_id, duration=v.duration, file_size=v.file_size, default_title=caption)
    if m.video_note:
        v = m.video_note
        return dict(kind="video_note", file_id=v.file_id, file_unique_id=v.file_unique_id, duration=v.duration, file_size=v.file_size, default_title=caption)
    if m.document:
        d = m.document
        return dict(kind="document", file_id=d.file_id, file_unique_id=d.file_unique_id, duration=None, file_size=d.file_size, default_title=_strip_ext(d.file_name) or caption)
    return None


async def _draft(state: FSMContext) -> dict[str, Any]:
    return dict((await state.get_data()).get("draft") or {})


async def _save_draft(state: FSMContext, draft: dict[str, Any]) -> None:
    await state.update_data(draft=draft)


@router.callback_query(A.filter(F.act == "uadd"))
async def cb_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Upload.file)
    await show(
        callback,
        "📤 فایل صوتی / ویدئو / ویس / فایل را بفرستید.\n\n"
        "ℹ️ می‌توانید فایل را مستقیم از جای دیگر فوروارد هم کنید.",
        kb(CANCEL_ROW),
    )
    await callback.answer()


@router.callback_query(A.filter(F.act == "alink"))
async def cb_add_link(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Upload.url)
    await show(callback, "🔗 آدرس لینک (http/https) را بفرستید. برای محتوای حجیم روی ذخیره‌ساز خارجی مناسب است.", kb(CANCEL_ROW))
    await callback.answer()


@router.message(MEDIA, StateFilter(None, Upload.file))
async def on_media(message: Message, state: FSMContext, child: ChildBot) -> None:
    info = extract_media(message)
    if info is None:  # pragma: no cover - filter guarantees media
        return
    draft = {
        **info,
        "src_chat_id": message.chat.id,
        "src_message_id": message.message_id,
        "access": "free",
        "price_irt": None,
        "price_usd_cents": None,
        "category_id": None,
        "description": None,
        "storage": child.default_storage if child.storage_channel_id else "telegram",
    }
    await state.set_state(Upload.title)
    await _save_draft(state, draft)
    default = (info["default_title"] or "").strip()[:255]
    rows = []
    if default:
        rows.append([btn("✅ استفاده از «" + trunc(default, 28) + "»", "utitle")])
    rows.append(CANCEL_ROW)
    size = f" · {human_size(info['file_size'])}" if info.get("file_size") else ""
    await message.answer(
        f"{KIND_ICON[info['kind']]} {KIND_LABEL[info['kind']]} دریافت شد{size}.\n\n✏️ <b>عنوان</b> محتوا را بفرستید:",
        reply_markup=kb(*rows),
    )


@router.message(Upload.url, F.text)
async def on_url(message: Message, state: FSMContext) -> None:
    url = (message.text or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or " " in url or len(url) > 2000:
        await message.answer("❌ آدرس معتبر نیست. باید با http:// یا https:// شروع شود.", reply_markup=kb(CANCEL_ROW))
        return
    draft = {
        "kind": "link",
        "storage": "external",
        "external_url": url,
        "default_title": parsed.netloc,
        "access": "free",
        "price_irt": None,
        "price_usd_cents": None,
        "category_id": None,
        "description": None,
    }
    await state.set_state(Upload.title)
    await _save_draft(state, draft)
    await message.answer(
        "✏️ <b>عنوان</b> لینک را بفرستید:",
        reply_markup=kb([btn("✅ استفاده از «" + trunc(parsed.netloc, 28) + "»", "utitle")], CANCEL_ROW),
    )


async def _ask_desc(ui: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Upload.desc)
    await show(ui, "📝 <b>توضیحات</b> محتوا را بفرستید (اختیاری):", kb([btn("⏭ رد کردن", "udesc")], CANCEL_ROW))


@router.message(Upload.title, F.text)
async def on_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title or len(title) > 255:
        await message.answer("عنوان باید بین ۱ تا ۲۵۵ حرف باشد.", reply_markup=kb(CANCEL_ROW))
        return
    draft = await _draft(state)
    draft["title"] = title
    await _save_draft(state, draft)
    await _ask_desc(message, state)


@router.callback_query(A.filter(F.act == "utitle"), Upload.title)
async def cb_default_title(callback: CallbackQuery, state: FSMContext) -> None:
    draft = await _draft(state)
    title = (draft.get("default_title") or "").strip()[:255]
    if not title:
        await callback.answer("عنوان پیشنهادی وجود ندارد؛ عنوان را بنویسید.", show_alert=True)
        return
    draft["title"] = title
    await _save_draft(state, draft)
    await callback.answer()
    await _ask_desc(callback, state)


async def _ask_category(ui: Message | CallbackQuery, state: FSMContext, app: App, child: ChildBot, bot: Bot) -> None:
    async with app.db.session() as s:
        cats = list((await s.scalars(select(Category).where(Category.bot_id == child.id).order_by(Category.id))).all())
    if not cats:
        await _ask_access(ui, state)
        return
    await state.set_state(Upload.category)
    rows = [[btn(f"🗂 {trunc(c.name)}", "ucat", c.id)] for c in cats]
    rows.append([btn("بدون دسته‌بندی", "ucat", 0)])
    rows.append(CANCEL_ROW)
    await show(ui, "🗂 دسته‌بندی را انتخاب کنید:", kb(*rows))


@router.message(Upload.desc, F.text)
async def on_desc(message: Message, state: FSMContext, app: App, child: ChildBot, bot: Bot) -> None:
    draft = await _draft(state)
    draft["description"] = (message.text or "").strip()[:800] or None
    await _save_draft(state, draft)
    await _ask_category(message, state, app, child, bot)


@router.callback_query(A.filter(F.act == "udesc"), Upload.desc)
async def cb_skip_desc(callback: CallbackQuery, state: FSMContext, app: App, child: ChildBot, bot: Bot) -> None:
    await callback.answer()
    await _ask_category(callback, state, app, child, bot)


@router.callback_query(A.filter(F.act == "ucat"), Upload.category)
async def cb_category(callback: CallbackQuery, callback_data: A, state: FSMContext) -> None:
    draft = await _draft(state)
    draft["category_id"] = callback_data.id or None
    await _save_draft(state, draft)
    await callback.answer()
    await _ask_access(callback, state)


async def _ask_access(ui: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Upload.access)
    await show(
        ui,
        "🔐 <b>نوع دسترسی</b> را انتخاب کنید:\n\n"
        "🆓 رایگان: کاربر پس از عضویت در کانال‌های اسپانسر (اگر تعریف کرده باشید) دریافت می‌کند.\n"
        "💎 ویژه: فقط با خرید همین محتوا یا داشتن اشتراک.",
        kb([btn("🆓 رایگان", "uacc", 0, "free"), btn("💎 ویژه", "uacc", 0, "premium")], CANCEL_ROW),
    )


@router.callback_query(A.filter(F.act == "uacc"), Upload.access)
async def cb_access(callback: CallbackQuery, callback_data: A, state: FSMContext, app: App, child: ChildBot, bot: Bot) -> None:
    draft = await _draft(state)
    draft["access"] = "premium" if callback_data.p == "premium" else "free"
    await _save_draft(state, draft)
    await callback.answer()
    if draft["access"] == "premium":
        await state.set_state(Upload.price_irt)
        await show(
            callback,
            "💰 <b>قیمت فروش تکی</b> را به تومان بفرستید.\nاگر این محتوا فقط برای مشترکین است «فقط اشتراکی» را بزنید.",
            kb([btn("فقط اشتراکی", "uprice0")], CANCEL_ROW),
        )
    else:
        await _ask_storage(callback, state, app, child, bot)


async def _ask_usd(ui: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Upload.price_usd)
    await show(
        ui,
        "🪙 <b>قیمت به دلار</b> را بفرستید (مثلاً 2.5) تا پرداخت با ارز دیجیتال هم فعال شود، یا رد کنید.",
        kb([btn("⏭ رد کردن", "uusd0")], CANCEL_ROW),
    )


@router.message(Upload.price_irt, F.text)
async def on_price_irt(message: Message, state: FSMContext) -> None:
    n = parse_int(message.text or "")
    if n is None:
        await message.answer("❌ فقط عدد (تومان) بفرستید.", reply_markup=kb(CANCEL_ROW))
        return
    draft = await _draft(state)
    draft["price_irt"] = n or None
    await _save_draft(state, draft)
    await _ask_usd(message, state)


@router.callback_query(A.filter(F.act == "uprice0"), Upload.price_irt)
async def cb_price0(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _ask_usd(callback, state)


@router.message(Upload.price_usd, F.text)
async def on_price_usd(message: Message, state: FSMContext, app: App, child: ChildBot, bot: Bot) -> None:
    cents = parse_cents(message.text or "")
    if cents is None:
        await message.answer("❌ عدد معتبر بفرستید (مثلاً 2.5).", reply_markup=kb(CANCEL_ROW))
        return
    draft = await _draft(state)
    draft["price_usd_cents"] = cents or None
    await _save_draft(state, draft)
    await _ask_storage(message, state, app, child, bot)


@router.callback_query(A.filter(F.act == "uusd0"), Upload.price_usd)
async def cb_usd0(callback: CallbackQuery, state: FSMContext, app: App, child: ChildBot, bot: Bot) -> None:
    await callback.answer()
    await _ask_storage(callback, state, app, child, bot)


async def _ask_storage(ui: Message | CallbackQuery, state: FSMContext, app: App, child: ChildBot, bot: Bot) -> None:
    draft = await _draft(state)
    if draft.get("kind") == "link" or not child.storage_channel_id:
        await _finalize(ui, state, app, child, bot)
        return
    await state.set_state(Upload.storage)
    default = draft.get("storage", "telegram")
    await show(
        ui,
        "🗄 فایل کجا ذخیره شود؟\n\n"
        "📡 <b>مستقیم در تلگرام</b>: ساده و سریع.\n"
        "🗄 <b>کانال ذخیره‌ساز</b>: فایل در کانال خصوصی شما هم نگه‌داری می‌شود؛ امن‌تر در برابر مشکلات ربات.",
        kb(
            [
                btn(("✅ " if default == "telegram" else "") + "📡 تلگرام مستقیم", "ustore", 0, "telegram"),
                btn(("✅ " if default == "channel" else "") + "🗄 کانال ذخیره‌ساز", "ustore", 0, "channel"),
            ],
            CANCEL_ROW,
        ),
    )


@router.callback_query(A.filter(F.act == "ustore"), Upload.storage)
async def cb_storage(callback: CallbackQuery, callback_data: A, state: FSMContext, app: App, child: ChildBot, bot: Bot) -> None:
    draft = await _draft(state)
    draft["storage"] = "channel" if callback_data.p == "channel" else "telegram"
    await _save_draft(state, draft)
    await callback.answer()
    await _finalize(callback, state, app, child, bot)


async def _finalize(ui: Message | CallbackQuery, state: FSMContext, app: App, child: ChildBot, bot: Bot) -> None:
    draft = await _draft(state)
    if not draft.get("title"):
        await state.clear()
        await show(ui, "❌ اطلاعات ناقص بود؛ دوباره شروع کنید.", kb([btn("⚙️ پنل", "panel")]))
        return
    storage = "external" if draft["kind"] == "link" else (draft.get("storage") or "telegram")
    storage_chat = storage_msg = None
    if storage == "channel":
        if not child.storage_channel_id:
            storage = "telegram"
        else:
            try:
                copied = await bot.copy_message(child.storage_channel_id, draft["src_chat_id"], draft["src_message_id"])
                storage_chat, storage_msg = child.storage_channel_id, copied.message_id
            except TelegramAPIError as exc:
                await show(
                    ui,
                    f"❌ ذخیره در کانال انجام نشد: {esc(exc)}\nمطمئن شوید ربات در کانال ادمین است و اجازه ارسال پیام دارد.",
                    kb([btn("📡 ذخیره مستقیم در تلگرام", "ustore", 0, "telegram")], CANCEL_ROW),
                )
                return

    async with app.db.session() as s:
        content = Content(
            bot_id=child.id,
            title=draft["title"],
            description=draft.get("description"),
            kind=draft["kind"],
            storage=storage,
            file_id=draft.get("file_id"),
            file_unique_id=draft.get("file_unique_id"),
            storage_chat_id=storage_chat,
            storage_message_id=storage_msg,
            external_url=draft.get("external_url"),
            duration=draft.get("duration"),
            file_size=draft.get("file_size"),
            category_id=draft.get("category_id"),
            access=draft.get("access", "free"),
            price_irt=draft.get("price_irt"),
            price_usd_cents=draft.get("price_usd_cents"),
        )
        s.add(content)
        await s.commit()
    await state.clear()
    price = f"\n💰 {price_text(content.price_irt, content.price_usd_cents)}" if content.access == "premium" and (content.price_irt or content.price_usd_cents) else ""
    await show(
        ui,
        f"✅ <b>ثبت شد</b>\n\n{KIND_ICON[content.kind]} {esc(content.title)}\n"
        f"{'💎 ویژه' if content.access == 'premium' else '🆓 رایگان'}{price}\n\n"
        f"🔗 لینک اشتراک‌گذاری:\n<code>{deep_link(child, content.id)}</code>",
        kb([btn("➕ محتوای دیگر", "uadd"), btn("📚 مدیریت محتوا", "cl", 0)], [btn("⚙️ پنل", "panel")]),
    )


# ----------------------------------------------------------------------------- list / detail
@router.callback_query(A.filter(F.act == "cl"))
async def cb_list(callback: CallbackQuery, callback_data: A, state: FSMContext, app: App, child: ChildBot) -> None:
    await state.clear()
    page = callback_data.id
    async with app.db.session() as s:
        total = int(await s.scalar(select(func.count()).select_from(Content).where(Content.bot_id == child.id)) or 0)
        items = list(
            (
                await s.scalars(
                    select(Content).where(Content.bot_id == child.id).order_by(Content.id.desc()).offset(page * PAGE).limit(PAGE)
                )
            ).all()
        )
    if not items and page == 0:
        await show(callback, "هنوز محتوایی ثبت نشده است. یک فایل برای ربات بفرستید.", kb([btn("📤 افزودن محتوا", "uadd")], [btn("🔙 پنل", "panel")]))
        await callback.answer()
        return
    rows = [
        [btn(f"{'💎' if c.access == 'premium' else '🆓'}{'' if c.is_active else '🚫'} {KIND_ICON.get(c.kind, '')} {trunc(c.title, 30)}", "cd", c.id, str(page))]
        for c in items
    ]
    nav = []
    if page > 0:
        nav.append(btn("◀️ قبلی", "cl", page - 1))
    if (page + 1) * PAGE < total:
        nav.append(btn("بعدی ▶️", "cl", page + 1))
    if nav:
        rows.append(nav)
    rows.append([btn("🔙 پنل", "panel")])
    await show(callback, f"📚 <b>محتواها</b> ({fmt_int(total)})", kb(*rows))
    await callback.answer()


async def _get_content(app: App, child: ChildBot, content_id: int) -> Content | None:
    async with app.db.session() as s:
        c = await s.get(Content, content_id)
    return c if c is not None and c.bot_id == child.id else None


async def render_detail(ui: Message | CallbackQuery, app: App, child: ChildBot, content_id: int, page: int = 0) -> None:
    async with app.db.session() as s:
        c = await s.get(Content, content_id)
        cat = await s.get(Category, c.category_id) if c and c.category_id else None
    if c is None or c.bot_id != child.id:
        await show(ui, "محتوا پیدا نشد.", kb([btn("🔙 لیست", "cl", page)]))
        return
    storage = {"telegram": "تلگرام (مستقیم)", "channel": "کانال ذخیره‌ساز", "external": "لینک خارجی"}.get(c.storage, c.storage)
    lines = [
        f"{KIND_ICON.get(c.kind, '📦')} <b>{esc(c.title)}</b>",
        f"نوع: {KIND_LABEL.get(c.kind, c.kind)}" + (f" · {human_size(c.file_size)}" if c.file_size else ""),
        f"دسترسی: {'💎 ویژه' if c.access == 'premium' else '🆓 رایگان'}",
    ]
    if c.access == "premium":
        lines.append(f"قیمت: {price_text(c.price_irt, c.price_usd_cents) or 'فقط اشتراکی'}")
    lines += [
        f"ذخیره‌سازی: {storage}",
        f"دسته: {esc(cat.name) if cat else '—'}",
        f"وضعیت: {'فعال' if c.is_active else '🚫 غیرفعال'}",
        f"📥 دریافت‌ها: {fmt_int(c.downloads)}",
        f"📅 {jdate(c.created_at)}",
    ]
    if c.description:
        lines += ["", esc(trunc(c.description, 200))]
    lines += ["", f"🔗 <code>{deep_link(child, c.id)}</code>"]
    p = str(page)
    markup = kb(
        [btn("✏️ عنوان", "ce", c.id, "title"), btn("📝 توضیحات", "ce", c.id, "desc")],
        [btn("🔁 تغییر دسترسی", "ct", c.id, "access"), btn("👁 فعال/غیرفعال", "ct", c.id, "active")],
        [btn("💰 قیمت (تومان)", "ce", c.id, "price_irt"), btn("🪙 قیمت (دلار)", "ce", c.id, "price_usd")],
        [btn("🗂 دسته‌بندی", "ce", c.id, "cat"), btn("👀 پیش‌نمایش", "cs", c.id)],
        [btn("🗑 حذف", "cx", c.id, p), btn("🔙 لیست", "cl", page)],
    )
    await show(ui, "\n".join(lines), markup)


@router.callback_query(A.filter(F.act == "cd"))
async def cb_detail(callback: CallbackQuery, callback_data: A, state: FSMContext, app: App, child: ChildBot) -> None:
    await state.clear()
    await render_detail(callback, app, child, callback_data.id, int(callback_data.p) if callback_data.p.isdigit() else 0)
    await callback.answer()


@router.callback_query(A.filter(F.act == "cs"))
async def cb_preview(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot, bot: Bot) -> None:
    c = await _get_content(app, child, callback_data.id)
    if c is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    await callback.answer()
    try:
        await send_content(bot, child, c, callback.from_user.id)
    except ContentUnavailable as exc:
        await bot.send_message(callback.from_user.id, f"⚠️ ارسال این فایل ممکن نیست: {esc(exc)}\nفایل را دوباره آپلود کنید.")


@router.callback_query(A.filter(F.act == "ct"))
async def cb_toggle(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    async with app.db.session() as s:
        c = await s.get(Content, callback_data.id)
        if c is None or c.bot_id != child.id:
            await callback.answer("پیدا نشد.", show_alert=True)
            return
        if callback_data.p == "access":
            c.access = "free" if c.access == "premium" else "premium"
        elif callback_data.p == "active":
            c.is_active = not c.is_active
        await s.commit()
    await callback.answer("انجام شد.")
    await render_detail(callback, app, child, callback_data.id)


_EDIT_PROMPTS = {
    "title": "✏️ عنوان جدید را بفرستید:",
    "desc": "📝 توضیحات جدید را بفرستید (برای حذف توضیحات «-» بفرستید):",
    "price_irt": "💰 قیمت جدید به تومان را بفرستید (برای حذف قیمت، 0):",
    "price_usd": "🪙 قیمت جدید به دلار را بفرستید، مثل 2.5 (برای حذف، 0):",
}


@router.callback_query(A.filter(F.act == "ce"))
async def cb_edit(callback: CallbackQuery, callback_data: A, state: FSMContext, app: App, child: ChildBot) -> None:
    c = await _get_content(app, child, callback_data.id)
    if c is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    field = callback_data.p
    if field == "cat":
        async with app.db.session() as s:
            cats = list((await s.scalars(select(Category).where(Category.bot_id == child.id).order_by(Category.id))).all())
        if not cats:
            await callback.answer("ابتدا از پنل یک دسته‌بندی بسازید.", show_alert=True)
            return
        rows = [[btn(f"🗂 {trunc(x.name)}", "cec", c.id, str(x.id))] for x in cats]
        rows.append([btn("بدون دسته‌بندی", "cec", c.id, "0")])
        rows.append([btn("🔙 بازگشت", "cd", c.id)])
        await show(callback, "🗂 دسته‌بندی را انتخاب کنید:", kb(*rows))
        await callback.answer()
        return
    if field not in _EDIT_PROMPTS:
        await callback.answer()
        return
    await state.set_state(EditContent.value)
    await state.update_data(content_id=c.id, field=field)
    await show(callback, _EDIT_PROMPTS[field], kb([btn("❌ انصراف", "cd", c.id)]))
    await callback.answer()


@router.callback_query(A.filter(F.act == "cec"))
async def cb_edit_category(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    async with app.db.session() as s:
        c = await s.get(Content, callback_data.id)
        if c is None or c.bot_id != child.id:
            await callback.answer("پیدا نشد.", show_alert=True)
            return
        cat_id = int(callback_data.p) if callback_data.p.isdigit() else 0
        if cat_id:
            cat = await s.get(Category, cat_id)
            if cat is None or cat.bot_id != child.id:
                await callback.answer("دسته پیدا نشد.", show_alert=True)
                return
        c.category_id = cat_id or None
        await s.commit()
    await callback.answer("انجام شد.")
    await render_detail(callback, app, child, callback_data.id)


@router.message(EditContent.value, F.text)
async def on_edit_value(message: Message, state: FSMContext, app: App, child: ChildBot) -> None:
    data = await state.get_data()
    field, text = data.get("field"), (message.text or "").strip()
    async with app.db.session() as s:
        c = await s.get(Content, int(data.get("content_id", 0)))
        if c is None or c.bot_id != child.id:
            await state.clear()
            await message.answer("محتوا پیدا نشد.")
            return
        if field == "title":
            if not text or len(text) > 255:
                await message.answer("عنوان باید بین ۱ تا ۲۵۵ حرف باشد.")
                return
            c.title = text
        elif field == "desc":
            c.description = None if text in ("-", "0") else text[:800]
        elif field == "price_irt":
            n = parse_int(text)
            if n is None:
                await message.answer("❌ فقط عدد بفرستید.")
                return
            c.price_irt = n or None
        elif field == "price_usd":
            cents = parse_cents(text)
            if cents is None:
                await message.answer("❌ عدد معتبر بفرستید (مثلاً 2.5).")
                return
            c.price_usd_cents = cents or None
        await s.commit()
        cid = c.id
    await state.clear()
    await render_detail(message, app, child, cid)


@router.callback_query(A.filter(F.act == "cx"))
async def cb_delete_ask(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    c = await _get_content(app, child, callback_data.id)
    if c is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    await show(
        callback,
        f"🗑 «{esc(c.title)}» حذف شود؟\n(لینک‌های اشتراک‌گذاری‌شده از کار می‌افتند. خریدهای قبلی ثبت می‌مانند.)",
        kb([btn("🗑 بله، حذف", "cxy", c.id)], [btn("❌ انصراف", "cd", c.id, callback_data.p)]),
    )
    await callback.answer()


@router.callback_query(A.filter(F.act == "cxy"))
async def cb_delete_do(callback: CallbackQuery, callback_data: A, app: App, child: ChildBot) -> None:
    async with app.db.session() as s:
        await s.execute(delete(Content).where(Content.id == callback_data.id, Content.bot_id == child.id))
        await s.commit()
    await callback.answer("حذف شد.")
    async with app.db.session() as s:
        total = int(await s.scalar(select(func.count()).select_from(Content).where(Content.bot_id == child.id)) or 0)
    await show(callback, "🗑 حذف شد.", kb([btn("📚 مدیریت محتوا", "cl", 0)] if total else [], [btn("⚙️ پنل", "panel")]))
