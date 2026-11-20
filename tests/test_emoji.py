from datetime import datetime

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, Chat

from app import emoji as E
from app.config import get_settings


def test_apply_text_wraps_with_fallback_and_handles_variation_selector():
    out = E.apply_text("⚠️ Внимание ⏱ 10 мин. 🛠 админ")
    assert out.startswith('<tg-emoji emoji-id="5985346521103604145">⚠️</tg-emoji> Внимание')
    assert '<tg-emoji emoji-id="5776213190387961618">⏱</tg-emoji>' in out     # без FE0F
    assert "🛠 админ" in out and "tg-emoji emoji-id=\"\"" not in out           # у 🛠 ID нет — не трогаем
    assert E.apply_text("без эмодзи <b>тег</b>") == "без эмодзи <b>тег</b>"


def test_apply_button_moves_emoji_to_icon():
    b = E.apply_button(InlineKeyboardButton(text="✅ Подтвердить", callback_data="x"))
    assert b.text == "Подтвердить" and b.icon_custom_emoji_id == "5985596818912712352" and b.callback_data == "x"
    plain = InlineKeyboardButton(text="‹ Назад", callback_data="back")
    assert E.apply_button(plain) == plain
    unmapped = InlineKeyboardButton(text="↩️ Вернуть", callback_data="y")
    assert E.apply_button(unmapped) == unmapped
    url = E.apply_button(InlineKeyboardButton(text="💬 Поддержка", url="https://t.me/s"))
    assert url.url == "https://t.me/s" and url.icon_custom_emoji_id == "5884510167986343350"


def test_all_client_emoji_have_ids():
    assert len(E.EMOJI_IDS) == 25 and all(v.isdigit() for v in E.EMOJI_IDS.values())


async def test_middleware_retries_plain_when_telegram_rejects():
    class Session(BaseSession):
        def __init__(self):
            super().__init__()
            self.texts = []
        async def close(self): ...
        async def stream_content(self, *a, **k): ...
        async def make_request(self, bot, method, timeout=None):
            self.texts.append(method.text)
            if "tg-emoji" in method.text:
                raise TelegramBadRequest(method=method, message="Bad Request: custom emoji not allowed")
            return Message(message_id=1, date=datetime.now(), chat=Chat(id=1, type="private"))

    s = Session()
    s.middleware(E.PremiumEmojiMiddleware())
    bot = Bot("1:t", session=s)
    await bot.send_message(1, "✅ готово")
    assert "tg-emoji" in s.texts[0] and s.texts[1] == "✅ готово"       # второй запрос — без премиум-эмодзи


def test_support_link_normalisation(monkeypatch):
    cfg = get_settings()
    for raw, want in [("@my_support", "https://t.me/my_support"), ("my_support", "https://t.me/my_support"),
                      ("https://t.me/x_support", "https://t.me/x_support"), ("tg://resolve?domain=abc", "tg://resolve?domain=abc"),
                      ("", ""), ("не ссылка", "")]:
        monkeypatch.setattr(cfg, "support_url", raw)
        assert cfg.support_link == want, raw


def test_support_button_hidden_without_link(monkeypatch):
    from app import keyboards as K
    monkeypatch.setattr(get_settings(), "support_url", "")
    assert all("Поддержка" not in b.text for row in K.main().inline_keyboard for b in row)
