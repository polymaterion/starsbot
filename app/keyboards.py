from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import get_settings
from app.texts import MONTHS, money


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
        [("⭐ Звезды", "stars"), ("🎁 Premium", "premium")],
        [("👛 Баланс", "balance"), ("👤 Профиль", "profile")],
    ]
    link = get_settings().support_link
    if link:  # URL-кнопка с прямой ссылкой из SUPPORT_URL
        rows.append([("💬 Поддержка", f"url:{link}")])
    return _kb(rows)


def back(to: str = "menu") -> InlineKeyboardMarkup:
    return _kb([[("‹ Назад", to)]])


def step1() -> InlineKeyboardMarkup:
    return _kb([[("👤 Купить для себя", "self")], [("‹ Назад", "back")]])


def step_back() -> InlineKeyboardMarkup:
    return _kb([[("‹ Назад", "back")]])


def premium_periods(prices: dict[int, int]) -> InlineKeyboardMarkup:
    rows = [[(f"На {MONTHS[m].replace('1 год', '1 год')} · {money(c)} TMT", f"prem:{m}")] for m, c in prices.items()]
    rows.append([("‹ Назад", "back")])
    return _kb(rows)


def pay_methods(balance_cents: int) -> InlineKeyboardMarkup:
    return _kb([
        [("💳 Перевод с баланса оператора", "pay:tr")],
        [(f"👛 С баланса бота · {money(balance_cents)} TMT", "pay:bal")],
        [("‹ Назад", "back")],
    ])


def cancel_payment(kind: str, item_id: int) -> InlineKeyboardMarkup:
    return _kb([[("🚫 Отменить", f"cancel:{kind}:{item_id}")], [("‹ В меню", "menu")]])


def waiting_check() -> InlineKeyboardMarkup:
    return _kb([[("‹ В меню", "menu")]])


def balance_menu() -> InlineKeyboardMarkup:
    return _kb([[("➕ Пополнить баланс", "topup")], [("‹ Назад", "menu")]])


def profile_menu(history_count: int) -> InlineKeyboardMarkup:
    return _kb([[(f"🧾 История покупок ({history_count})", "history")], [("‹ Назад", "menu")]])




def admin_payment(kind: str, item_id: int) -> InlineKeyboardMarkup:
    return _kb([[("✅ Подтвердить", f"adm:ok:{kind}:{item_id}"), ("❌ Отклонить", f"adm:no:{kind}:{item_id}")]])


def price_review(order_id: int) -> InlineKeyboardMarkup:
    return _kb([[("✅ Выполнить по новой цене", f"pr:ok:{order_id}")],
                [("↩️ Отменить и вернуть деньги", f"pr:no:{order_id}")]])
