from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from app.config import get_settings

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
