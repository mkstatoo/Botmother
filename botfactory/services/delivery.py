"""Sending content to end users (Telegram file_id / storage channel / external link)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from ..db.models import ChildBot, Content, Delivery, ScheduledDelete
from ..util import esc, human_duration, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger(__name__)

KIND_ICON = {
    "audio": "🎧",
    "video": "🎬",
    "voice": "🎙",
    "document": "📄",
    "video_note": "⭕️",
    "link": "🔗",
}
KIND_LABEL = {
    "audio": "صوت",
    "video": "ویدئو",
    "voice": "ویس",
    "document": "فایل",
    "video_note": "ویدئو پیام",
    "link": "لینک",
}


class ContentUnavailable(Exception):
    """The stored file can no longer be sent (deleted from the storage channel, …)."""


def build_caption(child: ChildBot, content: Content, limit: int = 1024) -> str:
    head = f"{KIND_ICON.get(content.kind, '📦')} <b>{esc(content.title)}</b>"
    tail = f"@{esc(child.username)}"
    desc = (content.description or "").strip()
    while True:
        parts = [head] + ([esc(desc)] if desc else []) + [tail]
        caption = "\n\n".join(parts)
        if len(caption) <= limit or not desc:
            return caption[:limit] if len(caption) > limit else caption
        desc = desc[: int(len(desc) * 0.85)].rstrip()


async def send_content(bot: Bot, child: ChildBot, content: Content, chat_id: int) -> list[int]:
    """Send one content item; returns the message ids that were created."""
    caption = build_caption(child, content)
    protect = bool(child.protect_content)

    if content.kind == "link":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔗 باز کردن لینک", url=content.external_url or "")]]
        )
        msg = await bot.send_message(
            chat_id,
            caption,
            reply_markup=kb,
            protect_content=protect,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return [msg.message_id]

    # 1) storage channel → copy (works for any file size, survives token/bot changes)
    if (
        content.storage == "channel"
        and content.storage_chat_id is not None
        and content.storage_message_id is not None
        and content.kind != "video_note"
    ):
        try:
            copied = await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=content.storage_chat_id,
                message_id=content.storage_message_id,
                caption=caption,
                protect_content=protect,
            )
            return [copied.message_id]
        except TelegramBadRequest as exc:
            if not content.file_id:
                raise ContentUnavailable(str(exc)) from exc
            log.warning("copy from storage failed (content=%s): %s — falling back to file_id", content.id, exc)

    # 2) direct file_id
    if not content.file_id:
        raise ContentUnavailable("no file_id")
    fid = content.file_id
    try:
        if content.kind == "audio":
            msg = await bot.send_audio(chat_id, fid, caption=caption, protect_content=protect, duration=content.duration)
        elif content.kind == "video":
            msg = await bot.send_video(
                chat_id, fid, caption=caption, protect_content=protect, duration=content.duration, supports_streaming=True
            )
        elif content.kind == "voice":
            msg = await bot.send_voice(chat_id, fid, caption=caption, protect_content=protect, duration=content.duration)
        elif content.kind == "video_note":
            first = await bot.send_video_note(chat_id, fid, protect_content=protect)
            second = await bot.send_message(chat_id, caption, protect_content=protect)
            return [first.message_id, second.message_id]
        else:
            msg = await bot.send_document(chat_id, fid, caption=caption, protect_content=protect)
    except TelegramBadRequest as exc:
        raise ContentUnavailable(str(exc)) from exc
    return [msg.message_id]


async def deliver(app: App, bot: Bot, child: ChildBot, content: Content, chat_id: int, user_id: int) -> list[int]:
    """Send content, log it and schedule auto-deletion if the bot is configured so."""
    ids = await send_content(bot, child, content, chat_id)

    if child.auto_delete_seconds and content.kind != "link":
        notice = await bot.send_message(
            chat_id,
            f"⏳ این پیام تا {human_duration(child.auto_delete_seconds)} دیگر حذف می‌شود؛ آن را ذخیره کنید.",
        )
        ids.append(notice.message_id)

    async with app.db.session() as s:
        s.add(Delivery(bot_id=child.id, user_id=user_id, content_id=content.id))
        db_content = await s.get(Content, content.id)
        if db_content is not None:
            db_content.downloads = (db_content.downloads or 0) + 1
        if child.auto_delete_seconds and content.kind != "link":
            due = utcnow() + timedelta(seconds=child.auto_delete_seconds)
            for mid in ids:
                s.add(ScheduledDelete(bot_id=child.id, chat_id=chat_id, message_id=mid, delete_at=due))
        await s.commit()
    return ids
