import os

os.environ.update({
    "BOT_TOKEN": "1:test", "DATABASE_URL": "sqlite+aiosqlite:///:memory:", "HEX_API_KEY": "k",
    "ADMIN_GROUP_ID": "-100", "ADMIN_IDS": "1,2", "PAY_NUMBERS": "99361000001,99361000002",
    "POLL_MIN_INTERVAL_SEC": "0", "SUPPORT_URL": "@test_support",
})

import pytest_asyncio

from app import db, deps
from app.db import User


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((chat_id, text))

        class M:  # noqa
            message_id = len(self.sent)
        return M()

    def texts_for(self, chat_id):
        return [t for c, t in self.sent if c == chat_id]


class FakeHex:
    """Сценарный двойник Hex API."""

    def __init__(self):
        self.balance_nano = 10_000_000
        self.total_nano = 100_000
        self.orders: dict[str, dict] = {}
        self.create_error = None
        self.pay_error = None
        self.calls: list[tuple] = []
        self.next_status = "delivered"
        self.next_error_code = None
        self.next_delivery_state = None

    async def create_order(self, custom_id, product, recipient, quantity=None):
        from app.services.hex_client import HexError
        self.calls.append(("create", custom_id))
        if self.create_error:
            raise self.create_error
        self.orders.setdefault(custom_id, {"status": "created", "total": self.total_nano})
        return {"status": "created", "total_to_pay_nano": self.total_nano, "expires_at": "2099-01-01T00:00:00Z"}

    async def pay_order(self, custom_id):
        self.calls.append(("pay", custom_id))
        if self.pay_error:
            raise self.pay_error
        self.orders[custom_id]["status"] = "pending"
        return {"status": "pending"}

    async def get_order(self, custom_id):
        self.calls.append(("get", custom_id))
        return {"status": self.next_status, "error_code": self.next_error_code,
                "delivery_state": self.next_delivery_state}

    async def balance(self):
        return {"balance_nano": self.balance_nano}


@pytest_asyncio.fixture
async def env():
    url = os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    await db.init_db(url)
    if not url.startswith("sqlite"):  # чистая схема для каждого теста
        async with db.engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.drop_all)
            await conn.run_sync(db.Base.metadata.create_all)
    deps.hexc = FakeHex()
    async with db.Session() as s:
        s.add(User(id=100, username="client"))
        await s.commit()
    yield deps.hexc, FakeBot()
    await db.engine.dispose()


# ── расширения FakeHex для сквозного теста обработчиков ──
async def _check_recipient(self, product, username):
    return username.lower() != "nobody"


async def _products(self):
    return [
        {"code": "stars", "unit_wholesale_price_rub": 1.3207},
        {"code": "premium_3m", "wholesale_price_rub": 1042.09},
        {"code": "premium_6m", "wholesale_price_rub": 1390},
        {"code": "premium_12m", "wholesale_price_rub": 2520.52},
    ]


FakeHex.check_recipient = _check_recipient
FakeHex.products = _products
