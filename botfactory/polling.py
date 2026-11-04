"""Long-polling loop usable for any number of bots on one shared Dispatcher.

aiogram's ``Dispatcher.start_polling`` can only run once per dispatcher, but we need to
add/remove child bots at runtime — so we run our own tiny getUpdates loop and hand each
update to ``dp.feed_update``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramConflictError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import Update

log = logging.getLogger(__name__)


async def _feed(dp: Dispatcher, bot: Bot, update: Update) -> None:
    try:
        await dp.feed_update(bot, update)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("unhandled error while processing update %s of bot %s", update.update_id, bot.id)


async def poll_forever(
    bot: Bot,
    dp: Dispatcher,
    *,
    on_unauthorized: Callable[[], Awaitable[None]] | None = None,
    poll_timeout: int = 30,
) -> None:
    allowed = dp.resolve_used_update_types()
    offset: int | None = None
    backoff = 1.0
    tasks: set[asyncio.Task] = set()

    try:
        # a leftover webhook would make getUpdates fail with a conflict
        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except TelegramUnauthorizedError:
            if on_unauthorized:
                await on_unauthorized()
            return
        except (TimeoutError, TelegramAPIError, aiohttp.ClientError) as exc:
            log.warning("delete_webhook failed for bot %s: %s", bot.id, exc)

        while True:
            try:
                updates = await bot.get_updates(
                    offset=offset,
                    timeout=poll_timeout,
                    allowed_updates=allowed,
                    request_timeout=poll_timeout + 15,
                )
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except TelegramUnauthorizedError:
                log.warning("bot %s: token rejected, stopping poller", bot.id)
                if on_unauthorized:
                    await on_unauthorized()
                return
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 0.5)
                continue
            except TelegramConflictError:
                log.warning("bot %s: polling conflict (webhook or another instance); retrying", bot.id)
                try:
                    await bot.delete_webhook(drop_pending_updates=False)
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(5)
                continue
            except (TimeoutError, TelegramNetworkError, aiohttp.ClientError) as exc:
                log.warning("bot %s: network error (%s); retry in %.0fs", bot.id, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            except Exception:
                log.exception("bot %s: unexpected polling error", bot.id)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue

            for update in updates:
                offset = update.update_id + 1
                task = asyncio.create_task(_feed(dp, bot, update))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
    finally:
        for task in tasks:
            task.cancel()
