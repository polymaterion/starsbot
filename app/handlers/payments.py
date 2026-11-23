"""Баланс, пополнение, приём скриншотов, отмена, подтверждение/отклонение админом."""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app import db, keyboards as K, texts as T
from app.config import get_settings
from app.db import Order, ProofHash, Topup, User, utcnow
from app.handlers.flow import show_open_payment
from app.handlers.menu import show_main
from app.i18n import t
from app.services.notify import send_to_user
from app.services.orders import create_topup, credit_balance, open_item
from app.ui import Ref, draw, render, try_delete
from datetime import timedelta

log = logging.getLogger(__name__)
router = Router()


class Top(StatesGroup):
    amount = State()


priv = F.chat.type == "private"


# ── баланс и пополнение ──────────────────────────────────────────────
@router.callback_query(F.data == "balance")
async def cb_balance(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    async with db.Session() as s:
        user = await s.get(User, cb.from_user.id)
    await render(cb.bot, Ref.of(cb.message), cb.message.chat.id, "balance", T.balance(user.balance_cents), K.balance_menu())


@router.callback_query(F.data == "topup")
async def cb_topup(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    ref = Ref.of(cb.message)
    async with db.Session() as s:
        item = await open_item(s, cb.from_user.id)
    if item:
        await show_open_payment(cb.bot, cb.message.chat.id, ref, *item)
        return
    cfg = get_settings()
    await state.update_data(ref=ref.to_dict())
    await state.set_state(Top.amount)
    await draw(cb.bot, state, cb.message.chat.id, "balance",
               t("topup.ask", min=cfg.topup_min_tmt, max=cfg.topup_max_tmt),
               K.back("balance"))


@router.message(Top.amount, F.text, priv)
async def got_topup_amount(m: Message, state: FSMContext):
    await try_delete(m)
    cfg = get_settings()
    try:
        amount = Decimal(m.text.strip().replace(",", "."))
    except InvalidOperation:
        amount = Decimal(-1)
    if not (cfg.topup_min_tmt <= amount <= cfg.topup_max_tmt):
        await draw(m.bot, state, m.chat.id, "balance",
                   t("topup.bad", min=cfg.topup_min_tmt, max=cfg.topup_max_tmt), K.back("balance"))
        return
    cents = int((amount * 100).to_integral_value())
    async with db.Session() as s:
        tp = await create_topup(s, user_id=m.from_user.id, amount_cents=cents)
    await draw(m.bot, state, m.chat.id, "payment",
               T.requisites(t("req.topup_title", id=tp.id), tp.total_cents, tp.requisite, cfg.payment_ttl_minutes)
               + "\n\n" + t("req.topup_note"),
               K.cancel_payment("t", tp.id))
    await state.clear()


# ── отмена клиентом ──────────────────────────────────────────────────
@router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    _, kind, sid = cb.data.split(":")
    model = Order if kind == "o" else Topup
    async with db.Session() as s:
        res = await s.execute(update(model).where(model.id == int(sid), model.user_id == cb.from_user.id,
                                                  model.status == "awaiting_payment").values(status="cancelled"))
        await s.commit()
    await state.clear()
    if res.rowcount:
        await cb.answer(t("alert.cancelled"))
        await show_main(cb.bot, cb.message.chat.id, Ref.of(cb.message), t("cancel.done"))
    else:
        await cb.answer(t("alert.cannot_cancel"), show_alert=True)


# ── приём скриншота ──────────────────────────────────────────────────
@router.message(priv, F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def on_proof(m: Message):
    cfg = get_settings()
    uid = m.from_user.id
    async with db.Session() as s:
        item = await open_item(s, uid)
        if not item:
            await m.answer(t("proof.no_item"))
            return
        kind, obj = item
        if obj.status == "proof_sent":
            await m.answer(t("proof.already_review"))
            return
        file_id = m.photo[-1].file_id if m.photo else m.document.file_id
        buf = await m.bot.download(file_id)
        digest = sha256(buf.getvalue()).hexdigest()
        if await s.get(ProofHash, digest):
            await m.answer(t("proof.duplicate"))
            return
        s.add(ProofHash(sha256=digest, user_id=uid))
        obj.proof_attempts += 1
        obj.status = "proof_sent"
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()
            await m.answer(t("proof.duplicate_short"))
            return

        user = await s.get(User, uid)
        label = t("adm.label.order", id=obj.id) if kind == "order" else t("adm.label.topup", id=obj.id)
        if kind == "order":
            rcp = T.esc(obj.recipient)
            what = (t("adm.what.stars", n=obj.stars_count, recipient=rcp) if obj.kind == "stars"
                    else t("adm.what.premium", n=obj.months, recipient=rcp))
        else:
            what = t("adm.what.topup")
        caption = t("adm.card", label=label, client=("@" + T.esc(user.username)) if user.username else "—", uid=uid,
                    what=what, amount=T.money(obj.total_cents), number=T.esc(obj.requisite),
                    attempt=obj.proof_attempts, max=cfg.max_proof_attempts)
        kb = K.admin_payment("o" if kind == "order" else "t", obj.id)
        try:
            if m.photo:
                sent = await m.bot.send_photo(cfg.admin_group_id, file_id, caption=caption, reply_markup=kb)
            else:
                sent = await m.bot.send_document(cfg.admin_group_id, file_id, caption=caption, reply_markup=kb)
        except Exception:
            log.exception("cannot deliver proof to admin group")
            obj.proof_attempts -= 1
            obj.status = "awaiting_payment"
            await s.delete(await s.get(ProofHash, digest))
            await s.commit()
            await m.answer(t("proof.send_failed"))
            return
        obj.admin_msg_id = sent.message_id
        await s.commit()
    await m.answer(t("proof.received"))


# ── решение админа в группе ──────────────────────────────────────────
async def _finish_card(cb: CallbackQuery, verdict: str) -> None:
    try:
        base = cb.message.caption or ""
        await cb.message.edit_caption(caption=f"{T.esc(base)}\n\n{verdict} — {T.esc(cb.from_user.full_name)}", reply_markup=None)
    except Exception:
        log.info("card edit skipped", exc_info=True)


@router.callback_query(F.data.startswith("adm:"))
async def admin_decision(cb: CallbackQuery):
    cfg = get_settings()
    if cb.from_user.id not in cfg.admin_id_list:
        await cb.answer(t("adm.alert.no_access"), show_alert=True)
        return
    _, action, kind, sid = cb.data.split(":")
    sid_i = int(sid)
    model = Order if kind == "o" else Topup
    now = utcnow()
    async with db.Session() as s:
        obj = await s.get(model, sid_i)
        if obj is None or obj.status != "proof_sent":
            await cb.answer(t("adm.alert.done_already"), show_alert=True)
            await _finish_card(cb, t("adm.verdict.already"))
            return
        uid = obj.user_id
        if action == "ok":
            new_status = "approved" if kind == "o" else "credited"
            values = {"status": new_status}
            if kind == "o":
                values["approved_at"] = now
            else:
                values["credited_at"] = now
            res = await s.execute(update(model).where(model.id == sid_i, model.status == "proof_sent").values(**values))
            if res.rowcount != 1:
                await s.rollback()
                await cb.answer(t("adm.alert.done_already"), show_alert=True)
                return
            if kind == "t":
                await credit_balance(s, uid, obj.total_cents)
            await s.commit()
            user = await s.get(User, uid)
            if kind == "o":
                await send_to_user(cb.bot, uid, lambda: t("notify.order_approved", id=sid_i))
            else:
                await send_to_user(cb.bot, uid, lambda: t("notify.topup_credited", amount=T.money(obj.total_cents),
                                                                     balance=T.money(user.balance_cents)))
            await cb.answer(t("adm.alert.approved"))
            await _finish_card(cb, t("adm.verdict.approved"))
        else:
            final = obj.proof_attempts >= cfg.max_proof_attempts
            if final:
                values = {"status": "rejected"}
            else:
                values = {"status": "awaiting_payment", "expires_at": now + timedelta(minutes=cfg.payment_ttl_minutes)}
            res = await s.execute(update(model).where(model.id == sid_i, model.status == "proof_sent").values(**values))
            if res.rowcount != 1:
                await s.rollback()
                await cb.answer(t("adm.alert.done_already"), show_alert=True)
                return
            await s.commit()
            left = cfg.max_proof_attempts - obj.proof_attempts
            if final:
                await send_to_user(cb.bot, uid, lambda: t("notify.rejected_final", id=sid_i))
            else:
                await send_to_user(cb.bot, uid, lambda: t("notify.rejected_retry", id=sid_i, left=left))
            await cb.answer(t("adm.alert.rejected"))
            await _finish_card(cb, t("adm.verdict.rejected"))
