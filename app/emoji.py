"""Премиум-эмодзи (custom emoji).

Один перехватчик исходящих запросов (PremiumEmojiMiddleware) делает всё сам:
  * в тексте/подписях заменяет обычные эмодзи на <tg-emoji emoji-id="…">…</tg-emoji>;
  * на inline-кнопках, текст которых начинается с эмодзи, ставит icon_custom_emoji_id
    и убирает эмодзи из текста (иначе будет дубль).

Если Telegram отклонит запрос с премиум-эмодзи (например, у владельца бота нет Premium),
запрос автоматически повторяется с обычными эмодзи — бот не ломается.

Чтобы заменить или добавить эмодзи — правьте EMOJI_IDS (ключ без U+FE0F).
Отключить всё целиком: PREMIUM_EMOJI=false в .env.
"""
from __future__ import annotations

import logging
import re

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (EditMessageCaption, EditMessageMedia, EditMessageText, SendDocument, SendMessage,
                             SendPhoto)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)

# эмодзи (без variation selector U+FE0F) -> id премиум-эмодзи
EMOJI_IDS: dict[str, str] = {
    "⭐": "5958376256788502078",
    "🎁": "6032937473162614352",
    "👛": "5927169041595634481",
    "👤": "5886412370347036129",
    "💬": "5884510167986343350",
    "💳": "5987880246865565644",
    "➕": "5775937998948404844",
    "🧾": "6008163345885040772",
    "✉": "5884510167986343350",
    "🚫": "5985346521103604145",
    "👋": "5958413064658228696",
    "⬇": "5875008416132370818",
    "✏": "5879841310902324730",
    "⚠": "5985346521103604145",
    "💰": "5992430854909989581",
    "✅": "5985596818912712352",
    "❌": "5985346521103604145",
    "🔎": "5874960879434338403",
    "⏱": "5776213190387961618",
    "⏳": "5900104897885376843",
    "⚙": "5988023995125993550",
    "🚚": "5927266769281487788",
    "🔍": "5843862283964390528",
    "📊": "5994378914636500516",
    "📅": "5967412305338568701",
    "🌐": "5879585266426973039",   # знак языка
    "🇷🇺": "5291734595862018096",   # флаг России
    "🇹🇲": "5292055356904585500",   # флаг Туркменистана
    # админские (ID пока не заданы, остаются обычными): ↩ 🛠 🚨 ℹ
}

_ALT = "|".join(sorted((re.escape(k) for k in EMOJI_IDS), key=len, reverse=True))
_ANY = re.compile(f"({_ALT})\uFE0F?")
_LEAD = re.compile(f"^({_ALT})\uFE0F?\\s*")


def apply_text(text: str) -> str:
    """HTML-текст: обычные эмодзи -> <tg-emoji>. Обычный эмодзи остаётся внутри как запасной вариант."""
    return _ANY.sub(lambda m: f'<tg-emoji emoji-id="{EMOJI_IDS[m.group(1)]}">{m.group(0)}</tg-emoji>', text)


def apply_button(b: InlineKeyboardButton) -> InlineKeyboardButton:
    if b.icon_custom_emoji_id or not b.text:
        return b
    m = _LEAD.match(b.text)
    if not m:
        return b
    rest = b.text[m.end():].strip()
    if not rest:
        return b
    return b.model_copy(update={"text": rest, "icon_custom_emoji_id": EMOJI_IDS[m.group(1)]})


def apply_markup(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    return markup.model_copy(update={"inline_keyboard": [[apply_button(b) for b in row] for row in markup.inline_keyboard]})


def transform_method(method):
    """Возвращает копию метода с премиум-эмодзи или None, если менять нечего."""
    upd: dict = {}
    if isinstance(method, (SendMessage, EditMessageText)):
        if method.text:
            upd["text"] = apply_text(method.text)
    elif isinstance(method, (SendPhoto, SendDocument, EditMessageCaption)):
        if method.caption:
            upd["caption"] = apply_text(method.caption)
    elif isinstance(method, EditMessageMedia):
        media = method.media
        if getattr(media, "caption", None):
            upd["media"] = media.model_copy(update={"caption": apply_text(media.caption)})
    markup = getattr(method, "reply_markup", None)
    if isinstance(markup, InlineKeyboardMarkup):
        new = apply_markup(markup)
        if new != markup:
            upd["reply_markup"] = new
    if not upd:
        return None
    changed = method.model_copy(update=upd)
    return changed


class PremiumEmojiMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        new = transform_method(method)
        if new is None:
            return await make_request(bot, method)
        try:
            return await make_request(bot, new)
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                raise
            log.warning("request with premium emoji rejected (%s), retrying with plain emoji", e)
            return await make_request(bot, method)
