"""Main-channel gate shared by the mother bot and the child bots."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, Update

from .services.mainchannel import PAYLOAD_RE, MainChannel
from .ui import kb, url_btn

if TYPE_CHECKING:  # pragma: no cover
    from .app import App

log = logging.getLogger(__name__)


def _start_payload(message: Message | None) -> str:
    text = (message.text or "") if message else ""
    if text.startswith("/start "):
        payload = text[7:].strip()
        return payload if PAYLOAD_RE.match(payload) else ""
    return ""


async def enforce(app: App, event: Update, bot: Bot, user_id: int, scope: str) -> bool:
    """Return True when the update may go on; otherwise the join prompt was sent and it must stop."""
    msg, cb = event.message, event.callback_query
    if msg is None and cb is None:
        return True
    if cb is not None and (cb.data or "").startswith("a:mchk:"):
        return True  # the "I joined" button must always reach its handler
    chat = msg.chat if msg is not None else (cb.message.chat if cb is not None and cb.message else None)
    if chat is not None and chat.type != "private":
        return True
    cfg = await app.mainchan.blocked(user_id, scope)
    if cfg is None:
        return True
    kind = "این ربات" if scope == "children" else "کارخانه ربات"
    text = MainChannel.prompt_text(cfg, bot_kind=kind)
    markup = MainChannel.prompt_markup(cfg, _start_payload(msg))
    try:
        if cb is not None:
            await cb.answer("ابتدا باید در کانال اصلی عضو شوید.", show_alert=True)
        await bot.send_message(user_id, text, reply_markup=markup)
    except TelegramAPIError:
        pass
    return False


async def on_joined_pressed(app: App, callback: CallbackQuery, scope: str, bot_username: str | None) -> None:
    """Handler body of the "✅ عضو شدم" button."""
    cfg = await app.mainchan.config()
    if cfg.active and await app.mainchan.blocked(callback.from_user.id, scope, force=True) is not None:
        await callback.answer("هنوز عضو کانال نشده‌اید. ابتدا عضو شوید و دوباره بزنید.", show_alert=True)
        return
    payload = (callback.data or "").split(":", 3)[-1]
    rows = []
    if bot_username:
        link = f"https://t.me/{bot_username}" + (f"?start={payload}" if PAYLOAD_RE.match(payload) else "")
        rows.append([url_btn("▶️ ادامه", link)])
    msg = callback.message
    if isinstance(msg, Message):
        try:
            await msg.edit_text("✅ عضویت شما تأیید شد. خوش آمدید!\nبرای ادامه دکمهٔ زیر یا /start را بزنید.", reply_markup=kb(*rows) if rows else None)
        except TelegramAPIError:
            pass
    await callback.answer("✅ تأیید شد.")
