from decimal import Decimal

from app import deps
from app.services.pricing import Catalog, PriceUnavailable, make_quote, rub_to_nano, tmt_cents
from app import db
import pytest


def test_tmt_cents_rounds_up():
    # 100 ₽ * 0.045 * 1.10 = 4.95 TMT = 495 коп.
    assert tmt_cents(Decimal("100"), Decimal("0.045"), Decimal("10")) == 495
    # дробные копейки округляются вверх
    assert tmt_cents(Decimal("1.3207"), Decimal("0.045"), Decimal("10")) == 7  # 6.54 -> 7
    assert rub_to_nano(Decimal("1.3207")) == 1321


class FakeCatalogHex:
    async def products(self):
        return [
            {"code": "stars", "unit_wholesale_price_rub": 1.3207, "unit_wholesale_price_nano": 1320},
            {"code": "premium_3m", "wholesale_price_rub": 1042.09},
        ]


async def test_quote_stars_and_premium(env):
    deps.catalog = Catalog(FakeCatalogHex())
    async with db.Session() as s:
        q = await make_quote(s, "stars", stars=1000)
        assert q.rub == Decimal("1320.7")
        assert q.nano == 1_320_700
        assert q.base_cents == tmt_cents(Decimal("1320.7"), Decimal("0.045"), Decimal("10"))
        p = await make_quote(s, "premium", months=3)
        assert p.rub == Decimal("1042.09")
        with pytest.raises(PriceUnavailable):
            await make_quote(s, "premium", months=12)
