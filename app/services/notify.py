from __future__ import annotations

import logging
from typing import Callable

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from app import db, i18n
from app.config import get_settings
from app.db import User

log = logging.getLogger(__name__)


async def safe_send(bot: Bot, chat_id: int, text: str, kb: InlineKeyboardMarkup | None = None) -> bool:
    try:
        await bot.send_message(chat_id, text, reply_markup=kb)
        return True
    except Exception as e:  # пользователь заблокировал бота, админ не нажимал /start и т.п.
        log.warning("send to %s failed: %s", chat_id, e)
        return False


async def notify_admins(bot: Bot, text: str, kb: InlineKeyboardMarkup | None = None) -> list[tuple[int, int]]:
    """Личные уведомления админам. Возвращает [(admin_id, message_id)] для доставленных."""
    sent = []
    for admin_id in get_settings().admin_id_list:
        try:
            m = await bot.send_message(admin_id, text, reply_markup=kb)
            sent.append((admin_id, m.message_id))
        except Exception as e:
            log.warning("admin %s unreachable: %s", admin_id, e)
    return sent


async def send_to_user(bot: Bot, chat_id: int, build: Callable[[], str], kb: InlineKeyboardMarkup | None = None) -> bool:
    """Сообщение клиенту на ЕГО языке (из профиля), а не на языке того, кто вызвал действие.
    build — функция без аргументов, возвращающая текст; вызывается внутри языкового контекста получателя."""
    lang = None
    try:
        async with db.Session() as s:
            user = await s.get(User, chat_id)
            lang = user.lang if user else None
    except Exception:
        log.exception("cannot load language of %s", chat_id)
    with i18n.lang_ctx(lang):
        text = build()
    return await safe_send(bot, chat_id, text, kb)
