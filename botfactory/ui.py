"""Tiny UI helpers shared by the mother and child bots."""

from __future__ import annotations

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .callbacks import A


def btn(text: str, act: str, id: int = 0, p: str = "") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=A(act=act, id=id, p=p).pack())


def url_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


async def show(event: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Edit the message behind a button press, or reply to a plain message."""
    if isinstance(event, CallbackQuery):
        msg = event.message
        if isinstance(msg, Message):
            try:
                await msg.edit_text(text, reply_markup=markup)
                return
            except TelegramBadRequest as exc:
                if "not modified" in str(exc):
                    return
            except TelegramAPIError:
                pass
            await msg.answer(text, reply_markup=markup)
        else:  # inaccessible message
            await event.bot.send_message(event.from_user.id, text, reply_markup=markup)  # type: ignore[union-attr]
    else:
        await event.answer(text, reply_markup=markup)


async def safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except TelegramAPIError:
        pass
