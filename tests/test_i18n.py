import ast
import glob
import json
import re
from pathlib import Path

import pytest
from aiogram.methods import AnswerCallbackQuery

from app import db, deps, i18n
from app.db import Order, User
from app.services import fulfillment
from app.services.pricing import Catalog
from test_handlers_smoke import GROUP, Harness

CYR = re.compile("[А-Яа-яЁё]")
ROOT = Path(__file__).resolve().parent.parent
PREFIXES = ("btn.", "status.", "order.", "step.", "pay.", "req.", "review.", "warn.", "alert.", "paid.", "balance.",
            "topup.", "profile.", "history.", "cancel.", "proof.", "notify.", "ban.", "fulfil.", "expire.", "err.",
            "adm.", "main.", "months.", "lang.")
DYNAMIC = ("months.", "err.", "status.", "adm.status.")   # ключи собираются из переменных


def code_keys() -> set[str]:
    keys = set()
    for f in glob.glob(str(ROOT / "app" / "**" / "*.py"), recursive=True):
        for node in ast.walk(ast.parse(open(f, encoding="utf-8").read())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(r"[a-z_]+(\.[a-z0-9_]+)+", node.value):
                if node.value.startswith(PREFIXES):
                    keys.add(node.value)
    return keys


# ───────── файлы переводов ─────────
def test_locale_files_are_consistent():
    assert set(i18n.available_languages()) >= {"ru", "tk"}
    assert i18n.check_locales() == []                       # те же ключи и плейсхолдеры, что в ru
    for lang in ("ru", "tk"):
        data = json.loads((ROOT / "locales" / f"{lang}.json").read_text(encoding="utf-8"))
        assert all(isinstance(v, str) and v.strip() for v in data.values())


def test_every_key_used_in_code_exists_and_none_are_orphaned():
    ru = json.loads((ROOT / "locales" / "ru.json").read_text(encoding="utf-8"))
    used = code_keys()
    assert not (used - ru.keys()), f"в коде есть ключи без перевода: {sorted(used - ru.keys())}"
    unused = {k for k in ru if k not in used and not k.startswith(DYNAMIC)}
    assert not unused, f"ключи не используются в коде: {sorted(unused)}"


def test_turkmen_uses_turkmen_alphabet_not_cyrillic():
    tk = json.loads((ROOT / "locales" / "tk.json").read_text(encoding="utf-8"))
    assert not [k for k, v in tk.items() if CYR.search(v)]


# ───────── поведение i18n ─────────
def test_missing_key_and_unknown_language_fall_back_to_russian():
    i18n.use("tk")
    assert i18n.t("btn.back") == "‹ Yza"
    i18n._cache["tk"] = {k: v for k, v in i18n._load("tk").items() if k != "btn.back"}
    assert i18n.t("btn.back") == "‹ Назад"                    # нет в tk — берём ru
    assert i18n.t("no.such.key") == "no.such.key"
    i18n.use("xx")
    assert i18n.t("btn.back") == "‹ Назад"                    # неизвестный язык — русский


def test_admin_language_is_independent():
    i18n.use("tk", "ru")
    assert i18n.t("btn.back") == "‹ Yza" and i18n.t("adm.btn.approve") == "✅ Подтвердить"
    i18n.use("tk")                                           # admin по умолчанию = язык клиентов
    assert i18n.t("adm.btn.approve") == "✅ Tassykla"


# ───────── исправление: меню не отвечает на любой текст ─────────
@pytest.fixture
def h(env):
    deps.catalog = Catalog(deps.hexc)
    return Harness()


async def test_random_text_is_ignored_and_start_still_works(h):
    n = len(h.session.calls)
    await h.say(100, "привет")
    await h.say(100, "hello")
    await h.say(100, "12345")
    assert len(h.session.calls) == n                          # бот не отвечает и ничего не удаляет
    await h.say(100, "/start")
    assert len(h.session.calls) > n


async def test_stale_button_gets_an_answer_instead_of_spinner(h):
    await h.click(100, "support")                             # старая callback-кнопка из прежнего меню
    answers = h.session.of(AnswerCallbackQuery)
    assert answers and "устарела" in answers[-1].text


# ───────── полный путь на туркменском: ни одного русского слова ─────────
def visible(session) -> list[str]:
    out = []
    for c in session.calls:
        for attr in ("text", "caption"):
            v = getattr(c, attr, None)
            if isinstance(v, str):
                out.append(v)
        media = getattr(c, "media", None)
        if media is not None and getattr(media, "caption", None):
            out.append(media.caption)
        rm = getattr(c, "reply_markup", None)
        if rm is not None and hasattr(rm, "inline_keyboard"):
            out += [b.text for row in rm.inline_keyboard for b in row]
    return out


async def newest_order() -> Order:
    async with db.Session() as s:
        from sqlalchemy import select
        return await s.scalar(select(Order).order_by(Order.id.desc()))


async def test_turkmen_journey_has_no_russian_text(env):
    hx, _ = env
    deps.catalog = Catalog(hx)
    i18n.use("tk")                                            # клиенты и админы — туркменский
    h = Harness(premium=True)                                 # заодно проверяем премиум-эмодзи на tk-текстах
    u, adm = 100, 1

    await h.say(u, "/start")
    assert "hoş geldiňiz" in visible(h.session)[0].lower()

    # звёзды: ошибки ввода, оплата переводом, отклонение, подтверждение, выдача
    await h.click(u, "stars"); await h.say(u, "nobody"); await h.say(u, "durov"); await h.say(u, "10"); await h.say(u, "100")
    await h.click(u, "pay:bal")                               # на балансе пусто
    await h.click(u, "pay:tr"); await h.click(u, "stars")     # повторный заказ при неоплаченном
    await h.say(u, photo=True)
    await h.say(u, photo=True)                                # уже на проверке
    await h.click(adm, "adm:no:o:1", chat_id=GROUP, username="admin")
    await h.say(u, photo=True)
    await h.click(adm, "adm:ok:o:1", chat_id=GROUP, username="admin")
    await fulfillment.process_order(h.bot, 1); await fulfillment.process_order(h.bot, 1)
    assert (await newest_order()).status == "done"

    # premium: отмена клиентом
    await h.click(u, "premium"); await h.click(u, "self"); await h.click(u, "prem:3"); await h.click(u, "pay:tr")
    await h.click(u, "cancel:o:2")

    # пополнение баланса
    await h.click(u, "balance"); await h.click(u, "topup"); await h.say(u, "5"); await h.say(u, "50")
    await h.say(u, photo=True)
    await h.click(adm, "adm:ok:t:1", chat_id=GROUP, username="admin")

    # оплата с баланса: сбой доставки -> возврат; рост цены -> решение админа; пустой депозит -> ожидание
    async with db.Session() as s:
        usr = await s.get(User, u); usr.balance_cents = 1_000_000; await s.commit()
    await h.click(u, "premium"); await h.click(u, "self"); await h.click(u, "prem:6"); await h.click(u, "pay:bal")
    oid = (await newest_order()).id
    await fulfillment.process_order(h.bot, oid)
    hx.next_status, hx.next_error_code = "failed", "out_of_stock"
    await fulfillment.process_order(h.bot, oid)
    hx.next_status, hx.next_error_code = "delivered", None

    hx.total_nano = 10_000_000                                # цена выросла
    for verdict in ("pr:ok", "pr:no"):
        await h.click(u, "stars"); await h.click(u, "self"); await h.say(u, "50"); await h.click(u, "pay:bal")
        oid = (await newest_order()).id
        await fulfillment.process_order(h.bot, oid)
        await h.click(adm, f"{verdict}:{oid}", username="admin")
    hx.total_nano, hx.balance_nano = 100_000, 0               # пустой депозит
    await h.click(u, "stars"); await h.click(u, "self"); await h.say(u, "50"); await h.click(u, "pay:bal")
    await fulfillment.process_order(h.bot, (await newest_order()).id)
    await fulfillment.check_deposit(h.bot, {})

    # истечение времени оплаты
    from datetime import timedelta
    from app.db import utcnow
    await h.click(u, "stars"); await h.click(u, "self"); await h.say(u, "50"); await h.click(u, "pay:tr")
    async with db.Session() as s:
        o = await s.get(Order, (await newest_order()).id); o.expires_at = utcnow() - timedelta(minutes=1); await s.commit()
    await fulfillment.expire_unpaid(h.bot)

    # профиль, история, админ-команды, бан
    await h.click(u, "profile"); await h.click(u, "history")
    for cmd in ("/admin", "/rate 0.05", "/rate x", "/markup 12", "/balance_hex", "/stats", "/orders", "/credit 100 5",
                "/credit", "/ban 100", "/ban"):
        await h.say(adm, cmd, username="admin")
    await h.say(u, "/start")                                  # забанен
    await h.click(u, "stars")
    await h.say(adm, "/unban 100", username="admin")

    texts = visible(h.session)
    leaks = [x for x in texts if CYR.search(x)]
    assert not leaks, f"русский текст в туркменском режиме: {leaks[:5]}"
    joined = "\n".join(texts)
    for marker in ("Sargyt", "Töleg", "Ýyldyzlar", "Premium", "Balans", "Goldaw", "Statistika", "Girmek çäklendirilen"):
        assert marker in joined, marker
