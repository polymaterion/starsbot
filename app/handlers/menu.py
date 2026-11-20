from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app import db, keyboards as K, texts as T
from app.db import Order, User
from app.ui import Ref, render, try_delete

router = Router()
router.message.filter(F.chat.type == "private")


async def show_main(bot, chat_id: int, ref: Ref | None, note: str = "") -> None:
    text = (note + "\n\n" if note else "") + T.main_menu()
    await render(bot, ref, chat_id, "main", text, K.main())


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await show_main(m.bot, m.chat.id, None)


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await show_main(cb.bot, cb.message.chat.id, Ref.of(cb.message))


@router.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    async with db.Session() as s:
        user = await s.get(User, uid)
        total, stars, months = (await s.execute(
            select(func.count(Order.id), func.coalesce(func.sum(Order.stars_count), 0), func.coalesce(func.sum(Order.months), 0))
            .where(Order.user_id == uid, Order.status == "done")
        )).one()
        history = await s.scalar(select(func.count(Order.id)).where(Order.user_id == uid))
    reg = user.created_at.strftime("%d.%m.%Y")
    await render(cb.bot, Ref.of(cb.message), cb.message.chat.id, "profile",
                 T.profile(uid, user.username, user.balance_cents, total, int(stars), int(months), reg),
                 K.profile_menu(history or 0))


@router.callback_query(F.data == "history")
async def cb_history(cb: CallbackQuery):
    await cb.answer()
    async with db.Session() as s:
        rows = (await s.scalars(select(Order).where(Order.user_id == cb.from_user.id).order_by(Order.id.desc()).limit(10))).all()
    if not rows:
        body = "Покупок пока нет."
    else:
        lines = []
        for o in rows:
            what = f"⭐ {o.stars_count}" if o.kind == "stars" else f"🎁 {o.months} мес."
            lines.append(f"#{o.id} · {what} → @{T.esc(o.recipient)} · {T.money(o.total_cents)} TMT\n    {T.STATUS_RU.get(o.status, o.status)} · {o.created_at:%d.%m}")
        body = "\n".join(lines)
    await render(cb.bot, Ref.of(cb.message), cb.message.chat.id, "profile",
                 f"🧾 <b>История покупок</b> (последние 10)\n\n{body}", K.back("profile"))


@router.message(StateFilter(None), F.text)
async def fallback_text(m: Message):
    await try_delete(m)
    await show_main(m.bot, m.chat.id, None)
