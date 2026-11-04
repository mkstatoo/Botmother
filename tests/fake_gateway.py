"""Local stand-in for the Zarinpal and NOWPayments HTTP APIs."""

from __future__ import annotations

import itertools
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer


class FakeGateway:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []  # recorded request bodies (with "_path")
        self.verify_calls: list[dict[str, Any]] = []
        self.invoices: list[dict[str, Any]] = []
        self.reject_request = False
        self.reject_verify = False
        self._auth = itertools.count(1)
        self.server: TestServer | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/pg/v4/payment/request.json", self._request)
        app.router.add_post("/pg/v4/payment/verify.json", self._verify)
        app.router.add_post("/np/invoice", self._invoice)
        self.server = TestServer(app)
        await self.server.start_server()

    async def stop(self) -> None:
        if self.server:
            await self.server.close()

    @property
    def base(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.port}"

    @property
    def zarinpal_base(self) -> str:
        return self.base

    @property
    def nowpayments_base(self) -> str:
        return self.base + "/np"

    async def _request(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append(body)
        if self.reject_request:
            return web.json_response({"data": [], "errors": {"code": -9, "message": "validation error"}})
        authority = f"A{next(self._auth):035d}"
        return web.json_response({"data": {"code": 100, "message": "Success", "authority": authority, "fee": 0}, "errors": []})

    async def _verify(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.verify_calls.append(body)
        if self.reject_verify:
            return web.json_response({"data": [], "errors": {"code": -50, "message": "mismatch"}})
        return web.json_response({"data": {"code": 100, "message": "Verified", "ref_id": 987654}, "errors": []})

    async def _invoice(self, request: web.Request) -> web.Response:
        body = await request.json()
        body["_api_key"] = request.headers.get("x-api-key")
        self.invoices.append(body)
        if not body["_api_key"]:
            return web.json_response({"message": "forbidden"}, status=403)
        n = len(self.invoices)
        return web.json_response({"id": f"inv{n}", "invoice_url": f"https://nowpayments.example/pay/inv{n}"})
