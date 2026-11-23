from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app import db, i18n
from app.config import get_settings
from app.i18n import t
from app.db import User


class UserMiddleware(BaseMiddleware):
    """Регистрирует пользователя, обновляет username, блокирует забаненных."""

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]], event: TelegramObject, data: dict) -> Any:
        tg = data.get("event_from_user")
        if tg is None or tg.is_bot:
            return await handler(event, data)
        async with db.Session() as s:
            user = await s.get(User, tg.id)
            if user is None:
                user = User(id=tg.id, username=tg.username)
                s.add(user)
            elif user.username != tg.username:
                user.username = tg.username
            await s.commit()
        if user.is_banned and tg.id not in get_settings().admin_id_list:
            if isinstance(event, CallbackQuery):
                await event.answer(t("ban.alert"), show_alert=True)
            elif isinstance(event, Message):
                await event.answer(t("ban.message"))
            return None
        data["db_user"] = user
        token = i18n.push_user_lang(user.lang)
        try:
            return await handler(event, data)
        finally:
            i18n.pop_user_lang(token)
