from __future__ import annotations

import asyncio
import random
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import Order, Topup, User, utcnow
from app.services.pricing import Quote

OPEN_STATUSES = ("awaiting_payment", "proof_sent")
_alloc_lock = asyncio.Lock()


class InsufficientBalance(Exception):
    pass


async def open_item(s: AsyncSession, user_id: int) -> tuple[str, Order | Topup] | None:
    """Единственный неоплаченный платёж клиента (заказ или пополнение)."""
    o = await s.scalar(
        select(Order).where(Order.user_id == user_id, Order.status.in_(OPEN_STATUSES)).order_by(Order.id.desc())
    )
    if o:
        return "order", o
    t = await s.scalar(
        select(Topup).where(Topup.user_id == user_id, Topup.status.in_(OPEN_STATUSES)).order_by(Topup.id.desc())
    )
    if t:
        return "topup", t
    return None


async def _pick_total(s: AsyncSession, base_cents: int) -> tuple[int, int]:
    """Уникальная сумма среди открытых платежей: base + «хвост» 1..49, при нехватке — следующие диапазоны."""
    taken = set(
        (await s.scalars(select(Order.total_cents).where(Order.status.in_(OPEN_STATUSES), Order.pay_method == "transfer"))).all()
    ) | set((await s.scalars(select(Topup.total_cents).where(Topup.status.in_(OPEN_STATUSES)))).all())
    for lo in range(1, 500, 50):
        cand = list(range(lo, min(lo + 50, 500)))
        random.shuffle(cand)
        for tail in cand:
            if base_cents + tail not in taken:
                return tail, base_cents + tail
    raise RuntimeError("no free payment amount")


def _requisite(entity_id: int) -> str | None:
    nums = get_settings().pay_number_list
    return nums[entity_id % len(nums)] if nums else None


async def create_order(
    s: AsyncSession,
    *,
    user_id: int,
    kind: str,
    recipient: str,
    stars: int | None,
    months: int | None,
    quote: Quote,
    method: str,
) -> Order:
    cfg = get_settings()
    now = utcnow()
    o = Order(
        user_id=user_id,
        kind=kind,
        recipient=recipient,
        stars_count=stars,
        months=months,
        hex_product="stars" if kind == "stars" else f"premium_{months}m",
        pay_method=method,
        base_cents=quote.base_cents,
        tail_cents=0,
        total_cents=quote.base_cents,
        quoted_nano=quote.nano,
        rate=str(quote.rate),
        markup=str(quote.markup),
        hex_custom_id=str(uuid.uuid4()),
        created_at=now,
    )
    if method == "balance":
        res = await s.execute(
            update(User)
            .where(User.id == user_id, User.balance_cents >= quote.base_cents)
            .values(balance_cents=User.balance_cents - quote.base_cents)
        )
        if res.rowcount != 1:
            await s.rollback()
            raise InsufficientBalance
        o.status = "approved"
        o.approved_at = now
        s.add(o)
        await s.commit()
        return o

    o.status = "awaiting_payment"
    o.expires_at = now + timedelta(minutes=cfg.payment_ttl_minutes)
    async with _alloc_lock:
        tail, total = await _pick_total(s, quote.base_cents)
        o.tail_cents, o.total_cents = tail, total
        s.add(o)
        await s.flush()
        o.requisite = _requisite(o.id)
        await s.commit()
    return o


async def create_topup(s: AsyncSession, *, user_id: int, amount_cents: int) -> Topup:
    cfg = get_settings()
    now = utcnow()
    t = Topup(user_id=user_id, amount_cents=amount_cents, status="awaiting_payment", created_at=now,
              expires_at=now + timedelta(minutes=cfg.payment_ttl_minutes), tail_cents=0, total_cents=amount_cents)
    async with _alloc_lock:
        tail, total = await _pick_total(s, amount_cents)
        t.tail_cents, t.total_cents = tail, total
        s.add(t)
        await s.flush()
        t.requisite = _requisite(t.id)
        await s.commit()
    return t


async def credit_balance(s: AsyncSession, user_id: int, cents: int) -> None:
    await s.execute(update(User).where(User.id == user_id).values(balance_cents=User.balance_cents + cents))


async def refund_order(s: AsyncSession, order: Order, code: str, message: str = "") -> bool:
    """Идемпотентный возврат клиенту на баланс бота + перевод заказа в failed."""
    res = await s.execute(
        update(Order)
        .where(Order.id == order.id, Order.refunded.is_(False))
        .values(refunded=True, status="failed", error_code=code, last_error=message[:500] or None, done_at=utcnow())
    )
    if res.rowcount != 1:
        return False
    await credit_balance(s, order.user_id, order.total_cents)
    order.refunded, order.status, order.error_code = True, "failed", code
    return True
