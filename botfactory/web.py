"""aiohttp web server: Telegram webhooks, payment callbacks, health check."""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:  # pragma: no cover
    from .app import App

log = logging.getLogger(__name__)

_PAGE = """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{font-family:Tahoma,Vazirmatn,sans-serif;background:#f4f6f8;display:flex;min-height:100vh;
align-items:center;justify-content:center;margin:0}}.c{{background:#fff;padding:32px 28px;border-radius:16px;
box-shadow:0 4px 24px #0001;max-width:380px;text-align:center}}.i{{font-size:56px}}h1{{font-size:20px;margin:12px 0}}
p{{color:#555;line-height:1.9}}a{{display:inline-block;margin-top:16px;padding:10px 22px;background:#229ed9;
color:#fff;border-radius:10px;text-decoration:none}}</style></head><body><div class="c"><div class="i">{icon}</div>
<h1>{title}</h1><p>{message}</p>{link}</div></body></html>"""


def _result_page(ok: bool, message: str, bot_username: str | None) -> web.Response:
    link = f'<a href="https://t.me/{html.escape(bot_username)}">بازگشت به ربات</a>' if bot_username else ""
    body = _PAGE.format(
        title="پرداخت موفق" if ok else "پرداخت ناموفق",
        icon="✅" if ok else "❌",
        message=html.escape(message),
        link=link,
    )
    return web.Response(text=body, content_type="text/html", charset="utf-8", status=200 if ok else 400)


def create_web_app(app: App) -> web.Application:
    routes = web.RouteTableDef()

    @routes.get("/healthz")
    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "bots": len(app.manager.runtimes)})

    @routes.post("/tg/m/{secret}")
    async def mother_hook(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except ValueError:
            return web.Response(status=400)
        status = await app.feed_mother_webhook(
            request.match_info["secret"], request.headers.get("X-Telegram-Bot-Api-Secret-Token"), data
        )
        return web.Response(status=status)

    @routes.post("/tg/c/{bot_id}/{secret}")
    async def child_hook(request: web.Request) -> web.Response:
        try:
            bot_id = int(request.match_info["bot_id"])
            data = await request.json()
        except ValueError:
            return web.Response(status=400)
        status = await app.manager.feed_webhook(
            bot_id, request.match_info["secret"], request.headers.get("X-Telegram-Bot-Api-Secret-Token"), data
        )
        return web.Response(status=status)

    @routes.get("/pay/zarinpal/{payment_id}")
    async def zarinpal_callback(request: web.Request) -> web.Response:
        try:
            payment_id = int(request.match_info["payment_id"])
        except ValueError:
            raise web.HTTPNotFound() from None
        result = await app.payments.handle_zarinpal_callback(
            payment_id, request.query.get("Authority", ""), request.query.get("Status", "")
        )
        return _result_page(result.ok, result.message, result.bot_username)

    @routes.post("/pay/nowpayments/{payment_id}")
    async def nowpayments_ipn(request: web.Request) -> web.Response:
        try:
            payment_id = int(request.match_info["payment_id"])
        except ValueError:
            raise web.HTTPNotFound() from None
        raw = await request.read()
        status, text = await app.payments.handle_nowpayments_ipn(
            payment_id, raw, request.headers.get("x-nowpayments-sig", "")
        )
        return web.Response(status=status, text=text)

    web_app = web.Application(client_max_size=2 * 1024 * 1024)
    web_app.add_routes(routes)
    return web_app
