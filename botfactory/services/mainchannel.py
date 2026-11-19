"""Platform main channel: every creator and every end user of every child bot must join it.

Design notes
- Only the *mother* bot has to be an administrator of the channel. Membership of any
  Telegram user is checked through the mother bot, so child bots need no setup at all.
- Settings live in ``PlatformSetting`` and are cached in memory (invalidated on change),
  so the gate costs no DB query per update.
- Membership results are cached per user (joined: 15 min, not joined: 20 s, unverifiable:
  60 s) so a busy bot never turns into a flood of ``getChatMember`` calls.
- Fail-open: when membership cannot be verified (mother bot lost admin rights, channel
  deleted …) nobody is locked out.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select

from ..db.models import PlatformSetting
from ..ui import btn, kb, url_btn
from ..util import esc
from .sponsors import ChannelInfo, is_member

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

log = logging.getLogger(__name__)

KEYS = ("main_chat_id", "main_title", "main_username", "main_invite", "main_gate_children", "main_gate_creators")
TTL_JOINED, TTL_LEFT, TTL_UNKNOWN = 900.0, 20.0, 60.0
MAX_CACHE = 50_000
PAYLOAD_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


@dataclass(frozen=True)
class MainCfg:
    chat_id: int | None = None
    title: str = ""
    username: str | None = None
    invite: str | None = None
    gate_children: bool = True
    gate_creators: bool = True

    @property
    def active(self) -> bool:
        return self.chat_id is not None

    @property
    def url(self) -> str | None:
        if self.username:
            return f"https://t.me/{self.username}"
        return self.invite or None

    def label(self) -> str:
        who = f"@{self.username}" if self.username else str(self.chat_id)
        return f"«{esc(self.title)}» ({who})"


class MainChannel:
    def __init__(self, app: App) -> None:
        self.app = app
        self._cfg: MainCfg | None = None
        self._seen: dict[int, tuple[float, bool | None]] = {}

    # ------------------------------------------------------------------ settings
    async def config(self) -> MainCfg:
        if self._cfg is None:
            async with self.app.db.session() as s:
                rows = {r.key: r.value for r in (await s.scalars(select(PlatformSetting).where(PlatformSetting.key.in_(KEYS)))).all()}
            raw = rows.get("main_chat_id", "")
            try:
                chat_id = int(raw) if raw else None
            except ValueError:
                chat_id = None
            self._cfg = MainCfg(
                chat_id=chat_id,
                title=rows.get("main_title", ""),
                username=rows.get("main_username") or None,
                invite=rows.get("main_invite") or None,
                gate_children=rows.get("main_gate_children", "1") != "0",
                gate_creators=rows.get("main_gate_creators", "1") != "0",
            )
        return self._cfg

    async def _write(self, values: dict[str, str]) -> None:
        async with self.app.db.session() as s:
            for key, value in values.items():
                row = await s.get(PlatformSetting, key)
                if row is None:
                    s.add(PlatformSetting(key=key, value=value))
                else:
                    row.value = value
            await s.commit()
        self._cfg = None
        self._seen.clear()

    async def set_channel(self, info: ChannelInfo) -> None:
        await self._write(
            {
                "main_chat_id": str(info.chat_id),
                "main_title": info.title,
                "main_username": info.username or "",
                "main_invite": info.invite_link or "",
            }
        )

    async def clear(self) -> None:
        await self._write({"main_chat_id": "", "main_title": "", "main_username": "", "main_invite": ""})

    async def set_gate(self, scope: str, enabled: bool) -> None:
        if scope not in ("children", "creators"):
            raise KeyError(scope)
        await self._write({f"main_gate_{scope}": "1" if enabled else "0"})

    # ------------------------------------------------------------------ membership
    async def blocked(self, user_id: int, scope: str, *, force: bool = False) -> MainCfg | None:
        """The config if this user must join the channel before going on, else None."""
        cfg = await self.config()
        if not cfg.active or not (cfg.gate_children if scope == "children" else cfg.gate_creators):
            return None
        if user_id in self.app.settings.admin_ids:
            return None
        ok = await self._member(cfg, user_id, force=force)
        return cfg if ok is False else None

    async def _member(self, cfg: MainCfg, user_id: int, *, force: bool = False) -> bool | None:
        now = time.monotonic()
        hit = self._seen.get(user_id)
        if hit is not None and not force and hit[0] > now:
            return hit[1]
        bot = self.app.mother_bot
        if bot is None or cfg.chat_id is None:
            return None
        ok = await is_member(bot, cfg.chat_id, user_id)
        ttl = TTL_JOINED if ok else TTL_LEFT if ok is False else TTL_UNKNOWN
        if len(self._seen) >= MAX_CACHE:
            self._seen = {k: v for k, v in self._seen.items() if v[0] > now}
            if len(self._seen) >= MAX_CACHE:
                self._seen.clear()
        self._seen[user_id] = (now + ttl, ok)
        return ok

    async def mother_is_admin(self) -> bool | None:
        """Is the mother bot an administrator of the channel (None = cannot tell)."""
        cfg = await self.config()
        bot = self.app.mother_bot
        if not cfg.active or bot is None or cfg.chat_id is None:
            return None
        from aiogram.exceptions import TelegramAPIError

        try:
            me = await bot.get_chat_member(cfg.chat_id, bot.id)
        except TelegramAPIError:
            return False
        return me.status in ("administrator", "creator")

    # ------------------------------------------------------------------ messages
    @staticmethod
    def prompt_text(cfg: MainCfg, *, bot_kind: str) -> str:
        return (
            f"📢 برای استفاده از {bot_kind} ابتدا باید در کانال اصلی {cfg.label()} عضو شوید.\n\n"
            "بعد از عضویت، دکمهٔ «✅ عضو شدم» را بزنید."
        )

    @staticmethod
    def prompt_markup(cfg: MainCfg, payload: str = "") -> InlineKeyboardMarkup:
        rows = []
        if cfg.url:
            rows.append([url_btn("📢 عضویت در کانال", cfg.url)])
        rows.append([btn("✅ عضو شدم", "mchk", 0, payload if PAYLOAD_RE.match(payload or "") else "")])
        return kb(*rows)
