"""Команды админов (только личные сообщения) и решение по росту цены Hex."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select, update

from app import db, deps, texts as T
from app.config import get_settings
from app.i18n import t
from app.db import Order, Topup, User
from app.services.errors import reason_text
from app.services.hex_client import HexError
from app.services.notify import send_to_user
from app.services.orders import credit_balance, refund_order
from app.services.pricing import get_markup, get_rate, set_setting

router = Router()
router.message.filter(F.chat.type == "private")


def _is_admin(user_id: int) -> bool:
    return user_id in get_settings().admin_id_list





@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if _is_admin(m.from_user.id):
        await m.answer(t("adm.help"))


@router.message(Command("rate"))
async def cmd_rate(m: Message, command: CommandObject):
    if not _is_admin(m.from_user.id):
        return
    async with db.Session() as s:
        if command.args:
            try:
                v = Decimal(command.args.strip().replace(",", "."))
                assert v > 0
            except (InvalidOperation, AssertionError):
                await m.answer(t("adm.fmt.rate"))
                return
            await set_setting(s, "rate", str(v))
            await s.commit()
        await m.answer(t("adm.rate.show", rate=await get_rate(s)))


@router.message(Command("markup"))
async def cmd_markup(m: Message, command: CommandObject):
    if not _is_admin(m.from_user.id):
        return
    async with db.Session() as s:
        if command.args:
            try:
                v = Decimal(command.args.strip().replace(",", "."))
                assert 0 <= v < 1000
            except (InvalidOperation, AssertionError):
                await m.answer(t("adm.fmt.markup"))
                return
            await set_setting(s, "markup", str(v))
            await s.commit()
        await m.answer(t("adm.markup.show", markup=await get_markup(s)))


@router.message(Command("balance_hex"))
async def cmd_balance_hex(m: Message):
    if not _is_admin(m.from_user.id):
        return
    try:
        b = await deps.hexc.balance()
        await m.answer(t("adm.hex_balance", amount=f"{Decimal(int(b.get('balance_nano', 0))) / 1000:.2f}"))
    except HexError as e:
        await m.answer(t("adm.hex_error", error=T.esc(e)))


@router.message(Command("stats"))
async def cmd_stats(m: Message):
    if not _is_admin(m.from_user.id):
        return
    async with db.Session() as s:
        users = await s.scalar(select(func.count(User.id)))
        by_status = dict((await s.execute(select(Order.status, func.count(Order.id)).group_by(Order.status))).all())
        revenue = await s.scalar(select(func.coalesce(func.sum(Order.total_cents), 0)).where(Order.status == "done"))
        topups = await s.scalar(select(func.coalesce(func.sum(Topup.total_cents), 0)).where(Topup.status == "credited"))
        balances = await s.scalar(select(func.coalesce(func.sum(User.balance_cents), 0)))
    lines = [f"{T.status(k, admin=True)}: {v}" for k, v in sorted(by_status.items())]
    await m.answer(t("adm.stats", users=users, lines="\n".join(lines) or t("adm.stats.no_orders"),
                     revenue=T.money(revenue), topups=T.money(topups), balances=T.money(balances)))


@router.message(Command("orders"))
async def cmd_orders(m: Message, command: CommandObject):
    if not _is_admin(m.from_user.id):
        return
    n = int(command.args) if command.args and command.args.strip().isdigit() else 10
    async with db.Session() as s:
        rows = (await s.scalars(select(Order).order_by(Order.id.desc()).limit(min(n, 30)))).all()
    if not rows:
        await m.answer(t("adm.orders.empty"))
        return
    lines = []
    for o in rows:
        what = t("adm.orders.stars", n=o.stars_count) if o.kind == "stars" else t("adm.orders.premium", n=o.months)
        lines.append(t("adm.orders.item", id=o.id, what=what, recipient=T.esc(o.recipient), price=T.money(o.total_cents),
                       uid=o.user_id, status=T.status(o.status, admin=True)))
    await m.answer("\n".join(lines))


@router.message(Command("ban", "unban"))
async def cmd_ban(m: Message, command: CommandObject):
    if not _is_admin(m.from_user.id):
        return
    if not command.args or not command.args.strip().lstrip("-").isdigit():
        await m.answer(t("adm.fmt.ban", cmd=command.command))
        return
    uid = int(command.args.strip())
    async with db.Session() as s:
        res = await s.execute(update(User).where(User.id == uid).values(is_banned=command.command == "ban"))
        await s.commit()
    await m.answer(t("adm.done") if res.rowcount else t("adm.not_found"))


@router.message(Command("credit"))
async def cmd_credit(m: Message, command: CommandObject):
    if not _is_admin(m.from_user.id):
        return
    parts = (command.args or "").split()
    try:
        uid, amount = int(parts[0]), Decimal(parts[1].replace(",", "."))
        assert amount > 0
    except (IndexError, ValueError, InvalidOperation, AssertionError):
        await m.answer(t("adm.fmt.credit"))
        return
    cents = int((amount * 100).to_integral_value())
    async with db.Session() as s:
        if not await s.get(User, uid):
            await m.answer(t("adm.not_found"))
            return
        await credit_balance(s, uid, cents)
        await s.commit()
    await send_to_user(m.bot, uid, lambda: t("notify.credited", amount=T.money(cents)))
    await m.answer(t("adm.credited", amount=T.money(cents), uid=uid))


# ── решение по росту цены Hex ────────────────────────────────────────
@router.callback_query(F.data.startswith("pr:"))
async def price_decision(cb: CallbackQuery):
    cfg = get_settings()
    if not _is_admin(cb.from_user.id):
        await cb.answer(t("adm.alert.no_access"), show_alert=True)
        return
    _, action, sid = cb.data.split(":")
    async with db.Session() as s:
        o = await s.get(Order, int(sid))
        if o is None or o.status != "price_review":
            await cb.answer(t("adm.alert.done_already"), show_alert=True)
            return
        if action == "ok":
            ceiling = int(Decimal(o.hex_total_nano or 0) * (Decimal(1) + cfg.price_guard_percent / 100))
            res = await s.execute(update(Order).where(Order.id == o.id, Order.status == "price_review")
                                  .values(status="hex_waiting", price_ceiling_nano=ceiling))
            await s.commit()
            verdict = t("adm.verdict.price_ok") if res.rowcount else t("adm.alert.done_already")
        else:
            res = await s.execute(update(Order).where(Order.id == o.id, Order.status == "price_review").values(status="approved"))
            if res.rowcount:
                o.status = "approved"
                done = await refund_order(s, o, "price_declined", "declined by admin")
                await s.commit()
                if done:
                    await send_to_user(cb.bot, o.user_id, lambda: t("fulfil.failed", id=o.id, reason=reason_text("price_declined"),
                                                                      amount=T.money(o.total_cents)))
            verdict = t("adm.verdict.price_no") if res.rowcount else t("adm.alert.done_already")
    await cb.answer(verdict, show_alert=True)
    try:
        await cb.message.edit_text(f"{cb.message.html_text}\n\n{verdict}", reply_markup=None)
    except Exception:
        pass
