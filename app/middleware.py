from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app import db
from app.config import get_settings
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
                await event.answer("Доступ ограничен", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("Доступ ограничен. Обратитесь в поддержку.")
            return None
        data["db_user"] = user
        return await handler(event, data)
