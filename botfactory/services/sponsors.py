"""Sponsor channels: membership checks and channel resolution."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from aiogram import Bot
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError

from ..db.models import Sponsor

log = logging.getLogger(__name__)

_JOINED = {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER}


class SponsorError(Exception):
    """Raised with a user-facing (Persian) message."""


async def is_member(bot: Bot, chat_id: int, user_id: int) -> bool | None:
    """True/False, or None when membership cannot be verified (bot not admin, chat gone…)."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError as exc:
        log.warning("cannot verify membership chat=%s user=%s: %s", chat_id, user_id, exc)
        return None
    if member.status in _JOINED:
        return True
    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return False


async def missing_sponsors(
    bot: Bot, sponsors: list[Sponsor], user_id: int
) -> tuple[list[Sponsor], list[Sponsor]]:
    """Return (sponsors the user has not joined, sponsors that could not be verified).

    Unverifiable sponsors never block the user (fail-open): a misconfigured channel
    must not lock every user out of the content.
    """
    results = await asyncio.gather(*(is_member(bot, s.chat_id, user_id) for s in sponsors))
    missing = [s for s, ok in zip(sponsors, results) if ok is False]
    unverifiable = [s for s, ok in zip(sponsors, results) if ok is None]
    return missing, unverifiable


def join_url(sponsor: Sponsor) -> str | None:
    if sponsor.username:
        return f"https://t.me/{sponsor.username}"
    return sponsor.invite_link or None


_REF_RE = re.compile(r"^(?:https?://)?(?:t(?:elegram)?\.me/)?@?([A-Za-z][A-Za-z0-9_]{3,31})/?$")


def normalize_chat_ref(text: str) -> str | int | None:
    """Parse '@name', 't.me/name', 'https://t.me/name' or a numeric id (-100…)."""
    text = (text or "").strip()
    if re.fullmatch(r"-?\d{5,}", text):
        return int(text)
    m = _REF_RE.match(text)
    return f"@{m.group(1)}" if m else None


@dataclass
class ChannelInfo:
    chat_id: int
    title: str
    username: str | None
    invite_link: str | None


async def resolve_channel(bot: Bot, ref: str | int) -> ChannelInfo:
    """Resolve a channel/group and make sure the bot is an administrator there."""
    try:
        chat = await bot.get_chat(ref)
    except TelegramAPIError as exc:
        raise SponsorError("کانال پیدا نشد. آیدی را بررسی کنید و مطمئن شوید ربات در آن ادمین است.") from exc
    if chat.type not in (ChatType.CHANNEL, ChatType.SUPERGROUP, ChatType.GROUP):
        raise SponsorError("فقط کانال یا گروه قابل قبول است.")
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
    except TelegramAPIError as exc:
        raise SponsorError("ربات به این کانال دسترسی ندارد. ابتدا ربات را ادمین کانال کنید.") from exc
    if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        raise SponsorError("ربات باید در این کانال ادمین باشد تا بتواند عضویت کاربران را بررسی کند.")

    link: str | None = None
    if not chat.username:
        link = getattr(chat, "invite_link", None)
        if not link:
            try:
                link = await bot.export_chat_invite_link(chat.id)
            except TelegramAPIError:
                link = None
    return ChannelInfo(chat.id, chat.title or str(chat.id), chat.username, link)
