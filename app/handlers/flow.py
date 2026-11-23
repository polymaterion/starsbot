"""Оформление заказа: Stars и Premium (3 шага) + оплата."""
from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app import db, deps, keyboards as K, texts as T
from app.config import get_settings
from app.db import User
from app.handlers.menu import show_main
from app.i18n import t
from app.services.hex_client import HexError
from app.services.orders import InsufficientBalance, create_order, open_item
from app.services.pricing import PriceUnavailable, make_quote
from app.ui import Ref, draw, render, try_delete

router = Router()
router.message.filter(F.chat.type == "private")

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
PERIODS = (3, 6, 12)


class Buy(StatesGroup):
    username = State()
    quantity = State()
    confirm = State()


async def show_open_payment(bot, chat_id: int, ref: Ref | None, kind: str, obj) -> None:
    """У клиента уже есть неоплаченный платёж — показываем его вместо нового."""
    cfg = get_settings()
    if obj.status == "proof_sent":
        await render(bot, ref, chat_id, "payment",
                     t("review.screen", id=obj.id), K.waiting_check())
        return
    title = t("req.pay_title", id=obj.id) if kind == "order" else t("req.topup_title", id=obj.id)
    text = T.requisites(title, obj.total_cents, obj.requisite, cfg.payment_ttl_minutes,
                        prefix=t("req.existing_prefix"))
    await render(bot, ref, chat_id, "payment", text, K.cancel_payment("o" if kind == "order" else "t", obj.id))


async def show_step(bot, state: FSMContext, chat_id: int, n: int, note: str = "") -> None:
    d = await state.get_data()
    kind = d["kind"]
    if n == 1:
        await state.set_state(Buy.username)
        prompt = t("step.username.stars" if kind == "stars" else "step.username.premium")
        await draw(bot, state, chat_id, kind,
                   T.step(kind, 1, prompt=prompt, note=note),
                   K.step1())
    elif n == 2 and kind == "stars":
        cfg = get_settings()
        await state.set_state(Buy.quantity)
        await draw(bot, state, chat_id, kind,
                   T.step(kind, 2, d.get("username"), prompt=t("step.quantity", min=cfg.stars_min, max=cfg.stars_max), note=note),
                   K.step_back())
    elif n == 2:
        await state.set_state(Buy.confirm)
        try:
            async with db.Session() as s:
                prices = {m: (await make_quote(s, "premium", months=m)).base_cents for m in PERIODS}
        except (PriceUnavailable, HexError):
            await draw(bot, state, chat_id, kind,
                       T.step(kind, 2, d.get("username"), note=t("warn.prices_unavailable")), K.step_back())
            return
        await draw(bot, state, chat_id, kind,
                   T.step(kind, 2, d.get("username"), prompt=t("step.period"), note=note),
                   K.premium_periods(prices))
    else:
        await state.set_state(Buy.confirm)
        async with db.Session() as s:
            try:
                q = await make_quote(s, kind, stars=d.get("qty"), months=d.get("months"))
            except (PriceUnavailable, HexError):
                await draw(bot, state, chat_id, kind,
                           T.step(kind, 2, d.get("username"), d.get("qty"), d.get("months"),
                                  note=t("warn.prices_unavailable")), K.step_back())
                return
            user = await s.get(User, d["uid"])
        await draw(bot, state, chat_id, "payment",
                   T.payment_methods(kind, d.get("username"), d.get("qty"), d.get("months"), q.base_cents, note),
                   K.pay_methods(user.balance_cents))


