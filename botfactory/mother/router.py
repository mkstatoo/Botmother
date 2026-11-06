"""Mother bot: lets content providers create and manage their own bots."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    Message,
    Update,
)
from sqlalchemy import func, select, update

from ..callbacks import A
from ..db.models import BotUser, ChildBot, Content, Creator, Delivery
from ..services.botops import bot_counts, purge_bot
from ..ui import btn, kb, safe_delete, show, url_btn
from ..services.billing import describe
from ..util import esc, fmt_int, fmt_toman, jdate, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
STATUS_LABEL = {"active": "✅ فعال", "paused": "⏸ متوقف (توسط شما)", "suspended": "⛔️ مسدود (توسط مدیریت)", "expired": "💤 منقضی (نیاز به تمدید)", "invalid": "⚠️ توکن نامعتبر"}
PAGE = 8


class NewBot(StatesGroup):
    token = State()


class ChangeToken(StatesGroup):
    token = State()


class AdminBroadcast(StatesGroup):
    message = State()
    confirm = State()


router = Router(name="mother")
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


class CreatorMiddleware(BaseMiddleware):
    def __init__(self, app: App) -> None:
        self.app = app

    async def __call__(self, handler, event: Update, data: dict[str, Any]):
        user = data.get("event_from_user")
        if user is None or user.is_bot:
            return None
        async with self.app.db.session() as s:
            creator = await s.get(Creator, user.id)
            if creator is None:
                creator = Creator(id=user.id, username=user.username, full_name=user.full_name)
                s.add(creator)
            elif creator.username != user.username or creator.full_name != user.full_name:
                creator.username, creator.full_name = user.username, user.full_name
            await s.commit()
        if creator.is_banned:
            return None
        data["app"] = self.app
        data["creator"] = creator
        data["is_admin"] = user.id in self.app.settings.admin_ids
        return await handler(event, data)


# ----------------------------------------------------------------------------- home
def home_markup(is_admin: bool):
    rows = [
        [btn("➕ ساخت ربات جدید", "new")],
        [btn("🤖 ربات‌های من", "list")],
        [btn("❓ راهنما", "help")],
    ]
    if is_admin:
        rows.append([btn("🛠 مدیریت پلتفرم", "adm")])
    return kb(*rows)


HOME_TEXT = (
    "👋 به <b>کارخانه ربات</b> خوش آمدید!\n\n"
    "اینجا می‌توانید برای محتوای صوتی و ویدئویی‌تان یک ربات تلگرامی اختصاصی بسازید:\n"
    "• محتوای رایگان با شرط عضویت در کانال‌های اسپانسر\n"
    "• فروش تکی محتوا و اشتراک (درگاه بانکی، ارز دیجیتال یا واریز دستی)\n"
    "• ذخیره‌سازی ترکیبی: آپلود مستقیم، کانال ذخیره‌ساز یا لینک خارجی\n\n"
    "برای شروع «ساخت ربات جدید» را بزنید."
)

HELP_TEXT = (
    "❓ <b>راهنما</b>\n\n"
    "<b>۱. ساخت ربات</b>\n«ساخت ربات جدید» را بزنید؛ مراحل گرفتن توکن از @BotFather قدم‌به‌قدم توضیح داده شده. بعد از آن چند سؤال ساده می‌پرسیم و ربات آماده می‌شود.\n\n"
    "<b>۲. مدیریت</b>\nداخل ربات خودتان /start بزنید و «⚙️ پنل مدیریت» را باز کنید. هر فایل صوتی/ویدئویی را "
    "برای ربات بفرستید تا به‌عنوان محتوا ثبت شود.\n\n"
    "<b>۳. اسپانسر</b>\nربات را ادمین کانال‌های اسپانسر کنید و در پنل، بخش «کانال‌های اسپانسر» آن‌ها را اضافه کنید.\n\n"
    "<b>۴. فروش</b>\nدر پنل، بخش «پلن‌ها و پرداخت» را تنظیم کنید. وجه مستقیماً به حساب درگاه خودتان می‌رود.\n\n"
    "<b>۵. ذخیره‌سازی</b>\nبرای فایل‌های حجیم و جلوگیری از گم‌شدن فایل، یک کانال خصوصی بسازید، ربات را ادمین کنید و در "
    "«تنظیمات» به‌عنوان کانال ذخیره‌ساز ثبتش کنید."
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, is_admin: bool) -> None:
    await state.clear()
    await message.answer(HOME_TEXT, reply_markup=home_markup(is_admin))


@router.callback_query(A.filter(F.act == "home"))
async def cb_home(callback: CallbackQuery, state: FSMContext, is_admin: bool) -> None:
    await state.clear()
    await show(callback, HOME_TEXT, home_markup(is_admin))
    await callback.answer()


@router.callback_query(A.filter(F.act == "help"))
async def cb_help(callback: CallbackQuery) -> None:
    await show(callback, HELP_TEXT, kb([btn("🔙 بازگشت", "home")]))
    await callback.answer()


# ----------------------------------------------------------------------------- create
@router.callback_query(A.filter(F.act == "new"))
async def cb_new(callback: CallbackQuery, state: FSMContext, app: App, creator: Creator) -> None:
    async with app.db.session() as s:
        count = int(await s.scalar(select(func.count()).select_from(ChildBot).where(ChildBot.owner_id == creator.id)) or 0)
    if count >= app.settings.max_bots_per_user:
        await callback.answer(f"حداکثر {fmt_int(app.settings.max_bots_per_user)} ربات می‌توانید بسازید.", show_alert=True)
        return
    await state.set_state(NewBot.token)
    await show(
        callback,
        "🔑 <b>ساخت ربات جدید</b>\n\n"
        "ربات شما را باید خود تلگرام (از طریق @BotFather) صادر کند؛ فقط ۳ دقیقه کار دارد:\n\n"
        "<b>۱)</b> دکمه‌ی «باز کردن BotFather» را بزنید و Start کنید.\n"
        "<b>۲)</b> بنویسید <code>/newbot</code>\n"
        "<b>۳)</b> یک <b>نام</b> دلخواه بفرستید (مثلاً: آموزش‌های صوتی علی)\n"
        "<b>۴)</b> یک <b>یوزرنیم انگلیسی</b> بفرستید که به <code>bot</code> ختم شود (مثلاً: ali_audio_bot)\n"
        "<b>۵)</b> BotFather یک پیام طولانی می‌فرستد؛ خطی که بعد از «Use this token» است (شبیه <code>123456:ABC…</code>) را <b>کپی</b> کنید و همین‌جا بفرستید.\n\n"
        "🔒 توکن مثل رمز عبور است؛ پیام شما بعد از دریافت پاک می‌شود و توکن رمزنگاری‌شده ذخیره می‌گردد.",
        kb([url_btn("🤖 باز کردن BotFather", "https://t.me/BotFather")], [btn("❌ انصراف", "home")]),
    )
    await callback.answer()


async def _setup_child_commands(app: App, child: ChildBot) -> None:
    bot = app.manager.get_bot(child.id)
    if bot is None:
        return
    with suppress(TelegramAPIError):
        await bot.set_my_commands([BotCommand(command="start", description="شروع / منوی اصلی")])
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="شروع / منوی اصلی"),
                BotCommand(command="panel", description="پنل مدیریت"),
            ],
            scope=BotCommandScopeChat(chat_id=child.owner_id),
        )


async def _validate_token(app: App, token: str):
    """Returns (me, error_text)."""
    if not TOKEN_RE.match(token):
        return None, "❌ فرمت توکن درست نیست. توکن شبیه <code>123456:ABC-DEF…</code> است. دوباره بفرستید."
    bot = app.make_bot(token)
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        return None, "❌ این توکن معتبر نیست یا باطل شده است. توکن درست را از BotFather بفرستید."
    except (TelegramAPIError, aiohttp_errors()):
        return None, "⚠️ ارتباط با تلگرام برقرار نشد. کمی بعد دوباره تلاش کنید."
    finally:
        with suppress(Exception):
            await bot.session.close()
    if app.mother_bot is not None and me.id == app.mother_bot.id:
        return None, "❌ این توکن مربوط به همین ربات است. باید یک ربات جدید بسازید."
    return me, None


def aiohttp_errors():
    import aiohttp

    return (aiohttp.ClientError, asyncio.TimeoutError)


@router.message(NewBot.token, F.text)
async def on_new_token(message: Message, state: FSMContext, app: App, creator: Creator) -> None:
    token = (message.text or "").strip()
    await safe_delete(message)
    me, error = await _validate_token(app, token)
    if error or me is None:
        await message.answer(error or "خطا", reply_markup=kb([btn("❌ انصراف", "home")]))
        return

    async with app.db.session() as s:
        existing = await s.get(ChildBot, me.id)
        if existing is not None and existing.owner_id != creator.id:
            await message.answer("❌ این ربات قبلاً توسط کاربر دیگری ثبت شده است.", reply_markup=kb([btn("🔙 بازگشت", "home")]))
            return
        count = int(await s.scalar(select(func.count()).select_from(ChildBot).where(ChildBot.owner_id == creator.id)) or 0)
        if existing is None and count >= app.settings.max_bots_per_user:
            await message.answer("❌ به سقف تعداد ربات رسیده‌اید.", reply_markup=kb([btn("🔙 بازگشت", "home")]))
            return
        if existing is None:
            child = ChildBot(
                id=me.id,
                owner_id=creator.id,
                username=me.username or "",
                name=me.full_name,
                token_enc=app.box.encrypt(token),
                webhook_secret=secrets.token_urlsafe(24),
                service_until=await app.billing.trial_end(),
            )
            s.add(child)
        else:
            child = existing
            child.token_enc = app.box.encrypt(token)
            child.username, child.name = me.username or "", me.full_name
            if child.status == "invalid":
                child.status = "active"
        await s.commit()

    await state.clear()
    await app.manager.restart(child)
    await _setup_child_commands(app, child)
    cfg = await app.billing.config()
    trial_note = f"🎁 {fmt_int(cfg.trial_days)} روز اول کاملاً رایگان است.\n\n" if cfg.trial_days and cfg.price_irt else ""
    await message.answer(
        f"✅ ربات @{esc(child.username)} ساخته و فعال شد!\n\n{trial_note}"
        "حالا با چند سؤال ساده آماده‌اش کنیم (کمتر از ۲ دقیقه). هر مرحله را می‌توانید رد کنید.",
        reply_markup=kb(
            [btn("⚡ تنظیم سریع (پیشنهادی)", "wz", child.id, "start")],
            [url_btn("⚙️ باز کردن پنل ربات", f"https://t.me/{child.username}?start=panel")],
            [btn("🤖 ربات‌های من", "list")],
        ),
    )


# ----------------------------------------------------------------------------- list / detail
async def _my_bots(app: App, owner_id: int) -> list[ChildBot]:
    async with app.db.session() as s:
        return list((await s.scalars(select(ChildBot).where(ChildBot.owner_id == owner_id).order_by(ChildBot.created_at))).all())


@router.callback_query(A.filter(F.act == "list"))
async def cb_list(callback: CallbackQuery, state: FSMContext, app: App, creator: Creator) -> None:
    await state.clear()
    bots = await _my_bots(app, creator.id)
    if not bots:
        await show(callback, "هنوز رباتی نساخته‌اید.", kb([btn("➕ ساخت ربات جدید", "new")], [btn("🔙 بازگشت", "home")]))
    else:
        rows = [[btn(f"{STATUS_LABEL.get(b.status, b.status).split()[0]} @{b.username}", "mb", b.id)] for b in bots]
        rows.append([btn("🔙 بازگشت", "home")])
        await show(callback, "🤖 <b>ربات‌های شما</b>", kb(*rows))
    await callback.answer()


async def _owned(app: App, bot_id: int, owner_id: int) -> ChildBot | None:
    async with app.db.session() as s:
        child = await s.get(ChildBot, bot_id)
    return child if child and child.owner_id == owner_id else None


async def _detail(app: App, child: ChildBot):
    async with app.db.session() as s:
        counts = await bot_counts(s, child.id)
    cfg = await app.billing.config()
    text = (
        f"🤖 <b>@{esc(child.username)}</b>\n"
        f"وضعیت: {STATUS_LABEL.get(child.status, child.status)}\n"
        f"{describe(child, cfg, utcnow())}\n"
        f"ساخته‌شده: {jdate(child.created_at)}\n\n"
        f"👥 کاربران: {fmt_int(counts['users'])}\n"
        f"🎞 محتوا: {fmt_int(counts['contents'])}\n"
        f"📥 دریافت‌ها: {fmt_int(counts['deliveries'])}"
    )
    rows = [
        [url_btn("⚙️ باز کردن پنل", f"https://t.me/{child.username}?start=panel"), url_btn("🚀 ربات", f"https://t.me/{child.username}")],
        [btn("⚡ تنظیم سریع", "wz", child.id, "start")],
    ]
    if child.service_until is not None:
        rows.append([btn("💳 تمدید سرویس", "sv", child.id)])
    if child.status == "active":
        rows.append([btn("⏸ توقف موقت", "mpause", child.id)])
    elif child.status == "paused":
        rows.append([btn("▶️ فعال‌سازی", "mresume", child.id)])
    rows.append([btn("🔑 تغییر توکن", "mtok", child.id), btn("🗑 حذف", "mdel", child.id)])
    rows.append([btn("🔙 بازگشت", "list")])
    return text, kb(*rows)


@router.callback_query(A.filter(F.act == "mb"))
async def cb_detail(callback: CallbackQuery, callback_data: A, app: App, creator: Creator) -> None:
    child = await _owned(app, callback_data.id, creator.id)
    if child is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    text, markup = await _detail(app, child)
    await show(callback, text, markup)
    await callback.answer()


@router.callback_query(A.filter(F.act.in_({"mpause", "mresume"})))
async def cb_pause(callback: CallbackQuery, callback_data: A, app: App, creator: Creator) -> None:
    child = await _owned(app, callback_data.id, creator.id)
    if child is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    new_status = "paused" if callback_data.act == "mpause" else "active"
    allowed_from = "active" if new_status == "paused" else "paused"
    async with app.db.session() as s:
        res = await s.execute(update(ChildBot).where(ChildBot.id == child.id, ChildBot.status == allowed_from).values(status=new_status))
        await s.commit()
        child = await s.get(ChildBot, child.id)  # type: ignore[assignment]
    if res.rowcount != 1:
        await callback.answer("این عملیات در وضعیت فعلی ممکن نیست.", show_alert=True)
    else:
        await callback.answer("انجام شد.")
    text, markup = await _detail(app, child)
    await show(callback, text, markup)


@router.callback_query(A.filter(F.act == "mdel"))
async def cb_delete_ask(callback: CallbackQuery, callback_data: A, app: App, creator: Creator) -> None:
    child = await _owned(app, callback_data.id, creator.id)
    if child is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    await show(
        callback,
        f"⚠️ ربات @{esc(child.username)} و <b>همه محتوا، کاربران و سوابق پرداخت آن</b> برای همیشه حذف می‌شود. مطمئنید؟",
        kb([btn("🗑 بله، حذف شود", "mdely", child.id)], [btn("❌ انصراف", "mb", child.id)]),
    )
    await callback.answer()


@router.callback_query(A.filter(F.act == "mdely"))
async def cb_delete_do(callback: CallbackQuery, callback_data: A, app: App, creator: Creator) -> None:
    child = await _owned(app, callback_data.id, creator.id)
    if child is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    await app.manager.stop(child.id, delete_webhook=True)
    async with app.db.session() as s:
        await purge_bot(s, child.id)
        await s.commit()
    await show(callback, "🗑 ربات حذف شد.", kb([btn("🤖 ربات‌های من", "list")], [btn("🔙 منوی اصلی", "home")]))
    await callback.answer()


# ----------------------------------------------------------------------------- change token
@router.callback_query(A.filter(F.act == "mtok"))
async def cb_token_ask(callback: CallbackQuery, callback_data: A, state: FSMContext, app: App, creator: Creator) -> None:
    child = await _owned(app, callback_data.id, creator.id)
    if child is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    await state.set_state(ChangeToken.token)
    await state.update_data(bot_id=child.id)
    await show(
        callback,
        f"🔑 توکن جدید ربات @{esc(child.username)} را بفرستید.\n"
        "فقط توکنِ <b>همین ربات</b> پذیرفته می‌شود (مثلاً بعد از Revoke در BotFather).",
        kb([btn("❌ انصراف", "mb", child.id)]),
    )
    await callback.answer()


@router.message(ChangeToken.token, F.text)
async def on_change_token(message: Message, state: FSMContext, app: App, creator: Creator) -> None:
    data = await state.get_data()
    child = await _owned(app, int(data.get("bot_id", 0)), creator.id)
    token = (message.text or "").strip()
    await safe_delete(message)
    if child is None:
        await state.clear()
        await message.answer("ربات پیدا نشد.")
        return
    me, error = await _validate_token(app, token)
    if error or me is None:
        await message.answer(error or "خطا", reply_markup=kb([btn("❌ انصراف", "mb", child.id)]))
        return
    if me.id != child.id:
        await message.answer("❌ این توکن مربوط به ربات دیگری است.", reply_markup=kb([btn("❌ انصراف", "mb", child.id)]))
        return
    async with app.db.session() as s:
        db_child = await s.get(ChildBot, child.id)
        assert db_child is not None
        db_child.token_enc = app.box.encrypt(token)
        if db_child.status == "invalid":
            db_child.status = "active"
        await s.commit()
    await state.clear()
    await app.manager.restart(db_child)
    text, markup = await _detail(app, db_child)
    await message.answer("✅ توکن به‌روزرسانی شد.\n\n" + text, reply_markup=markup)


# ----------------------------------------------------------------------------- platform admin
def _admin_only(is_admin: bool) -> bool:
    return is_admin


@router.message(Command("admin"))
async def cmd_admin(message: Message, is_admin: bool, app: App) -> None:
    if not is_admin:
        return
    text, markup = await _admin_home(app)
    await message.answer(text, reply_markup=markup)


async def _admin_home(app: App):
    async with app.db.session() as s:
        creators = int(await s.scalar(select(func.count()).select_from(Creator)) or 0)
        banned = int(await s.scalar(select(func.count()).select_from(Creator).where(Creator.is_banned.is_(True))) or 0)
        bots = int(await s.scalar(select(func.count()).select_from(ChildBot)) or 0)
        active = int(await s.scalar(select(func.count()).select_from(ChildBot).where(ChildBot.status == "active")) or 0)
        users = int(await s.scalar(select(func.count()).select_from(BotUser)) or 0)
        contents = int(await s.scalar(select(func.count()).select_from(Content)) or 0)
        deliveries = int(await s.scalar(select(func.count()).select_from(Delivery)) or 0)
    st = await app.billing.stats()
    cfg = await app.billing.config()
    price = fmt_toman(cfg.price_irt) + f" / {fmt_int(cfg.period_days)} روز" if cfg.price_irt else "تعیین نشده"
    text = (
        "🛠 <b>مدیریت پلتفرم</b>\n\n"
        f"👤 سازندگان: {fmt_int(creators)} (مسدود: {fmt_int(banned)})\n"
        f"🤖 ربات‌ها: {fmt_int(bots)} (فعال: {fmt_int(active)})\n"
        f"👥 کاربران ربات‌ها: {fmt_int(users)}\n"
        f"🎞 محتوا: {fmt_int(contents)}\n"
        f"📥 دریافت‌ها: {fmt_int(deliveries)}\n"
        f"⚙️ ربات‌های در حال اجرا: {fmt_int(len(app.manager.runtimes))}\n\n"
        "<b>اشتراک سرویس</b>\n"
        f"💵 قیمت: {price} · {'اعمال می‌شود' if cfg.enforced else 'اعمال نمی‌شود'}\n"
        f"🎁 آزمایشی: {fmt_int(st['trial'])} · ✅ فعال: {fmt_int(st['active'])} · ⚠️ در مهلت: {fmt_int(st['grace'])} · 💤 منقضی: {fmt_int(st['expired'])} · ♾ نامحدود: {fmt_int(st['free'])}\n"
        f"💰 درآمد ۳۰ روز اخیر: {fmt_toman(st['revenue30'])}"
    )
    review = f" ({fmt_int(st['review'])})" if st["review"] else ""
    return text, kb(
        [btn("👤 سازندگان", "admc", 0), btn("🔎 جستجو", "admq")],
        [btn("🤖 لیست ربات‌ها", "adml", 0)],
        [btn(f"🧾 رسیدهای در انتظار{review}", "admrv"), btn("💰 پرداخت‌ها", "admpay")],
        [btn("⚙️ تنظیمات اشتراک", "admset")],
        [btn("📣 پیام به سازندگان", "admbc")],
        [btn("🔙 بازگشت", "home")],
    )


@router.callback_query(A.filter(F.act == "adm"))
async def cb_admin(callback: CallbackQuery, is_admin: bool, app: App, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    text, markup = await _admin_home(app)
    await show(callback, text, markup)
    await callback.answer()


@router.callback_query(A.filter(F.act == "adml"))
async def cb_admin_list(callback: CallbackQuery, callback_data: A, is_admin: bool, app: App) -> None:
    if not is_admin:
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    page = callback_data.id
    async with app.db.session() as s:
        total = int(await s.scalar(select(func.count()).select_from(ChildBot)) or 0)
        rows = list(
            (await s.scalars(select(ChildBot).order_by(ChildBot.created_at.desc()).offset(page * PAGE).limit(PAGE))).all()
        )
    buttons = [[btn(f"{STATUS_LABEL.get(b.status, b.status).split()[0]} @{b.username} · {b.owner_id}", "admb", b.id)] for b in rows]
    nav = []
    if page > 0:
        nav.append(btn("◀️ قبلی", "adml", page - 1))
    if (page + 1) * PAGE < total:
        nav.append(btn("بعدی ▶️", "adml", page + 1))
    if nav:
        buttons.append(nav)
    buttons.append([btn("🔙 بازگشت", "adm")])
    await show(callback, f"🤖 ربات‌ها ({fmt_int(total)})", kb(*buttons))
    await callback.answer()


@router.callback_query(A.filter(F.act == "admb"))
async def cb_admin_bot(callback: CallbackQuery, callback_data: A, is_admin: bool, app: App) -> None:
    if not is_admin:
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    async with app.db.session() as s:
        child = await s.get(ChildBot, callback_data.id)
        counts = await bot_counts(s, callback_data.id) if child else {}
    if child is None:
        await callback.answer("پیدا نشد.", show_alert=True)
        return
    cfg = await app.billing.config()
    text = (
        f"🤖 <b>@{esc(child.username)}</b>\n"
        f"مالک: <code>{child.owner_id}</code>\n"
        f"وضعیت: {STATUS_LABEL.get(child.status, child.status)}\n"
        f"{describe(child, cfg, utcnow())}\n"
        f"👥 {fmt_int(counts['users'])} · 🎞 {fmt_int(counts['contents'])} · 📥 {fmt_int(counts['deliveries'])}"
    )
    toggle = (
        btn("✅ رفع مسدودی", "admu", child.id) if child.status == "suspended" else btn("⛔️ مسدود کردن", "adms", child.id)
    )
    await show(
        callback,
        text,
        kb(
            [btn("🎁 +۷ روز", "admg", child.id, "7"), btn("🎁 +۳۰ روز", "admg", child.id, "30"), btn("🎁 روز دلخواه", "admgc", child.id)],
            [btn("♾ نامحدود", "admgu", child.id), btn("⏹ پایان سرویس", "admge", child.id)],
            [toggle, btn("👤 مالک", "admcd", child.owner_id)],
            [btn("🔙 بازگشت", "adml", 0)],
        ),
    )
    await callback.answer()


@router.callback_query(A.filter(F.act.in_({"adms", "admu"})))
async def cb_admin_suspend(callback: CallbackQuery, callback_data: A, is_admin: bool, app: App) -> None:
    if not is_admin:
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    new_status = "suspended" if callback_data.act == "adms" else "active"
    async with app.db.session() as s:
        await s.execute(update(ChildBot).where(ChildBot.id == callback_data.id).values(status=new_status))
        await s.commit()
        child = await s.get(ChildBot, callback_data.id)
    if child is not None:
        await app.notify_creator(
            child.owner_id,
            f"⛔️ ربات @{esc(child.username)} توسط مدیریت پلتفرم مسدود شد."
            if new_status == "suspended"
            else f"✅ مسدودی ربات @{esc(child.username)} برداشته شد.",
        )
    await cb_admin_bot(callback, callback_data, is_admin, app)


@router.callback_query(A.filter(F.act == "admbc"))
async def cb_admin_bc(callback: CallbackQuery, is_admin: bool, state: FSMContext) -> None:
    if not is_admin:
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminBroadcast.message)
    await show(callback, "پیامی را که می‌خواهید برای همه سازندگان ارسال شود بفرستید (هر نوع پیام).", kb([btn("❌ انصراف", "adm")]))
    await callback.answer()


@router.message(AdminBroadcast.message)
async def on_admin_bc_message(message: Message, is_admin: bool, state: FSMContext, app: App) -> None:
    if not is_admin:
        return
    async with app.db.session() as s:
        total = int(await s.scalar(select(func.count()).select_from(Creator).where(Creator.is_banned.is_(False))) or 0)
    await state.set_state(AdminBroadcast.confirm)
    await state.update_data(chat_id=message.chat.id, message_id=message.message_id)
    await message.answer(
        f"این پیام برای {fmt_int(total)} سازنده ارسال شود؟",
        reply_markup=kb([btn("✅ ارسال", "admbcy")], [btn("❌ انصراف", "adm")]),
    )


@router.callback_query(A.filter(F.act == "admbcy"), AdminBroadcast.confirm)
async def cb_admin_bc_go(callback: CallbackQuery, is_admin: bool, state: FSMContext, app: App) -> None:
    if not is_admin:
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return
    data = await state.get_data()
    await state.clear()
    await callback.answer("ارسال شروع شد.")
    await show(callback, "📣 ارسال آغاز شد؛ نتیجه را همین‌جا می‌فرستم.", kb([btn("🔙 بازگشت", "adm")]))
    app.spawn(_broadcast_creators(app, callback.from_user.id, int(data["chat_id"]), int(data["message_id"])))


async def _broadcast_creators(app: App, admin_id: int, from_chat: int, message_id: int) -> None:
    bot = app.mother_bot
    if bot is None:
        return
    async with app.db.session() as s:
        ids = list((await s.scalars(select(Creator.id).where(Creator.is_banned.is_(False)))).all())
    sent = failed = 0
    for uid in ids:
        try:
            await bot.copy_message(uid, from_chat, message_id)
            sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 0.5)
            failed += 1
        except (TelegramForbiddenError, TelegramAPIError):
            failed += 1
        await asyncio.sleep(1 / max(1, app.settings.broadcast_rate))
    with suppress(TelegramAPIError):
        await bot.send_message(admin_id, f"📣 پایان ارسال: موفق {fmt_int(sent)} · ناموفق {fmt_int(failed)}")


@router.message(F.text & ~F.text.startswith("/"))
async def fallback_text(message: Message, is_admin: bool) -> None:
    await message.answer("از منوی زیر استفاده کنید 👇", reply_markup=home_markup(is_admin))
