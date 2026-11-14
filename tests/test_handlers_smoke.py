"""Сквозной прогон: настоящий Dispatcher + двойник Telegram-сессии."""
from datetime import datetime
from io import BytesIO

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (DeleteMessage, EditMessageCaption, EditMessageMedia, EditMessageText, SendDocument,
                             SendMessage, SendPhoto)
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User as TgUser

from app import db, deps
from app.db import Order, Topup, User
from app.handlers import admin, flow, menu, payments
from app.middleware import UserMiddleware
from app.services.pricing import Catalog

GROUP = -100


class MockSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.mid = 1000

    async def close(self): ...
    async def stream_content(self, *a, **k): ...

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendPhoto, SendDocument, EditMessageMedia, EditMessageText, EditMessageCaption)):
            self.mid += 1
            photo = [PhotoSize(file_id="banner", file_unique_id="b", width=1, height=1)] \
                if isinstance(method, (SendPhoto, EditMessageMedia)) else None
            chat_id = getattr(method, "chat_id", 1)
            return Message(message_id=self.mid, date=datetime.now(), chat=Chat(id=chat_id, type="private"), photo=photo)
        return True

    def of(self, cls):
        return [c for c in self.calls if isinstance(c, cls)]


def tg_user(uid, username="client"):
    return TgUser(id=uid, is_bot=False, first_name="N", username=username)


