"""Entry point: ``python -m botfactory``."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from pydantic import ValidationError

from .app import App
from .config import Settings


async def _run(settings: Settings) -> None:
    app = App(settings)
    await app.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            signal.signal(sig, lambda *_: stop.set())
    logging.getLogger(__name__).info("BotFactory is running. Press Ctrl+C to stop.")
    try:
        await stop.wait()
    finally:
        await app.stop()


def main() -> None:
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        missing = ", ".join(str(e["loc"][0]).upper() for e in exc.errors())
        sys.exit(f"Invalid configuration ({missing}). Copy .env.example to .env and fill it in.")
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(_run(settings))


if __name__ == "__main__":
    main()
