"""Shared callback-data factory (one compact factory for every inline button)."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class A(CallbackData, prefix="a"):
    """act = action name, id = numeric argument, p = free-form argument."""

    act: str
    id: int = 0
    p: str = ""
