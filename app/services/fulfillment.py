"""Воркер: доводит оплаченные заказы до выдачи через Hex.

Состояния заказа:
  approved / hex_waiting -> create -> проверка цены -> проверка депозита -> pay -> delivering
  delivering -> опрос GET /order/{id} -> done | failed (возврат на баланс бота)
  price_review — ждёт решения админа (callback pr:ok / pr:no)

Безопасность денег:
  * create/pay идемпотентны по custom_id; после каждого шага сразу commit;
  * перед ротацией custom_id (просрочка заказа в Hex) сверяемся с GET /order,
    чтобы не оплатить дважды;
  * возврат клиенту — через refund_order (CAS по флагу refunded).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aiogram import Bot
from sqlalchemy import select, update

from app import db, deps, keyboards as K
from app.config import get_settings
from app.db import Order, Topup, utcnow
from app.services.errors import reason_text
from app.services.hex_client import HexError
from app.services.notify import notify_admins, send_to_user
from app.services.orders import refund_order
from app.i18n import t
from app.texts import esc, money, months_label

log = logging.getLogger(__name__)

MAX_HEX_ERRORS_BEFORE_ALERT = 10


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _item_title(o: Order) -> str:
    if o.kind == "stars":
        return t("adm.item.stars", n=o.stars_count)
    return t("adm.item.premium", period=months_label(o.months))


async def _fail(bot: Bot, s, o: Order, code: str, message: str = "") -> None:
    if await refund_order(s, o, code, message):
        await s.commit()
        await send_to_user(bot, o.user_id, lambda: t("fulfil.failed", id=o.id, reason=reason_text(code),
                                                     amount=money(o.total_cents)))



def _rotate_custom_id(o: Order) -> None:
    o.hex_custom_id = str(uuid.uuid4())
    o.hex_created = False
    o.hex_total_nano = None
    o.hex_expires_at = None
    o.hex_attempts += 1


async def _enter_waiting(bot: Bot, s, o: Order, reason: str) -> None:
    first = o.status != "hex_waiting"
    o.status = "hex_waiting"
    if not o.deposit_alerted:
        o.deposit_alerted = True
        await notify_admins(bot, t("adm.deposit_wait", reason=reason, id=o.id))
    if first:
        await send_to_user(bot, o.user_id, lambda: t("fulfil.accepted", id=o.id))
    await s.commit()


async def _advance(bot: Bot, s, o: Order) -> None:
    cfg = get_settings()
    hexc = deps.hexc
    now = utcnow()

    if o.status == "hex_waiting" and o.approved_at and now - o.approved_at > timedelta(hours=cfg.hex_wait_hours):
        await _fail(bot, s, o, "wait_timeout")
        return

    # 0. заказ в Hex уже создан, но скоро истечёт: сверяемся и при необходимости пересоздаём
    if o.hex_created and o.hex_expires_at and o.hex_expires_at <= now + timedelta(seconds=60):
        try:
            data = await hexc.get_order(o.hex_custom_id)
        except HexError as e:
            if e.status == 404:
                data = {"status": "cancelled"}
            else:
                return
        st = data.get("status")
        if st in ("pending", "processing", "delivered"):
            o.status = "delivering"
            await s.commit()
            return
        _rotate_custom_id(o)
        await s.commit()

    # 1. create
    if not o.hex_created:
        try:
            resp = await hexc.create_order(
                o.hex_custom_id, o.hex_product, o.recipient,
                quantity=o.stars_count if o.kind == "stars" else None,
            )
        except HexError as e:
            await _on_create_error(bot, s, o, e)
            return
        o.hex_created = True
        o.hex_errors = 0
        o.hex_total_nano = int(resp["total_to_pay_nano"])
        o.hex_expires_at = _parse_dt(resp.get("expires_at"))
        await s.commit()

    # 2. защита от убытка: Hex подорожал больше порога относительно проданной цены
    guard = Decimal(1) + cfg.price_guard_percent / Decimal(100)
    ceiling = max(int(Decimal(o.quoted_nano) * guard), o.price_ceiling_nano)
    if o.hex_total_nano and o.hex_total_nano > ceiling:
        o.status = "price_review"
        await s.commit()
        await notify_admins(
            bot,
            t("adm.price_up", id=o.id, item=_item_title(o), recipient=esc(o.recipient),
              quoted=f"{Decimal(o.quoted_nano) / 1000:.2f}", now=f"{Decimal(o.hex_total_nano) / 1000:.2f}",
              paid=money(o.total_cents)),
            K.price_review(o.id),
        )
        return

    # 3. хватает ли депозита (иначе pay вернёт 402, а заказ в Hex станет rejected)
    try:
        bal = await hexc.balance()
    except HexError:
        return
    if int(bal.get("balance_nano", 0)) < (o.hex_total_nano or 0):
        await _enter_waiting(bot, s, o, t("adm.reason.no_deposit"))
        return

    # 4. pay
    try:
        resp = await hexc.pay_order(o.hex_custom_id)
    except HexError as e:
        if e.status == 402:
            _rotate_custom_id(o)  # заказ в Hex стал rejected — следующая попытка с новым custom_id
            await _enter_waiting(bot, s, o, t("adm.reason.no_deposit"))
        elif e.status == 409 and e.code in ("out_of_stock", "product_unavailable", "recipient_invalid"):
            await _fail(bot, s, o, e.code, e.message)
        elif e.status in (401, 403):
            await _alert_once(bot, s, o, t("adm.alert.key"))
        else:
            o.hex_errors += 1
            o.last_error = str(e)[:500]
            await s.commit()
        return
    o.status = "delivering"
    o.last_error = None
    await s.commit()
    log.info("order %s paid at Hex: %s", o.id, resp.get("status"))


async def _alert_once(bot: Bot, s, o: Order, text: str) -> None:
    if not o.deposit_alerted:
        o.deposit_alerted = True
        await notify_admins(bot, t("adm.alert_once", text=text, id=o.id))
    await s.commit()


async def _on_create_error(bot: Bot, s, o: Order, e: HexError) -> None:
    if e.status == 409:
        await _fail(bot, s, o, e.code or "out_of_stock", e.message)
    elif e.status == 400:
        await _fail(bot, s, o, e.code or "invalid_order_data", e.message)
    elif e.status in (401, 403):
        await _alert_once(bot, s, o, t("adm.alert.key"))
    else:
        o.hex_errors += 1
        o.last_error = str(e)[:500]
        if o.hex_errors == MAX_HEX_ERRORS_BEFORE_ALERT:
            await notify_admins(bot, t("adm.hex_down", id=o.id, error=esc(e)))
        await s.commit()


async def _poll(bot: Bot, s, o: Order) -> None:
    cfg = get_settings()
    now = utcnow()
    if o.last_polled_at and (now - o.last_polled_at).total_seconds() < cfg.poll_min_interval_sec:
        return
    o.last_polled_at = now
    try:
        data = await deps.hexc.get_order(o.hex_custom_id)
    except HexError:
        await s.commit()
        return
    st = data.get("status")
    code = data.get("error_code")
    if st == "delivered":
        o.status, o.done_at = "done", now
        await s.commit()
        await send_to_user(bot, o.user_id, lambda: (
            t("fulfil.done_stars", id=o.id, n=o.stars_count, recipient=esc(o.recipient)) if o.kind == "stars"
            else t("fulfil.done_premium", id=o.id, period=months_label(o.months), recipient=esc(o.recipient))))
    elif st in ("failed", "cancelled"):
        await _fail(bot, s, o, code or ("order_cancelled" if st == "cancelled" else "delivery_failed"))
    elif st == "rejected":
        _rotate_custom_id(o)
        await _enter_waiting(bot, s, o, t("adm.reason.hex_rejected"))
    else:
        # pending / processing; delivery_in_review — не провал, ждём
        in_review = code == "delivery_in_review" or data.get("delivery_state") == "in_review"
        if in_review and not o.review_notified:
            o.review_notified = True
            await send_to_user(bot, o.user_id, lambda: t("fulfil.review", id=o.id))
        await s.commit()


async def process_order(bot: Bot, order_id: int) -> None:
    async with db.Session() as s:
        o = await s.get(Order, order_id)
        if o is None:
            return
        if o.status in ("approved", "hex_waiting"):
            await _advance(bot, s, o)
        elif o.status == "delivering":
            await _poll(bot, s, o)


async def expire_unpaid(bot: Bot) -> None:
    now = utcnow()
    notices: list[tuple[int, str, int]] = []
    async with db.Session() as s:
        for model, key in ((Order, "expire.order"), (Topup, "expire.topup")):
            rows = (await s.scalars(select(model).where(model.status == "awaiting_payment", model.expires_at < now))).all()
            for r in rows:
                res = await s.execute(
                    update(model).where(model.id == r.id, model.status == "awaiting_payment").values(status="cancelled"))
                if res.rowcount:
                    notices.append((r.user_id, key, r.id))
        await s.commit()
    for uid, key, rid in notices:
        await send_to_user(bot, uid, lambda key=key, rid=rid: t(key, id=rid))


async def check_deposit(bot: Bot, state: dict) -> None:
    cfg = get_settings()
    try:
        bal = await deps.hexc.balance()
    except HexError:
        return
    nano = int(bal.get("balance_nano", 0))
    low = nano < int(cfg.hex_low_balance_rub * 1000)
    now = datetime.now()
    if low and (not state.get("alerted_at") or (now - state["alerted_at"]).total_seconds() > 6 * 3600):
        state["alerted_at"] = now
        await notify_admins(bot, t("adm.low_deposit", balance=f"{Decimal(nano) / 1000:.2f}", threshold=cfg.hex_low_balance_rub))
    if not low:
        state["alerted_at"] = None


async def run(bot: Bot) -> None:
    cfg = get_settings()
    dep_state: dict = {}
    last_dep = 0.0
    log.info("fulfillment worker started")
    while True:
        try:
            await expire_unpaid(bot)
            async with db.Session() as s:
                ids = (await s.scalars(
                    select(Order.id).where(Order.status.in_(("approved", "hex_waiting", "delivering"))).order_by(Order.id)
                )).all()
            for oid in ids:
                try:
                    await process_order(bot, oid)
                except Exception:
                    log.exception("order %s processing failed", oid)
            loop_time = asyncio.get_event_loop().time()
            if loop_time - last_dep > 600:
                last_dep = loop_time
                await check_deposit(bot, dep_state)
        except Exception:
            log.exception("worker iteration failed")
        await asyncio.sleep(cfg.worker_interval_sec)
