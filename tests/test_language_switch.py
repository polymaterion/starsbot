"""Выбор языка пользователем: профиль, сохранение в БД, язык получателя в уведомлениях, миграция."""
import sqlite3

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageMedia, SendMessage, SendPhoto
from sqlalchemy import select

from app import db, deps, i18n
from app.db import Order, User, utcnow
from app.services import fulfillment
from app.services.pricing import Catalog
from test_handlers_smoke import GROUP, Harness
from test_i18n import visible

ADMIN = 1


@pytest.fixture
def h(env):
    deps.catalog = Catalog(deps.hexc)
    deps.hexc.total_nano = 50_000          # цена Hex ниже оценки для 50 звёзд (≈66 ₽): защита цены не срабатывает
    return Harness()


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def sent_to(h, uid) -> list[str]:
    return [c.text for c in h.session.of(SendMessage) if c.chat_id == uid]


async def set_lang(uid, lang):
    async with db.Session() as s:
        u = await s.get(User, uid) or User(id=uid)
        u.lang = lang
        s.add(u)
        await s.commit()


async def lang_of(uid):
    async with db.Session() as s:
        return (await s.get(User, uid)).lang


async def buy_stars(h, uid, order_id, username):
    await h.click(uid, "stars", username=username); await h.click(uid, "self", username=username)
    await h.say(uid, "50", username=username); await h.click(uid, "pay:tr", username=username)
    await h.say(uid, photo=True, username=username)
    await h.click(ADMIN, f"adm:ok:o:{order_id}", chat_id=GROUP, username="admin")


# ───────── интерфейс ─────────
async def test_profile_shows_language_button_and_language_list(h):
    await h.click(100, "profile")
    btns = {b.callback_data: b for b in buttons(h.session.of(EditMessageMedia)[-1].reply_markup)}
    assert btns["lang"].text == "🌐 Язык"                   # название языка в кнопке профиля больше не показываем
    assert "Русский" not in btns["lang"].text and "Türkmençe" not in btns["lang"].text

    await h.click(100, "lang")
    langs = {b.callback_data: b.text for b in buttons(h.session.of(EditMessageMedia)[-1].reply_markup)}
    assert langs["lang:ru"] == "🇷🇺 Русский ✅"            # текущий отмечен галочкой в конце
    assert langs["lang:tk"] == "🇹🇲 Türkmençe"
    assert "profile" in langs                               # кнопка «Назад» в профиль


async def test_user_switches_language_and_it_persists(h):
    await h.click(100, "lang:tk")
    assert await lang_of(100) == "tk"
    assert h.session.of(AnswerCallbackQuery)[-1].text == "Dil üýtgedildi"
    assert "Siziň profiliňiz" in visible(h.session)[-3] or any("Siziň profiliňiz" in x for x in visible(h.session))

    await h.say(100, "/start")                              # главное меню уже на туркменском
    assert "Hoş geldiňiz" in visible(h.session)[-4] or any("Hoş geldiňiz" in x for x in visible(h.session))

    await h.say(200, "/start", username="other_user")      # другой пользователь остаётся на языке по умолчанию
    assert any("Добро пожаловать" in x for x in visible(h.session)[-6:])

    await h.click(100, "lang:ru")
    assert await lang_of(100) == "ru"


async def test_unknown_language_code_is_rejected(h):
    await h.click(100, "lang:xx")
    assert await lang_of(100) is None
    assert "устарела" in h.session.of(AnswerCallbackQuery)[-1].text


async def test_default_language_for_new_users_comes_from_bot_language(h):
    i18n.use("tk")                                          # BOT_LANGUAGE=tk
    await set_lang(300, "ru")                               # этот пользователь выбрал русский сам
    await h.say(400, "/start", username="newcomer")
    await h.say(300, "/start", username="chose_ru")
    shown = visible(h.session)
    assert any("Hoş geldiňiz" in x for x in shown[:3]) and any("Добро пожаловать" in x for x in shown[3:])


# ───────── уведомления идут на языке получателя ─────────
async def test_notifications_use_recipient_language_not_admin_language(h):
    await set_lang(100, "tk")                               # клиент 100: туркменский; клиент 101 и админ: русский
    await set_lang(101, "ru")
    await buy_stars(h, 100, 1, "client_tk")
    await buy_stars(h, 101, 2, "client_ru")

    assert any("üçin töleg tassyklandy" in x for x in sent_to(h, 100))          # решение админа -> клиенту на tk
    assert any("Оплата заказа #2 подтверждена" in x for x in sent_to(h, 101))   # а этому на ru

    for oid in (1, 2):                                      # воркер: выдача
        await fulfillment.process_order(h.bot, oid); await fulfillment.process_order(h.bot, oid)
    assert any("ýerine ýetirildi" in x for x in sent_to(h, 100))
    assert any("Заказ #2 выполнен" in x for x in sent_to(h, 101))

    cards = [c.caption for c in h.session.of(SendPhoto) if c.chat_id == GROUP]  # карточки в группе: язык админа
    assert cards and all("Платёж по заказу" in c for c in cards)


