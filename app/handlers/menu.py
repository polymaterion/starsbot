from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select, update

from app import db, i18n, keyboards as K, texts as T
from app.db import Order, User
from app.i18n import t
from app.ui import Ref, render

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


async def render_profile(cb: CallbackQuery) -> None:
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


@router.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    await cb.answer()
    await render_profile(cb)


@router.callback_query(F.data == "lang")
async def cb_lang(cb: CallbackQuery):
    await cb.answer()
    await render(cb.bot, Ref.of(cb.message), cb.message.chat.id, "profile", t("lang.screen"), K.language_menu())


@router.callback_query(F.data.startswith("lang:"))
async def cb_set_lang(cb: CallbackQuery):
    code = cb.data.split(":", 1)[1]
    if code not in i18n.available_languages():
        await cb.answer(t("alert.stale_button"))
        return
    async with db.Session() as s:
        await s.execute(update(User).where(User.id == cb.from_user.id).values(lang=code))
        await s.commit()
    i18n.set_current_lang(code)           # дальше этот же апдейт рисуется уже на новом языке
    await cb.answer(t("lang.changed"))
    await render_profile(cb)


@router.callback_query(F.data == "history")
async def cb_history(cb: CallbackQuery):
    await cb.answer()
    async with db.Session() as s:
        rows = (await s.scalars(select(Order).where(Order.user_id == cb.from_user.id).order_by(Order.id.desc()).limit(10))).all()
    if not rows:
        body = t("history.empty")
    else:
        lines = []
        for o in rows:
            what = t("history.stars", n=o.stars_count) if o.kind == "stars" else t("history.premium", n=o.months)
            lines.append(t("history.item", id=o.id, what=what, recipient=T.esc(o.recipient),
                           price=T.money(o.total_cents), status=T.status(o.status), date=f"{o.created_at:%d.%m}"))
        body = "\n".join(lines)
    await render(cb.bot, Ref.of(cb.message), cb.message.chat.id, "profile",
                 t("history.title", body=body), K.back("profile"))


@router.callback_query()
async def stale_callback(cb: CallbackQuery):
    """Кнопка от старого сообщения (например, удалённая «Поддержка» из прежнего меню): не оставляем «часики»."""
    await cb.answer(t("alert.stale_button"))
