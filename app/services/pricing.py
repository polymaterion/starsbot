from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import ROUND_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app import deps
from app.config import get_settings
from app.db import Setting


class PriceUnavailable(Exception):
    pass


async def get_setting(s: AsyncSession, key: str, default: str) -> str:
    row = await s.get(Setting, key)
    return row.value if row else default


async def set_setting(s: AsyncSession, key: str, value: str) -> None:
    row = await s.get(Setting, key)
    if row:
        row.value = value
    else:
        s.add(Setting(key=key, value=value))


async def get_rate(s: AsyncSession) -> Decimal:
    return Decimal(await get_setting(s, "rate", str(get_settings().default_rate_tmt_per_rub)))


async def get_markup(s: AsyncSession) -> Decimal:
    return Decimal(await get_setting(s, "markup", str(get_settings().default_markup_percent)))


def tmt_cents(rub: Decimal, rate: Decimal, markup_pct: Decimal) -> int:
    """Цена в тмт-копейках, округление вверх (в пользу магазина)."""
    value = rub * rate * (Decimal(1) + markup_pct / Decimal(100)) * 100
    return int(value.to_integral_value(rounding=ROUND_UP))


def rub_to_nano(rub: Decimal) -> int:
    return int((rub * 1000).to_integral_value(rounding=ROUND_UP))


class Catalog:
    """Кэш /products на 60 секунд (лимит запросов Hex — 60/мин)."""

    def __init__(self, hexc, ttl: float = 60.0):
        self._hex = hexc
        self._ttl = ttl
        self._at = 0.0
        self._by_code: dict[str, dict] = {}

    async def _load(self) -> dict[str, dict]:
        if time.monotonic() - self._at > self._ttl or not self._by_code:
            products = await self._hex.products()
            self._by_code = {p["code"]: p for p in products if "code" in p}
            self._at = time.monotonic()
        return self._by_code

    @staticmethod
    def _price(p: dict, rub_keys: tuple[str, ...], nano_keys: tuple[str, ...]) -> Decimal:
        for k in rub_keys:
            if p.get(k) is not None:
                return Decimal(str(p[k]))
        for k in nano_keys:
            if p.get(k) is not None:
                return Decimal(str(p[k])) / 1000
        raise PriceUnavailable(p.get("code", "?"))

    async def stars_unit_rub(self) -> Decimal:
        p = (await self._load()).get("stars")
        if not p:
            raise PriceUnavailable("stars")
        return self._price(p, ("unit_wholesale_price_rub", "wholesale_price_rub"),
                           ("unit_wholesale_price_nano", "wholesale_price_nano"))

    async def premium_rub(self, months: int) -> Decimal:
        p = (await self._load()).get(f"premium_{months}m")
        if not p:
            raise PriceUnavailable(f"premium_{months}m")
        return self._price(p, ("wholesale_price_rub",), ("wholesale_price_nano",))


@dataclass
class Quote:
    rub: Decimal
    nano: int
    base_cents: int
    rate: Decimal
    markup: Decimal


async def make_quote(s: AsyncSession, kind: str, *, stars: int | None = None, months: int | None = None) -> Quote:
    cat = deps.catalog
    if kind == "stars":
        assert stars
        rub = await cat.stars_unit_rub() * stars
    else:
        assert months
        rub = await cat.premium_rub(months)
    rate, markup = await get_rate(s), await get_markup(s)
    return Quote(rub=rub, nano=rub_to_nano(rub), base_cents=tmt_cents(rub, rate, markup), rate=rate, markup=markup)
