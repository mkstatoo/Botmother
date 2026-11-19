"""Child bot: the "✅ عضو شدم" button of the main-channel gate."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.types import CallbackQuery

from ..callbacks import A
from ..db.models import ChildBot
from ..gate import on_joined_pressed

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

router = Router(name="child_gate")


@router.callback_query(A.filter(F.act == "mchk"))
async def cb_joined(callback: CallbackQuery, app: App, child: ChildBot) -> None:
    await on_joined_pressed(app, callback, "children", child.username)
