"""Lightweight latency metrics (in memory, no DB): Telegram API calls and update handling.

Purpose: tell *where* a slow bot loses its time — in the network path to Telegram
(proxy / upstream server) or in our own code — without guessing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetUpdates, TelegramMethod

log = logging.getLogger(__name__)

WINDOW = 3600.0
SLOW_UPDATE = 3.0  # seconds; slower updates are logged


@dataclass
class Summary:
    n: int = 0
    avg: float = 0.0
    p50: float = 0.0
    p95: float = 0.0
    worst: float = 0.0
    errors: int = 0


def _pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


class Metrics:
    def __init__(self) -> None:
        self._api: deque[tuple[float, float, bool, str]] = deque(maxlen=2000)
        self._upd: deque[tuple[float, float]] = deque(maxlen=2000)
        self.started = time.time()

    def api(self, method: str, seconds: float, ok: bool) -> None:
        self._api.append((time.time(), seconds, ok, method))

    def update(self, seconds: float) -> None:
        self._upd.append((time.time(), seconds))

    @staticmethod
    def _sum(vals: list[float], errors: int = 0) -> Summary:
        if not vals:
            return Summary(errors=errors)
        s = sorted(vals)
        return Summary(len(s), sum(s) / len(s), _pct(s, 0.5), _pct(s, 0.95), s[-1], errors)

    def api_summary(self, window: float = WINDOW) -> Summary:
        """Telegram calls the bots *make* (long-poll getUpdates excluded: it waits by design)."""
        cutoff = time.time() - window
        rows = [r for r in self._api if r[0] >= cutoff and r[3] != "GetUpdates"]
        return self._sum([r[1] for r in rows if r[2]], errors=sum(1 for r in rows if not r[2]))

    def poll_errors(self, window: float = WINDOW) -> int:
        cutoff = time.time() - window
        return sum(1 for r in self._api if r[0] >= cutoff and r[3] == "GetUpdates" and not r[2])

    def update_summary(self, window: float = WINDOW) -> Summary:
        cutoff = time.time() - window
        return self._sum([r[1] for r in self._upd if r[0] >= cutoff])


class ApiTimer(BaseRequestMiddleware):
    def __init__(self, metrics: Metrics) -> None:
        self.metrics = metrics

    async def __call__(self, make_request, bot: Bot, method: TelegramMethod[Any]):
        t = time.perf_counter()
        ok = True
        try:
            return await make_request(bot, method)
        except BaseException:
            ok = False
            raise
        finally:
            name = type(method).__name__
            dt = time.perf_counter() - t
            # a long poll that simply returned nothing is not "slow"; only count it when it failed
            if not (isinstance(method, GetUpdates) and ok):
                self.metrics.api(name, dt, ok)


class UpdateTimer(BaseMiddleware):
    """Outermost middleware of a dispatcher: total time spent handling one update."""

    def __init__(self, metrics: Metrics, who: str) -> None:
        self.metrics = metrics
        self.who = who

    async def __call__(self, handler, event, data):
        t = time.perf_counter()
        try:
            return await handler(event, data)
        finally:
            dt = time.perf_counter() - t
            self.metrics.update(dt)
            if dt > SLOW_UPDATE:
                log.warning("slow update (%s): %.1fs update_id=%s", self.who, dt, getattr(event, "update_id", "?"))


def system_snapshot() -> dict[str, float | None]:
    """Load average and memory of the host (Linux); missing values are None."""
    out: dict[str, float | None] = {"load1": None, "cpus": float(os.cpu_count() or 1), "mem_total_mb": None, "mem_avail_mb": None}
    try:
        out["load1"] = os.getloadavg()[0]
    except (OSError, AttributeError):
        pass
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            info = {ln.split(":")[0]: int(ln.split()[1]) for ln in fh if ":" in ln}
        out["mem_total_mb"] = info.get("MemTotal", 0) / 1024
        out["mem_avail_mb"] = info.get("MemAvailable", 0) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return out


# --------------------------------------------------------------------------- network resilience
RETRIES = 2
RETRY_PAUSE = 0.3
# sending a message that timed out may already have been delivered → never repeat those on timeout
_SENDS = ("Send", "Copy", "Forward")


class NetRetry(BaseRequestMiddleware):
    """Retry a Telegram call whose connection died (typical for stale keep-alive sockets behind a proxy).

    A dead connection is reported at once ("Server disconnected"); retrying on a fresh socket
    fixes it in a fraction of a second instead of failing the user's click. A *timeout* is only
    retried for calls that are safe to repeat (edit / answer / lookups), not for sends.
    """

    async def __call__(self, make_request, bot: Bot, method: TelegramMethod[Any]):
        name = type(method).__name__
        if isinstance(method, GetUpdates):  # the polling loop has its own back-off
            return await make_request(bot, method)
        attempt = 0
        while True:
            try:
                return await make_request(bot, method)
            except TelegramNetworkError as exc:
                timed_out = "timeout" in str(exc).lower()
                if attempt >= RETRIES or (timed_out and name.startswith(_SENDS)):
                    raise
                attempt += 1
                log.info("retrying %s after network error (%s), attempt %d", name, exc, attempt)
                await asyncio.sleep(RETRY_PAUSE)