async def test_expiry_and_refund_notices_use_recipient_language(h):
    from datetime import timedelta
    await set_lang(100, "tk")
    await h.click(100, "stars", username="client_tk"); await h.click(100, "self", username="client_tk")
    await h.say(100, "50", username="client_tk"); await h.click(100, "pay:tr", username="client_tk")
    async with db.Session() as s:
        o = await s.get(Order, 1); o.expires_at = utcnow() - timedelta(minutes=1); await s.commit()
    await fulfillment.expire_unpaid(h.bot)
    assert any("töleg wagty gutardy" in x for x in sent_to(h, 100))

    async with db.Session() as s:                           # возврат при сбое: оплата с баланса и отказ Hex
        u = await s.get(User, 100); u.balance_cents = 100_000; await s.commit()
    deps.hexc.create_error = __import__("app.services.hex_client", fromlist=["HexError"]).HexError(409, "out_of_stock", "x")
    await h.click(100, "stars", username="client_tk"); await h.click(100, "self", username="client_tk")
    await h.say(100, "50", username="client_tk"); await h.click(100, "pay:bal", username="client_tk")
    oid = (await _last_order()).id
    await fulfillment.process_order(h.bot, oid)
    assert any("ýerine ýetirilmedi" in x and "gaýtaryldy" in x for x in sent_to(h, 100))


async def _last_order():
    async with db.Session() as s:
        return await s.scalar(select(Order).order_by(Order.id.desc()))


# ───────── контекст языка и миграция ─────────
def test_language_context_precedence():
    i18n.use("ru", "ru")
    assert i18n.t("btn.back") == "‹ Назад"
    with i18n.lang_ctx("tk"):
        assert i18n.t("btn.back") == "‹ Yza"                # язык пользователя из контекста
        assert i18n.t("adm.btn.approve") == "✅ Подтвердить"  # админские ключи его не слушают
        assert i18n.current_lang() == "tk"
    assert i18n.t("btn.back") == "‹ Назад"                  # после выхода из контекста — по умолчанию
    with i18n.lang_ctx("zz"):                               # неизвестный язык — язык по умолчанию
        assert i18n.t("btn.back") == "‹ Назад"


async def test_migration_adds_lang_column_to_existing_database(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)                             # «старая» схема без колонки lang
    con.execute("CREATE TABLE users (id BIGINT PRIMARY KEY, username VARCHAR(64), balance_cents BIGINT, "
                "is_banned BOOLEAN, created_at DATETIME)")
    con.execute("INSERT INTO users VALUES (5, 'old_user', 1234, 0, '2026-01-01 00:00:00')")
    con.commit(); con.close()

    await db.init_db(f"sqlite+aiosqlite:///{path}")
    async with db.Session() as s:
        u = await s.get(User, 5)
        assert u.balance_cents == 1234 and u.lang is None   # данные сохранены, язык по умолчанию
        u.lang = "tk"; await s.commit()
    await db.init_db(f"sqlite+aiosqlite:///{path}")         # повторный запуск не падает
    async with db.Session() as s:
        assert (await s.get(User, 5)).lang == "tk"
    await db.engine.dispose()


# ───────── название языка только внутри вкладки «Язык» ─────────
async def test_language_name_is_shown_only_inside_the_language_tab(h):
    await h.click(100, "profile")
    profile_kb = h.session.of(EditMessageMedia)[-1].reply_markup
    assert all("Русский" not in b.text and "Türkmençe" not in b.text for b in buttons(profile_kb))
    await h.click(100, "lang")
    media = h.session.of(EditMessageMedia)[-1]
    assert "Текущий язык: <b>🇷🇺 Русский</b>" in media.media.caption       # название — на экране вкладки
    await set_lang(100, "tk")
    await h.click(100, "lang")
    assert "Häzirki dil: <b>🇹🇲 Türkmençe</b>" in h.session.of(EditMessageMedia)[-1].media.caption


async def test_default_language_is_listed_first(h):
    i18n.use("tk")
    await h.click(100, "lang")
    order = [b.callback_data for b in buttons(h.session.of(EditMessageMedia)[-1].reply_markup)][:2]
    assert order == ["lang:tk", "lang:ru"]
    i18n.use("ru")
    await h.click(100, "lang")
    assert [b.callback_data for b in buttons(h.session.of(EditMessageMedia)[-1].reply_markup)][:2] == ["lang:ru", "lang:tk"]


