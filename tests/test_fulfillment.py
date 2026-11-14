import pytest
from sqlalchemy import select

from app import db
from app.db import Order, User, utcnow
from app.services import fulfillment
from app.services.hex_client import HexError
from app.services.orders import (InsufficientBalance, create_order, create_topup, open_item, refund_order)
from app.services.pricing import Quote
from decimal import Decimal


def quote(nano=100_000, cents=1000):
    return Quote(rub=Decimal(nano) / 1000, nano=nano, base_cents=cents, rate=Decimal("0.045"), markup=Decimal("10"))


async def approved_order(**kw) -> int:
    async with db.Session() as s:
        o = await create_order(s, user_id=100, kind="stars", recipient="durov", stars=100, months=None,
                               quote=kw.get("q") or quote(), method="transfer")
        o.status, o.approved_at = "approved", utcnow()
        await s.commit()
        return o.id


async def get(oid):
    async with db.Session() as s:
        return await s.get(Order, oid)


async def balance_of(uid=100):
    async with db.Session() as s:
        return (await s.get(User, uid)).balance_cents


async def test_happy_path_delivers_and_notifies(env):
    hx, bot = env
    oid = await approved_order()
    await fulfillment.process_order(bot, oid)          # create + pay
    o = await get(oid)
    assert o.status == "delivering" and o.hex_created
    assert [c[0] for c in hx.calls] == ["create", "pay"]
    await fulfillment.process_order(bot, oid)          # poll -> delivered
    o = await get(oid)
    assert o.status == "done"
    assert any("выполнен" in t for t in bot.texts_for(100))


async def test_insufficient_deposit_waits_then_continues(env):
    hx, bot = env
    hx.balance_nano = 10
    oid = await approved_order()
    await fulfillment.process_order(bot, oid)
    o = await get(oid)
    assert o.status == "hex_waiting"
    assert ("pay", o.hex_custom_id) not in hx.calls        # pay не дёргали вслепую
    assert bot.texts_for(1) and bot.texts_for(2)            # админы предупреждены
    hx.balance_nano = 10_000_000
    await fulfillment.process_order(bot, oid)
    assert (await get(oid)).status == "delivering"


async def test_pay_402_rotates_custom_id(env):
    hx, bot = env
    oid = await approved_order()
    first = (await get(oid)).hex_custom_id
    hx.pay_error = HexError(402, "insufficient_funds", "no money")
    await fulfillment.process_order(bot, oid)
    o = await get(oid)
    assert o.status == "hex_waiting" and o.hex_custom_id != first and not o.hex_created
    hx.pay_error = None
    await fulfillment.process_order(bot, oid)
    assert (await get(oid)).status == "delivering"


async def test_price_guard_holds_order_for_admin(env):
    hx, bot = env
    hx.total_nano = 200_000                                 # вдвое дороже оценки
    oid = await approved_order()
    await fulfillment.process_order(bot, oid)
    o = await get(oid)
    assert o.status == "price_review"
    assert not any(c[0] == "pay" for c in hx.calls)
    assert any("Цена Hex выросла" in t for _, t in bot.sent)


async def test_price_within_threshold_passes(env):
    hx, bot = env
    hx.total_nano = 102_000                                 # +2% при пороге 3%
    oid = await approved_order()
    await fulfillment.process_order(bot, oid)
    assert (await get(oid)).status == "delivering"


async def test_out_of_stock_refunds_to_bot_balance(env):
    hx, bot = env
    hx.create_error = HexError(409, "out_of_stock", "no stock")
    oid = await approved_order()
    total = (await get(oid)).total_cents
    await fulfillment.process_order(bot, oid)
    o = await get(oid)
    assert o.status == "failed" and o.refunded and o.error_code == "out_of_stock"
    assert await balance_of() == total
    assert any("возвращены на баланс" in t for t in bot.texts_for(100))


async def test_failed_delivery_refunds_once(env):
    hx, bot = env
    oid = await approved_order()
    await fulfillment.process_order(bot, oid)
    hx.next_status, hx.next_error_code = "failed", "delivery_failed"
    total = (await get(oid)).total_cents
    await fulfillment.process_order(bot, oid)
    assert (await get(oid)).status == "failed"
    async with db.Session() as s:                            # повторный возврат невозможен
        assert not await refund_order(s, await s.get(Order, oid), "x")
    assert await balance_of() == total


async def test_delivery_in_review_is_not_a_failure(env):
    hx, bot = env
    oid = await approved_order()
    await fulfillment.process_order(bot, oid)
    hx.next_status, hx.next_error_code = "processing", "delivery_in_review"
    await fulfillment.process_order(bot, oid)
    o = await get(oid)
    assert o.status == "delivering" and not o.refunded
    assert any("проверяется" in t for t in bot.texts_for(100))


async def test_wait_timeout_refunds(env):
    from datetime import timedelta
    hx, bot = env
    hx.balance_nano = 0
    oid = await approved_order()
    await fulfillment.process_order(bot, oid)
    async with db.Session() as s:
        o = await s.get(Order, oid)
        o.approved_at = utcnow() - timedelta(hours=7)
        await s.commit()
    await fulfillment.process_order(bot, oid)
    assert (await get(oid)).status == "failed"


async def test_unique_totals_and_balance_payment(env):
    async with db.Session() as s:
        totals = set()
        for uid in range(200, 230):
            s.add(User(id=uid))
        await s.commit()
        for uid in range(200, 230):
            o = await create_order(s, user_id=uid, kind="stars", recipient="x", stars=50, months=None,
                                   quote=quote(), method="transfer")
            totals.add(o.total_cents)
        assert len(totals) == 30                              # все суммы уникальны
        assert all(1000 < t <= 1050 for t in totals)
        # оплата с баланса: недостаточно -> ошибка, достаточно -> approved и списание
        with pytest.raises(InsufficientBalance):
            await create_order(s, user_id=100, kind="stars", recipient="x", stars=50, months=None,
                               quote=quote(), method="balance")
    async with db.Session() as s:
        u = await s.get(User, 100)
        u.balance_cents = 1500
        await s.commit()
        o = await create_order(s, user_id=100, kind="stars", recipient="x", stars=50, months=None,
                               quote=quote(), method="balance")
        assert o.status == "approved" and o.total_cents == 1000
    assert await balance_of() == 500


async def test_one_open_item_and_topup_tail(env):
    async with db.Session() as s:
        assert await open_item(s, 100) is None
        t = await create_topup(s, user_id=100, amount_cents=5000)
        assert 5000 < t.total_cents <= 5049
        kind, obj = await open_item(s, 100)
        assert kind == "topup" and obj.id == t.id


async def test_expire_unpaid_cancels_and_notifies(env):
    from datetime import timedelta
    hx, bot = env
    async with db.Session() as s:
        o = await create_order(s, user_id=100, kind="stars", recipient="x", stars=50, months=None,
                               quote=quote(), method="transfer")
        o.expires_at = utcnow() - timedelta(minutes=1)
        await s.commit()
        oid = o.id
    await fulfillment.expire_unpaid(bot)
    assert (await get(oid)).status == "cancelled"
    assert any("время на оплату истекло" in t for t in bot.texts_for(100))


async def test_delivery_state_in_review_is_not_a_failure(env):
    hx, bot = env
    oid = await approved_order()
    await fulfillment.process_order(bot, oid)
    hx.next_status, hx.next_delivery_state = "processing", "in_review"
    await fulfillment.process_order(bot, oid)
    o = await get(oid)
    assert o.status == "delivering" and not o.refunded
    assert any("проверяется" in t for t in bot.texts_for(100))