@router.callback_query(F.data.in_({"stars", "premium"}))
async def start_flow(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    ref = Ref.of(cb.message)
    async with db.Session() as s:
        item = await open_item(s, cb.from_user.id)
    if item:
        await show_open_payment(cb.bot, cb.message.chat.id, ref, *item)
        return
    await state.update_data(kind=cb.data, ref=ref.to_dict(), uid=cb.from_user.id)
    await show_step(cb.bot, state, cb.message.chat.id, 1)


async def _accept_username(bot, state: FSMContext, chat_id: int, name: str) -> None:
    d = await state.get_data()
    if not USERNAME_RE.match(name):
        await show_step(bot, state, chat_id, 1, t("warn.bad_username"))
        return
    product = "stars" if d["kind"] == "stars" else "premium_3m"
    try:
        valid = await deps.hexc.check_recipient(product, name)
    except HexError:
        await show_step(bot, state, chat_id, 1, t("warn.check_failed"))
        return
    if not valid:
        await show_step(bot, state, chat_id, 1, t("warn.recipient_not_found", name=T.esc(name)))
        return
    await state.update_data(username=name)
    await show_step(bot, state, chat_id, 2)


@router.message(Buy.username, F.text)
async def got_username(m: Message, state: FSMContext):
    await try_delete(m)
    await _accept_username(m.bot, state, m.chat.id, m.text.strip().lstrip("@"))


@router.callback_query(F.data == "self")
async def buy_for_self(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not d.get("kind"):
        await cb.answer(t("alert.session_expired"), show_alert=True)
        await show_main(cb.bot, cb.message.chat.id, Ref.of(cb.message))
        return
    if not cb.from_user.username:
        await cb.answer(t("alert.no_username"), show_alert=True)
        return
    await cb.answer()
    await _accept_username(cb.bot, state, cb.message.chat.id, cb.from_user.username)


@router.message(Buy.quantity, F.text)
async def got_quantity(m: Message, state: FSMContext):
    await try_delete(m)
    cfg = get_settings()
    raw = m.text.strip().replace(" ", "")
    if not raw.isdigit() or not (cfg.stars_min <= int(raw) <= cfg.stars_max):
        await show_step(m.bot, state, m.chat.id, 2, t("warn.bad_quantity", min=cfg.stars_min, max=cfg.stars_max))
        return
    await state.update_data(qty=int(raw))
    await show_step(m.bot, state, m.chat.id, 3)


@router.callback_query(F.data.startswith("prem:"))
async def pick_period(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    months = int(cb.data.split(":")[1])
    if not d.get("username") or months not in PERIODS:
        await cb.answer(t("alert.session_expired"), show_alert=True)
        await show_main(cb.bot, cb.message.chat.id, Ref.of(cb.message))
        return
    await cb.answer()
    await state.update_data(months=months)
    await show_step(cb.bot, state, cb.message.chat.id, 3)


@router.callback_query(F.data == "back")
async def back(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    d = await state.get_data()
    if not d.get("kind"):
        await state.clear()
        await show_main(cb.bot, cb.message.chat.id, Ref.of(cb.message))
        return
    if d.get("months") or d.get("qty"):          # шаг 3 -> шаг 2
        await state.update_data(months=None, qty=None)
        await show_step(cb.bot, state, cb.message.chat.id, 2)
    elif d.get("username"):                       # шаг 2 -> шаг 1
        await state.update_data(username=None)
        await show_step(cb.bot, state, cb.message.chat.id, 1)
    else:                                         # шаг 1 -> меню
        await state.clear()
        await show_main(cb.bot, cb.message.chat.id, Ref.of(cb.message))


@router.callback_query(F.data.in_({"pay:tr", "pay:bal"}))
async def pay(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    kind, username = d.get("kind"), d.get("username")
    if not kind or not username or not (d.get("qty") or d.get("months")):
        await cb.answer(t("alert.session_expired"), show_alert=True)
        await state.clear()
        await show_main(cb.bot, cb.message.chat.id, Ref.of(cb.message))
        return
    method = "balance" if cb.data == "pay:bal" else "transfer"
    cfg = get_settings()
    ref = Ref.from_dict(d.get("ref")) or Ref.of(cb.message)
    async with db.Session() as s:
        item = await open_item(s, cb.from_user.id)
        if item:
            await cb.answer()
            await state.clear()
            await show_open_payment(cb.bot, cb.message.chat.id, ref, *item)
            return
        try:
            q = await make_quote(s, kind, stars=d.get("qty"), months=d.get("months"))
        except (PriceUnavailable, HexError):
            await cb.answer(t("alert.prices_unavailable"), show_alert=True)
            return
        try:
            o = await create_order(s, user_id=cb.from_user.id, kind=kind, recipient=username,
                                   stars=d.get("qty"), months=d.get("months"), quote=q, method=method)
        except InsufficientBalance:
            await cb.answer(t("alert.no_funds"), show_alert=True)
            return
    await cb.answer()
    await state.clear()
    if method == "balance":
        await render(cb.bot, ref, cb.message.chat.id, "payment",
                     t("paid.balance", id=o.id, amount=T.money(o.total_cents)), K.waiting_check())
    else:
        await render(cb.bot, ref, cb.message.chat.id, "payment",
                     T.requisites(t("req.pay_title", id=o.id), o.total_cents, o.requisite, cfg.payment_ttl_minutes),
                     K.cancel_payment("o", o.id))
