"""Асинхронный клиент Hex Reseller API (по брифу hex-reseller-api-brief.md).

- Авторизация: Bearer-ключ.
- Лимит ~60 запросов/мин на ключ: клиент сам разносит запросы (~1.05 c) и
  уважает Retry-After при 429.
- create/pay идемпотентны по custom_id, поэтому повтор при сетевых ошибках и 5xx безопасен.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


class HexError(Exception):
    """Ответ Hex с ошибкой (status=0 — сетевая ошибка)."""

    def __init__(self, status: int, code: str | None = None, message: str = "", payload: Any = None):
        super().__init__(f"HTTP {status} {code or ''} {message}".strip())
        self.status = status
        self.code = code
        self.message = message
        self.payload = payload


class HexClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        min_interval: float = 1.05,
    ):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )
        self._max_retries = max_retries
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._last + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def _request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> Any:
        attempt = 0
        while True:
            attempt += 1
            await self._throttle()
            try:
                r = await self._http.request(method, path, params=params, json=json)
            except httpx.TransportError as e:
                if attempt > self._max_retries:
                    raise HexError(0, "network", str(e)) from e
                await asyncio.sleep(min(2 ** attempt, 10))
                continue

            if r.status_code == 429 and attempt <= self._max_retries:
                try:
                    delay = float(r.headers.get("Retry-After", "2"))
                except ValueError:
                    delay = 2.0
                await asyncio.sleep(min(max(delay, 1.0), 30.0))
                continue
            if r.status_code >= 500 and attempt <= self._max_retries:
                await asyncio.sleep(min(2 ** attempt, 10))
                continue

            try:
                data = r.json()
            except ValueError:
                data = {}
            if r.status_code >= 400:
                code = message = None
                if isinstance(data, dict):
                    code = data.get("error_code") or data.get("code")
                    message = data.get("error") or data.get("message")
                    if isinstance(code, dict):
                        code = None
                raise HexError(r.status_code, code, str(message or r.text[:200]), data)
            return data

    # ── публичные методы ─────────────────────────────────────────────
    async def products(self) -> list[dict]:
        data = await self._request("GET", "/products")
        if isinstance(data, dict):
            return data.get("products") or data.get("items") or []
        return data

    async def check_recipient(self, product: str, username: str) -> bool:
        data = await self._request(
            "GET", "/check-recipient", params={"product": product, "username": username.lstrip("@")}
        )
        return bool(isinstance(data, dict) and data.get("valid"))

    async def create_order(self, custom_id: str, product: str, recipient: str, quantity: int | None = None) -> dict:
        body: dict[str, Any] = {
            "custom_id": custom_id,
            "product": product,
            "recipient_username": recipient.lstrip("@"),
        }
        if quantity is not None:
            body["quantity"] = quantity
        return await self._request("POST", "/order/create", json=body)

    async def pay_order(self, custom_id: str) -> dict:
        return await self._request("POST", "/order/pay", json={"custom_id": custom_id})

    async def get_order(self, custom_id: str) -> dict:
        return await self._request("GET", f"/order/{custom_id}")

    async def balance(self) -> dict:
        return await self._request("GET", "/balance")