class Harness:
    def __init__(self):
        self.session = MockSession()
        self.bot = Bot("1:test", session=self.session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = Dispatcher(storage=MemoryStorage())
        self.dp.message.outer_middleware(UserMiddleware())
        self.dp.callback_query.outer_middleware(UserMiddleware())
        for r in (admin.router, payments.router, flow.router, menu.router):
            r._parent_router = None  # роутеры модульные: в тестах Dispatcher создаётся заново
        self.dp.include_routers(admin.router, payments.router, flow.router, menu.router)
        self.uid = 0

        async def fake_download(file, *a, **k):
            return BytesIO(b"screenshot-bytes-" + str(self.uid).encode() + str(len(self.session.calls)).encode())
        self.bot.download = fake_download

    def _next(self):
        self.uid += 1
        return self.uid

    async def say(self, uid, text=None, photo=False, username="client"):
        msg = Message(message_id=self._next(), date=datetime.now(), chat=Chat(id=uid, type="private"),
                      from_user=tg_user(uid, username), text=text,
                      photo=[PhotoSize(file_id="f1", file_unique_id="u1", width=1, height=1)] if photo else None)
        await self.dp.feed_update(self.bot, Update(update_id=self._next(), message=msg))

    async def click(self, uid, data, chat_id=None, photo_msg=True, username="client"):
        chat_id = chat_id or uid
        msg = Message(message_id=500, date=datetime.now(), chat=Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup"),
                      photo=[PhotoSize(file_id="b", file_unique_id="b", width=1, height=1)] if photo_msg else None,
                      caption="карточка" if chat_id < 0 else None)
        cb = CallbackQuery(id=str(self._next()), from_user=tg_user(uid, username), chat_instance="x", data=data, message=msg)
        await self.dp.feed_update(self.bot, Update(update_id=self._next(), callback_query=cb))


@pytest.fixture
def h(env):
    from app.services.pricing import Catalog
    deps.catalog = Catalog(deps.hexc)
    return Harness()


def last_text(h):
    for c in reversed(h.session.calls):
        t = getattr(c, "caption", None) or getattr(c, "text", None) or getattr(getattr(c, "media", None), "caption", None)
        if t:
            return t
    return ""


async def test_full_stars_purchase_via_transfer(h):
    uid = 100
    await h.say(uid, "/start")
    assert "Добро пожаловать" in last_text(h)
    assert h.session.of(SendPhoto)                       # баннер отправлен

    await h.click(uid, "stars")
    assert "[1/3]" in last_text(h) and "Не выбран" in last_text(h)

    await h.say(uid, "nobody")                           # неизвестный получатель
    assert "не найден" in last_text(h)

    await h.say(uid, "durov")
    assert "[2/3]" in last_text(h) and "@durov" in last_text(h)

    await h.say(uid, "10")                               # меньше минимума
    assert "от 50" in last_text(h)

    await h.say(uid, "100")
    assert "[3/3]" in last_text(h) and "TMT" in last_text(h)

    await h.click(uid, "pay:tr")
    assert "Переведите ровно" in last_text(h)
    async with db.Session() as s:
        o = await s.get(Order, 1)
        assert o.status == "awaiting_payment" and o.requisite in ("99361000001", "99361000002")
        total = o.total_cents

    await h.click(uid, "stars")                          # второй неоплаченный заказ запрещён
    assert "уже есть неоплаченный платёж" in last_text(h)

    await h.say(uid, photo=True)                         # скриншот -> в админ-группу
    sent = h.session.of(SendPhoto)[-1]
    assert sent.chat_id == GROUP and "Платёж по заказу #1" in sent.caption
    async with db.Session() as s:
        assert (await s.get(Order, 1)).status == "proof_sent"

    await h.click(5, "adm:ok:o:1", chat_id=GROUP, username="rand")   # посторонний
    async with db.Session() as s:
        assert (await s.get(Order, 1)).status == "proof_sent"

    await h.click(1, "adm:ok:o:1", chat_id=GROUP, username="admin")  # админ из ADMIN_IDS
    async with db.Session() as s:
        o = await s.get(Order, 1)
        assert o.status == "approved"
    assert any("Оплата заказа #1 подтверждена" in (getattr(c, "text", "") or "") for c in h.session.of(SendMessage))

    await h.click(1, "adm:ok:o:1", chat_id=GROUP, username="admin")  # повторное нажатие безопасно
    async with db.Session() as s:
        assert (await s.get(Order, 1)).status == "approved"

    from app.services import fulfillment
    await fulfillment.process_order(h.bot, 1)
    await fulfillment.process_order(h.bot, 1)
    async with db.Session() as s:
        assert (await s.get(Order, 1)).status == "done"
    assert any("выполнен" in (getattr(c, "text", "") or "") for c in h.session.of(SendMessage))
    assert total > 0


async def test_reject_allows_resend_then_closes_and_duplicate_blocked(h):
    uid = 100
    await h.click(uid, "premium")
    await h.click(uid, "self", username="client")
    assert "[2/3]" in last_text(h)
    await h.click(uid, "prem:3")
    assert "[3/3]" in last_text(h)
    await h.click(uid, "pay:tr")
    for attempt in range(1, 4):
        await h.say(uid, photo=True)
        # каждый скриншот должен отличаться по хешу
        await h.click(1, "adm:no:o:1", chat_id=GROUP, username="admin")
        async with db.Session() as s:
            o = await s.get(Order, 1)
            assert o.proof_attempts == attempt
            assert o.status == ("awaiting_payment" if attempt < 3 else "rejected")


async def test_duplicate_screenshot_rejected(h):
    h.bot.download = lambda *a, **k: _same()
    async def _same():
        return BytesIO(b"same")
    uid = 100
    await h.click(uid, "stars"); await h.click(uid, "self")
    await h.say(uid, "100"); await h.click(uid, "pay:tr")
    await h.say(uid, photo=True)
    await h.click(1, "adm:no:o:1", chat_id=GROUP, username="admin")
    await h.say(uid, photo=True)
    assert "уже использовался" in last_text(h)


async def test_cancel_then_new_order_and_balance_payment(h):
    uid = 100
    await h.click(uid, "stars"); await h.click(uid, "self"); await h.say(uid, "50"); await h.click(uid, "pay:tr")
    await h.click(uid, "cancel:o:1")
    async with db.Session() as s:
        assert (await s.get(Order, 1)).status == "cancelled"
        u = await s.get(User, uid); u.balance_cents = 100_000; await s.commit()
    await h.click(uid, "stars"); await h.click(uid, "self"); await h.say(uid, "50"); await h.click(uid, "pay:bal")
    assert "оплачен с баланса" in last_text(h)
    async with db.Session() as s:
        o = await s.get(Order, 2)
        assert o.status == "approved" and o.pay_method == "balance"
        assert (await s.get(User, uid)).balance_cents == 100_000 - o.total_cents


async def test_topup_flow_credits_balance(h):
    uid = 100
    await h.click(uid, "balance")
    assert "Баланс" in last_text(h)
    await h.click(uid, "topup")
    await h.say(uid, "5")                                 # ниже минимума 10
    assert "Введите сумму от" in last_text(h)
    await h.say(uid, "50")
    assert "Пополнение баланса #1" in last_text(h)
    async with db.Session() as s:
        t = await s.get(Topup, 1)
        assert 5000 < t.total_cents <= 5049
        total = t.total_cents
    await h.say(uid, photo=True)
    await h.click(1, "adm:ok:t:1", chat_id=GROUP, username="admin")
    async with db.Session() as s:
        assert (await s.get(User, uid)).balance_cents == total
        assert (await s.get(Topup, 1)).status == "credited"


async def test_profile_support_history_and_banned(h):
    uid = 100
    await h.click(uid, "profile"); assert "Ваш профиль" in last_text(h)
    await h.click(uid, "history"); assert "Покупок пока нет" in last_text(h)
    await h.click(uid, "support"); assert "Поддержка" in last_text(h)
    async with db.Session() as s:
        u = await s.get(User, uid); u.is_banned = True; await s.commit()
    n = len(h.session.calls)
    await h.say(uid, "/start")
    assert "Доступ ограничен" in last_text(h) and len(h.session.calls) == n + 1


async def test_admin_commands_only_for_admins(h):
    await h.say(1, "/rate 0.05", username="admin")
    assert "0.05" in last_text(h)
    await h.say(1, "/markup 12", username="admin")
    assert "12" in last_text(h)
    before = last_text(h)
    await h.say(100, "/rate 9")                           # не админ — курс не меняется
    async with db.Session() as s:
        from app.services.pricing import get_rate
        from decimal import Decimal
        assert await get_rate(s) == Decimal("0.05")
    await h.say(1, "/balance_hex", username="admin"); assert "Депозит Hex" in last_text(h)
    await h.say(1, "/stats", username="admin"); assert "Статистика" in last_text(h)
    await h.say(1, "/credit 100 25,5", username="admin")
    async with db.Session() as s:
        assert (await s.get(User, 100)).balance_cents == 2550
