"""Отрисовка экранов: одно сообщение редактируется на месте (баннер + подпись + кнопки)."""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InputMediaPhoto, Message

log = logging.getLogger(__name__)

BANNERS_DIR = Path(__file__).resolve().parent.parent / "assets" / "banners"
_file_ids: dict[str, str] = {}


@dataclass
class Ref:
    chat_id: int
    message_id: int
    photo: bool

    @classmethod
    def of(cls, m: Message) -> "Ref":
        return cls(m.chat.id, m.message_id, bool(m.photo))

    @classmethod
    def from_dict(cls, d: dict | None) -> "Ref | None":
        return cls(**d) if d else None

    def to_dict(self) -> dict:
        return asdict(self)


def _media(name: str):
    if name in _file_ids:
        return _file_ids[name]
    p = BANNERS_DIR / f"{name}.png"
    return FSInputFile(p) if p.exists() else None


def _remember(name: str, msg) -> None:
    if isinstance(msg, Message) and msg.photo:
        _file_ids[name] = msg.photo[-1].file_id


async def render(bot: Bot, ref: Ref | None, chat_id: int, banner: str, text: str,
                 kb: InlineKeyboardMarkup | None) -> Ref:
    media = _media(banner)
    if ref:
        try:
            if media and ref.photo:
                msg = await bot.edit_message_media(
                    chat_id=ref.chat_id, message_id=ref.message_id,
                    media=InputMediaPhoto(media=media, caption=text, parse_mode=ParseMode.HTML), reply_markup=kb)
                _remember(banner, msg)
                return ref
            if not media and not ref.photo:
                await bot.edit_message_text(text, chat_id=ref.chat_id, message_id=ref.message_id,
                                            reply_markup=kb, parse_mode=ParseMode.HTML)
                return ref
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return ref
            log.info("edit failed, resending: %s", e)
        try:
            await bot.delete_message(ref.chat_id, ref.message_id)
        except Exception:
            pass
    if media:
        msg = await bot.send_photo(chat_id, media, caption=text, reply_markup=kb, parse_mode=ParseMode.HTML)
        _remember(banner, msg)
        return Ref(chat_id, msg.message_id, True)
    msg = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)
    return Ref(chat_id, msg.message_id, False)


async def draw(bot: Bot, state: FSMContext, chat_id: int, banner: str, text: str,
               kb: InlineKeyboardMarkup | None) -> None:
    """render() для экранов с FSM: ссылка на сообщение хранится в данных состояния."""
    ref = Ref.from_dict((await state.get_data()).get("ref"))
    ref = await render(bot, ref, chat_id, banner, text, kb)
    await state.update_data(ref=ref.to_dict())


async def try_delete(m: Message) -> None:
    try:
        await m.delete()
    except Exception:
        pass
