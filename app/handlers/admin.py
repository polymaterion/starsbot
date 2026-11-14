"""Команды админов (только личные сообщения) и решение по росту цены Hex."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select, update

from app import db, deps, texts as T
from app.config import get_settings
from app.db import Order, Topup, User
from app.services.hex_client import HexError
from app.services.notify import safe_send
from app.services.orders import credit_balance, refund_order
from app.services.pricing import get_markup, get_rate, set_setting

router = Router()
router.message.filter(F.chat.type == "private")


def _is_admin(user_id: int) -> bool:
    return user_id in get_settings().admin_id_list


HELP = (
    "🛠 <b>Команды админа</b>\n"
    "/rate [значение] — курс, TMT за 1 ₽ (без аргумента — показать)\n"
    "/markup [процент] — наценка, %\n"
    "/balance_hex — депозит Hex\n"
    "/stats — статистика\n"
    "/orders [N] — последние заказы (по умолчанию 10)\n"
    "/ban ID, /unban ID — блокировка клиента\n"
    "/credit ID сумма — начислить TMT на баланс клиента"
)


@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if _is_admin(m.from_user.id):
        await m.answer(HELP)


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
                await m.answer("Формат: /rate 0.045")
                return
            await set_setting(s, "rate", str(v))
            await s.commit()
        await m.answer(f"Курс: 1 ₽ = <b>{await get_rate(s)}</b> TMT")


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
                await m.answer("Формат: /markup 10")
                return
            await set_setting(s, "markup", str(v))
            await s.commit()
        await m.answer(f"Наценка: <b>{await get_markup(s)}%</b>")


@router.message(Command("balance_hex"))
async def cmd_balance_hex(m: Message):
    if not _is_admin(m.from_user.id):
        return
    try:
        b = await deps.hexc.balance()
        await m.answer(f"Депозит Hex: <b>{Decimal(int(b.get('balance_nano', 0))) / 1000:.2f} ₽</b>")
    except HexError as e:
        await m.answer(f"Hex вернул ошибку: {T.esc(e)}")


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
    lines = [f"{T.STATUS_RU.get(k, k)}: {v}" for k, v in sorted(by_status.items())]
    await m.answer(
        f"📊 <b>Статистика</b>\nКлиентов: {users}\n\n" + ("\n".join(lines) or "Заказов нет") +
        f"\n\nВыручка (выполненные): <b>{T.money(revenue)} TMT</b>\nПополнений: {T.money(topups)} TMT\n"
        f"Остатки на балансах клиентов: {T.money(balances)} TMT")


@router.message(Command("orders"))
async def cmd_orders(m: Message, command: CommandObject):
    if not _is_admin(m.from_user.id):
        return
    n = int(command.args) if command.args and command.args.strip().isdigit() else 10
    async with db.Session() as s:
        rows = (await s.scalars(select(Order).order_by(Order.id.desc()).limit(min(n, 30)))).all()
    if not rows:
        await m.answer("Заказов нет.")
        return
    lines = []
    for o in rows:
        what = f"⭐{o.stars_count}" if o.kind == "stars" else f"🎁{o.months}м"
        lines.append(f"#{o.id} {what} @{T.esc(o.recipient)} · {T.money(o.total_cents)} TMT · uid {o.user_id}\n   {T.STATUS_RU.get(o.status, o.status)}")
    await m.answer("\n".join(lines))


@router.message(Command("ban", "unban"))
async def cmd_ban(m: Message, command: CommandObject):
    if not _is_admin(m.from_user.id):
        return
    if not command.args or not command.args.strip().lstrip("-").isdigit():
        await m.answer(f"Формат: /{command.command} ID")
        return
    uid = int(command.args.strip())
    async with db.Session() as s:
        res = await s.execute(update(User).where(User.id == uid).values(is_banned=command.command == "ban"))
        await s.commit()
    await m.answer("Готово." if res.rowcount else "Клиент не найден.")


@router.message(Command("credit"))
async def cmd_credit(m: Message, command: CommandObject):
    if not _is_admin(m.from_user.id):
        return
    parts = (command.args or "").split()
    try:
        uid, amount = int(parts[0]), Decimal(parts[1].replace(",", "."))
        assert amount > 0
    except (IndexError, ValueError, InvalidOperation, AssertionError):
        await m.answer("Формат: /credit ID сумма")
        return
    cents = int((amount * 100).to_integral_value())
    async with db.Session() as s:
        if not await s.get(User, uid):
            await m.answer("Клиент не найден.")
            return
        await credit_balance(s, uid, cents)
        await s.commit()
    await safe_send(m.bot, uid, f"💰 На ваш баланс зачислено {T.money(cents)} TMT.")
    await m.answer(f"Начислено {T.money(cents)} TMT клиенту {uid}.")


# ── решение по росту цены Hex ────────────────────────────────────────
@router.callback_query(F.data.startswith("pr:"))
async def price_decision(cb: CallbackQuery):
    cfg = get_settings()
    if not _is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    _, action, sid = cb.data.split(":")
    async with db.Session() as s:
        o = await s.get(Order, int(sid))
        if o is None or o.status != "price_review":
            await cb.answer("Уже обработано", show_alert=True)
            return
        if action == "ok":
            ceiling = int(Decimal(o.hex_total_nano or 0) * (Decimal(1) + cfg.price_guard_percent / 100))
            res = await s.execute(update(Order).where(Order.id == o.id, Order.status == "price_review")
                                  .values(status="hex_waiting", price_ceiling_nano=ceiling))
            await s.commit()
            verdict = "✅ Выполняем по новой цене" if res.rowcount else "Уже обработано"
        else:
            res = await s.execute(update(Order).where(Order.id == o.id, Order.status == "price_review").values(status="approved"))
            if res.rowcount:
                o.status = "approved"
                done = await refund_order(s, o, "price_declined", "declined by admin")
                await s.commit()
                if done:
                    await safe_send(cb.bot, o.user_id,
                                    f"❌ <b>Заказ #{o.id} не выполнен</b>\n{T.esc('Цена у поставщика заметно выросла, и заказ не может быть выполнен по прежней стоимости.')}\n\n"
                                    f"💰 {T.money(o.total_cents)} TMT возвращены на баланс бота.")
            verdict = "↩️ Отменено, деньги возвращены клиенту" if res.rowcount else "Уже обработано"
    await cb.answer(verdict, show_alert=True)
    try:
        await cb.message.edit_text(f"{cb.message.html_text}\n\n{verdict}", reply_markup=None)
    except Exception:
        pass
