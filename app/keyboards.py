from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import i18n
from app.config import get_settings
from app.i18n import t
from app.texts import money, months_label


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """(text, callback_data) или (text, 'url:https://...')."""
    out = []
    for row in rows:
        btns = []
        for text, data in row:
            if data.startswith("url:"):
                btns.append(InlineKeyboardButton(text=text, url=data[4:]))
            else:
                btns.append(InlineKeyboardButton(text=text, callback_data=data))
        out.append(btns)
    return InlineKeyboardMarkup(inline_keyboard=out)


def main() -> InlineKeyboardMarkup:
    rows = [
        [(t("btn.stars"), "stars"), (t("btn.premium"), "premium")],
        [(t("btn.balance"), "balance"), (t("btn.profile"), "profile")],
    ]
    link = get_settings().support_link
    if link:  # URL-кнопка с прямой ссылкой из SUPPORT_URL
        rows.append([(t("btn.support"), f"url:{link}")])
    return _kb(rows)


def back(to: str = "menu") -> InlineKeyboardMarkup:
    return _kb([[(t("btn.back"), to)]])


def step1() -> InlineKeyboardMarkup:
    return _kb([[(t("btn.self"), "self")], [(t("btn.back"), "back")]])


def step_back() -> InlineKeyboardMarkup:
    return _kb([[(t("btn.back"), "back")]])


def premium_periods(prices: dict[int, int]) -> InlineKeyboardMarkup:
    rows = [[(t("btn.period", period=months_label(m), price=money(c)), f"prem:{m}")] for m, c in prices.items()]
    rows.append([(t("btn.back"), "back")])
    return _kb(rows)


def pay_methods(balance_cents: int) -> InlineKeyboardMarkup:
    return _kb([
        [(t("btn.pay_transfer"), "pay:tr")],
        [(t("btn.pay_balance", amount=money(balance_cents)), "pay:bal")],
        [(t("btn.back"), "back")],
    ])


def cancel_payment(kind: str, item_id: int) -> InlineKeyboardMarkup:
    return _kb([[(t("btn.cancel"), f"cancel:{kind}:{item_id}")], [(t("btn.to_menu"), "menu")]])


def waiting_check() -> InlineKeyboardMarkup:
    return _kb([[(t("btn.to_menu"), "menu")]])


def balance_menu() -> InlineKeyboardMarkup:
    return _kb([[(t("btn.topup"), "topup")], [(t("btn.back"), "menu")]])


def profile_menu(history_count: int) -> InlineKeyboardMarkup:
    return _kb([[(t("btn.history", n=history_count), "history")], [(t("btn.language"), "lang")],
                [(t("btn.back"), "menu")]])


def language_menu(first: bool = False) -> InlineKeyboardMarkup:
    """Список языков из locales/. first=True: экран первого /start (без «Назад», после выбора откроется меню);
    иначе вкладка «Язык» в профиле, текущий язык отмечен галочкой."""
    cur = i18n.current_lang()
    prefix = "langs" if first else "lang"
    rows = [[(i18n.language_name(code) + (" ✅" if not first and code == cur else ""), f"{prefix}:{code}")]
            for code in i18n.ordered_languages()]
    if not first:
        rows.append([(t("btn.back"), "profile")])
    return _kb(rows)


def admin_payment(kind: str, item_id: int) -> InlineKeyboardMarkup:
    return _kb([[(t("adm.btn.approve"), f"adm:ok:{kind}:{item_id}"),
                 (t("adm.btn.reject"), f"adm:no:{kind}:{item_id}")]])


def price_review(order_id: int) -> InlineKeyboardMarkup:
    return _kb([[(t("adm.btn.price_ok"), f"pr:ok:{order_id}")],
                [(t("adm.btn.price_no"), f"pr:no:{order_id}")]])
