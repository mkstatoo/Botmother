"""A fake Telegram Bot API: an aiogram session that records calls and returns plausible results."""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import (
    Chat,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberMember,
    Message,
    MessageId,
    User,
)

SEND_METHODS = {
    "SendMessage", "SendAudio", "SendVideo", "SendVoice", "SendDocument", "SendVideoNote", "SendPhoto",
}


@dataclass
class Call:
    bot_id: int
    name: str
    data: dict[str, Any]


class FakeTelegram:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._ids = itertools.count(5000)
        self.members: dict[tuple[int, int], str] = {}  # (chat_id, user_id) -> status
        self.channels: dict[Any, dict[str, Any]] = {}  # id or @username -> info
        self.admin_in: set[int] = set()  # chats where every bot is administrator
        self.fail: dict[str, Callable[[Call], Exception | None]] = {}
        self.inbox: dict[int, list[dict[str, Any]]] = {}  # bot_id -> updates returned by getUpdates

    def session(self) -> FakeSession:
        return FakeSession(self)

    # -------------------------------------------------------------- inspection
    def find(self, name: str, bot_id: int | None = None, **contains: Any) -> list[Call]:
        out = []
        for c in self.calls:
            if c.name != name or (bot_id is not None and c.bot_id != bot_id):
                continue
            if all(_match(c.data.get(k), v) for k, v in contains.items()):
                out.append(c)
        return out

    def texts(self, bot_id: int, chat_id: int | None = None) -> list[str]:
        return [
            c.data.get("text") or c.data.get("caption") or ""
            for c in self.calls
            if c.bot_id == bot_id and c.name in SEND_METHODS | {"EditMessageText", "CopyMessage"}
            and (chat_id is None or c.data.get("chat_id") == chat_id)
        ]

    def last(self, name: str, bot_id: int | None = None) -> Call:
        found = self.find(name, bot_id)
        assert found, f"no {name} call recorded"
        return found[-1]

    def clear(self) -> None:
        self.calls.clear()

    # -------------------------------------------------------------- responses
    def add_channel(self, chat_id: int, title: str = "Chan", username: str | None = None, ctype: str = "channel") -> None:
        info = {"id": chat_id, "title": title, "username": username, "type": ctype}
        self.channels[chat_id] = info
        if username:
            self.channels[f"@{username}"] = info

    def respond(self, bot: Bot, name: str, data: dict[str, Any]) -> Any:
        if name == "GetMe":
            return User(id=bot.id, is_bot=True, first_name=f"Bot{bot.id}", username=f"bot{bot.id}_bot")
        if name in SEND_METHODS or name in {"EditMessageText", "EditMessageCaption"}:
            return Message(
                message_id=next(self._ids),
                date=datetime.now(),
                chat=Chat(id=data.get("chat_id", 0) if isinstance(data.get("chat_id"), int) else 0, type="private"),
            )
        if name == "CopyMessage":
            return MessageId(message_id=next(self._ids))
        if name == "GetChat":
            info = self.channels.get(data["chat_id"])
            if info is None:
                from aiogram.exceptions import TelegramBadRequest

                raise TelegramBadRequest(method=None, message="Bad Request: chat not found")  # type: ignore[arg-type]
            return Chat(**info)
        if name == "GetChatMember":
            chat_id, user_id = data["chat_id"], data["user_id"]
            user = User(id=user_id, is_bot=False, first_name="x")
            if user_id == bot.id:
                if chat_id in self.admin_in:
                    return ChatMemberAdministrator.model_construct(status="administrator", user=user, can_post_messages=True)
                return ChatMemberMember(status="member", user=user)
            status = self.members.get((chat_id, user_id), "left")
            if status == "left":
                return ChatMemberLeft(status="left", user=user)
            return ChatMemberMember(status="member", user=user)
        if name == "ExportChatInviteLink":
            return "https://t.me/+fakeinvite"
        if name == "GetUpdates":
            return []
        return True


def _match(actual: Any, expected: Any) -> bool:
    if callable(expected):
        return bool(expected(actual))
    if isinstance(expected, str) and isinstance(actual, str):
        return expected in actual
    return actual == expected


class FakeSession(BaseSession):
    def __init__(self, server: FakeTelegram) -> None:
        super().__init__()
        self.server = server

    async def close(self) -> None:
        return None

    async def stream_content(self, *a: Any, **k: Any):  # pragma: no cover
        raise NotImplementedError
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        name = type(method).__name__
        data = method.model_dump(exclude_none=True, exclude_defaults=False)
        call = Call(bot.id, name, data)
        self.server.calls.append(call)
        hook = self.server.fail.get(name)
        if hook is not None:
            exc = hook(call)
            if exc is not None:
                raise exc
        if name == "GetUpdates":
            await asyncio.sleep(0.02)
            queued, self.server.inbox[bot.id] = self.server.inbox.get(bot.id, []), []
            from aiogram.types import Update

            return [Update.model_validate(u, context={"bot": bot}) for u in queued]
        return self.server.respond(bot, name, data)


# ---------------------------------------------------------------------------
# update builders (plain dicts, validated with the bot as context like a webhook)
# ---------------------------------------------------------------------------
_update_ids = itertools.count(1)
_msg_ids = itertools.count(100)


def user_dict(uid: int, name: str = "User") -> dict[str, Any]:
    return {"id": uid, "is_bot": False, "first_name": name, "username": f"u{uid}"}


def message_dict(uid: int, text: str | None = None, **extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "message_id": next(_msg_ids),
        "date": int(time.time()),
        "chat": {"id": uid, "type": "private"},
        "from": user_dict(uid),
    }
    if text is not None:
        d["text"] = text
    d.update(extra)
    return d


def msg_update(uid: int, text: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"update_id": next(_update_ids), "message": message_dict(uid, text, **extra)}


def cb_update(uid: int, data: str, bot_id: int) -> dict[str, Any]:
    bot_msg = {
        "message_id": next(_msg_ids),
        "date": int(time.time()),
        "chat": {"id": uid, "type": "private"},
        "from": {"id": bot_id, "is_bot": True, "first_name": "Bot", "username": f"bot{bot_id}_bot"},
        "text": "menu",
    }
    return {
        "update_id": next(_update_ids),
        "callback_query": {
            "id": str(next(_update_ids)),
            "from": user_dict(uid),
            "chat_instance": "ci",
            "message": bot_msg,
            "data": data,
        },
    }


def audio_update(uid: int, title: str = "Song", file_id: str = "FID-AUDIO", size: int = 4_000_000) -> dict[str, Any]:
    return msg_update(
        uid,
        audio={"file_id": file_id, "file_unique_id": f"U-{file_id}", "duration": 120, "title": title, "file_size": size},
    )
