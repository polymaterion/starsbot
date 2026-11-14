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
from app.services.notify import safe_send
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
               f"➕ <b>Пополнение баланса</b>\n\n✏️ Введите сумму пополнения в TMT (от {cfg.topup_min_tmt} до {cfg.topup_max_tmt}):",
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
                   f"➕ <b>Пополнение баланса</b>\n\n⚠️ Введите сумму от {cfg.topup_min_tmt} до {cfg.topup_max_tmt} TMT.\n\n"
                   "✏️ Введите сумму пополнения в TMT:", K.back("balance"))
        return
    cents = int((amount * 100).to_integral_value())
    async with db.Session() as s:
        t = await create_topup(s, user_id=m.from_user.id, amount_cents=cents)
    await draw(m.bot, state, m.chat.id, "payment",
               T.requisites(f"Пополнение баланса #{t.id}", t.total_cents, t.requisite, cfg.payment_ttl_minutes)
               + "\n\nНа баланс зачислится вся переведённая сумма.",
               K.cancel_payment("t", t.id))
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
        await cb.answer("Отменено")
        await show_main(cb.bot, cb.message.chat.id, Ref.of(cb.message), "🚫 Платёж отменён. Можно оформить новый.")
    else:
        await cb.answer("Платёж уже нельзя отменить (он на проверке или завершён).", show_alert=True)


# ── приём скриншота ──────────────────────────────────────────────────
@router.message(priv, F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def on_proof(m: Message):
    cfg = get_settings()
    uid = m.from_user.id
    async with db.Session() as s:
        item = await open_item(s, uid)
        if not item:
            await m.answer("У вас нет платежа, ожидающего оплаты. Оформите заказ в меню: /start")
            return
        kind, obj = item
        if obj.status == "proof_sent":
            await m.answer("🔎 Ваш платёж уже на проверке. Пожалуйста, подождите.")
            return
        file_id = m.photo[-1].file_id if m.photo else m.document.file_id
        buf = await m.bot.download(file_id)
        digest = sha256(buf.getvalue()).hexdigest()
        if await s.get(ProofHash, digest):
            await m.answer("⚠️ Этот скриншот уже использовался. Отправьте актуальный чек вашего перевода.")
            return
        s.add(ProofHash(sha256=digest, user_id=uid))
        obj.proof_attempts += 1
        obj.status = "proof_sent"
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()
            await m.answer("⚠️ Этот скриншот уже использовался.")
            return

        user = await s.get(User, uid)
        label = f"заказу #{obj.id}" if kind == "order" else f"пополнению #{obj.id}"
        if kind == "order":
            what = (f"⭐ {obj.stars_count}" if obj.kind == "stars" else f"🎁 Premium {obj.months} мес.") + f" → @{T.esc(obj.recipient)}"
        else:
            what = "👛 Пополнение баланса"
        caption = (f"💳 <b>Платёж по {label}</b>\n"
                   f"Клиент: {'@' + T.esc(user.username) if user.username else '—'} (<code>{uid}</code>)\n"
                   f"{what}\n"
                   f"Сумма: <b>{T.money(obj.total_cents)} TMT</b>\n"
                   f"Номер: <code>{T.esc(obj.requisite)}</code>\n"
                   f"Попытка {obj.proof_attempts}/{cfg.max_proof_attempts}")
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
            await m.answer("⚠️ Не удалось передать скриншот на проверку. Попробуйте ещё раз чуть позже.")
            return
        obj.admin_msg_id = sent.message_id
        await s.commit()
    await m.answer("✅ Скриншот получен и отправлен на проверку. Мы сообщим, как только платёж подтвердят.")


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
        await cb.answer("Нет доступа", show_alert=True)
        return
    _, action, kind, sid = cb.data.split(":")
    sid_i = int(sid)
    model = Order if kind == "o" else Topup
    now = utcnow()
    async with db.Session() as s:
        obj = await s.get(model, sid_i)
        if obj is None or obj.status != "proof_sent":
            await cb.answer("Уже обработано", show_alert=True)
            await _finish_card(cb, "ℹ️ Уже обработано")
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
                await cb.answer("Уже обработано", show_alert=True)
                return
            if kind == "t":
                await credit_balance(s, uid, obj.total_cents)
            await s.commit()
            user = await s.get(User, uid)
            if kind == "o":
                await safe_send(cb.bot, uid, f"✅ <b>Оплата заказа #{sid_i} подтверждена!</b>\nВыдаём товар — сообщим о результате.")
            else:
                await safe_send(cb.bot, uid, f"✅ <b>Баланс пополнен на {T.money(obj.total_cents)} TMT.</b>\nТекущий баланс: {T.money(user.balance_cents)} TMT.")
            await cb.answer("Подтверждено")
            await _finish_card(cb, "✅ Подтверждено")
        else:
            final = obj.proof_attempts >= cfg.max_proof_attempts
            if final:
                values = {"status": "rejected"}
            else:
                values = {"status": "awaiting_payment", "expires_at": now + timedelta(minutes=cfg.payment_ttl_minutes)}
            res = await s.execute(update(model).where(model.id == sid_i, model.status == "proof_sent").values(**values))
            if res.rowcount != 1:
                await s.rollback()
                await cb.answer("Уже обработано", show_alert=True)
                return
            await s.commit()
            left = cfg.max_proof_attempts - obj.proof_attempts
            if final:
                await safe_send(cb.bot, uid, f"❌ Платёж #{sid_i} отклонён, попытки закончились. Если вы действительно оплатили — напишите в поддержку.")
            else:
                await safe_send(cb.bot, uid, f"❌ Платёж #{sid_i} не подтверждён: проверьте сумму и номер, затем отправьте скриншот перевода ещё раз. Осталось попыток: {left}.")
            await cb.answer("Отклонено")
            await _finish_card(cb, "❌ Отклонено")