# ───────── выбор языка при первом /start ─────────
@pytest.fixture
def ask(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "ask_language_on_start", True)


async def test_first_start_asks_for_language_then_shows_menu(h, ask):
    await h.say(500, "/start", username="brand_new")
    photo = h.session.of(SendPhoto)[-1]
    assert "Dil saýlaň / Выберите язык" in photo.caption                    # двуязычный экран
    kb = {b.callback_data: b.text for b in buttons(photo.reply_markup)}
    assert kb == {"langs:ru": "🇷🇺 Русский", "langs:tk": "🇹🇲 Türkmençe"}   # без «Назад» и без галочки
    assert await lang_of(500) is None

    await h.click(500, "langs:tk", username="brand_new")
    assert await lang_of(500) == "tk"
    assert h.session.of(AnswerCallbackQuery)[-1].text == "Dil üýtgedildi"
    assert "Hoş geldiňiz" in h.session.of(EditMessageMedia)[-1].media.caption   # сразу главное меню на выбранном языке

    n = len(h.session.calls)
    await h.say(500, "/start", username="brand_new")                          # второй /start: выбор больше не просят
    assert "Hoş geldiňiz" in h.session.of(SendPhoto)[-1].caption and len(h.session.calls) > n


async def test_first_start_picker_skipped_for_users_who_already_chose(h, ask):
    await set_lang(600, "ru")
    await h.say(600, "/start", username="chose_before")
    assert "Добро пожаловать" in h.session.of(SendPhoto)[-1].caption


async def test_picker_is_shown_again_for_removed_language_and_for_legacy_users(h, ask):
    await set_lang(601, "xx")                                                  # язык, которого больше нет в locales/
    await h.say(601, "/start", username="stale_lang")
    assert "Dil saýlaň / Выберите язык" in h.session.of(SendPhoto)[-1].caption
    await h.say(100, "/start")                                                 # клиент из базы со времён до выбора языка (lang=NULL)
    assert "Dil saýlaň / Выберите язык" in h.session.of(SendPhoto)[-1].caption


async def test_first_start_rejects_unknown_language_code(h, ask):
    await h.say(602, "/start", username="someone_x")
    await h.click(602, "langs:xx", username="someone_x")
    assert await lang_of(602) is None
    assert "устарела" in h.session.of(AnswerCallbackQuery)[-1].text


async def test_picker_can_be_switched_off(h, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "ask_language_on_start", False)
    await h.say(603, "/start", username="no_picker")
    assert "Добро пожаловать" in h.session.of(SendPhoto)[-1].caption


# ───────── премиум-эмодзи языка и флагов ─────────
async def test_premium_emoji_for_language_icon_and_flags(env):
    deps.catalog = Catalog(deps.hexc)
    hp = Harness(premium=True)
    await hp.click(100, "profile")
    prof = {b.callback_data: b for b in buttons(hp.session.of(EditMessageMedia)[-1].reply_markup)}
    assert prof["lang"].text == "Язык" and prof["lang"].icon_custom_emoji_id == "5879585266426973039"   # 🌐

    await hp.click(100, "lang")
    media = hp.session.of(EditMessageMedia)[-1]
    assert '<tg-emoji emoji-id="5879585266426973039">🌐</tg-emoji>' in media.media.caption           # 🌐 в заголовке
    assert '<tg-emoji emoji-id="5291734595862018096">🇷🇺</tg-emoji>' in media.media.caption           # флаг в тексте
    langs = {b.callback_data: b for b in buttons(media.reply_markup)}
    assert langs["lang:ru"].icon_custom_emoji_id == "5291734595862018096" and langs["lang:ru"].text == "Русский ✅"
    assert langs["lang:tk"].icon_custom_emoji_id == "5292055356904585500" and langs["lang:tk"].text == "Türkmençe"


async def test_premium_emoji_on_first_start_picker(env, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "ask_language_on_start", True)
    deps.catalog = Catalog(deps.hexc)
    hp = Harness(premium=True)
    await hp.say(700, "/start", username="fresh_user")
    photo = hp.session.of(SendPhoto)[-1]
    assert '<tg-emoji emoji-id="5879585266426973039">🌐</tg-emoji>' in photo.caption
    langs = {b.callback_data: b for b in buttons(photo.reply_markup)}
    assert langs["langs:ru"].icon_custom_emoji_id == "5291734595862018096" and langs["langs:ru"].text == "Русский"
    assert langs["langs:tk"].icon_custom_emoji_id == "5292055356904585500" and langs["langs:tk"].text == "Türkmençe"
