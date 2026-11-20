from __future__ import annotations

import html
from decimal import Decimal

from app.config import get_settings

MONTHS = {3: "3 месяца", 6: "6 месяцев", 12: "1 год"}

STATUS_RU = {
    "awaiting_payment": "⏳ Ожидает оплаты",
    "proof_sent": "🔎 Платёж на проверке",
    "approved": "⚙️ В работе",
    "hex_waiting": "⚙️ В работе",
    "price_review": "⚙️ В работе",
    "delivering": "🚚 Доставляется",
    "done": "✅ Выполнен",
    "failed": "❌ Не выполнен, деньги на балансе",
    "cancelled": "🚫 Отменён",
    "rejected": "🚫 Платёж отклонён",
}


def money(cents: int) -> str:
    return f"{Decimal(cents) / 100:.2f}"


def esc(x) -> str:
    return html.escape(str(x))


def tree(lines: list[str]) -> str:
    if len(lines) == 1:
        return "└ " + lines[0]
    out = []
    for i, line in enumerate(lines):
        out.append(("┌ " if i == 0 else "└ " if i == len(lines) - 1 else "├ ") + line)
    return "\n".join(out)


def order_lines(kind: str, recipient: str | None, qty: int | None, months: int | None) -> list[str]:
    r = f"@{esc(recipient)}" if recipient else "Не выбран"
    if kind == "stars":
        return ["Тип: <b>Telegram Stars</b>", f"Получатель: <b>{r}</b>",
                f"Количество: <b>{qty if qty else 'Не выбрано'}</b>"]
    return ["Тип: <b>Telegram Premium</b>", f"Получатель: <b>{r}</b>",
            f"Период: <b>{MONTHS.get(months, 'Не выбран') if months else 'Не выбран'}</b>"]


def head(kind: str, step: int) -> str:
    icon = "⭐" if kind == "stars" else "🎁"
    return f"{icon} <b>Оформление заявки [{step}/3]</b>"


def main_menu() -> str:
    return (
        f"👋 <b>Добро пожаловать в {esc(get_settings().bot_name)}!</b>\n\n"
        "Через нашего бота вы можете купить ⭐ Звезды и 🎁 Premium по низким ценам. "
        "Оплата в манатах переводом с баланса мобильного оператора.\n\n"
        "⬇️ Выберите нужный раздел в меню ниже"
    )


def step(kind: str, n: int, recipient=None, qty=None, months=None, prompt: str = "", note: str = "") -> str:
    parts = [head(kind, n), "", tree(order_lines(kind, recipient, qty, months))]
    if note:
        parts += ["", note]
    if prompt:
        parts += ["", prompt]
    return "\n".join(parts)


def payment_methods(kind, recipient, qty, months, base_cents: int, note: str = "") -> str:
    parts = [head(kind, 3), "", tree(order_lines(kind, recipient, qty, months)), "",
             f"💰 Стоимость: <b>{money(base_cents)} TMT</b>",
             "<i>Итоговая сумма перевода может отличаться на несколько копеек — "
             "по ней мы опознаём ваш платёж.</i>"]
    if note:
        parts += ["", note]
    parts += ["", "💳 Выберите способ оплаты:"]
    return "\n".join(parts)


def requisites(title: str, total_cents: int, number: str | None, ttl_min: int, prefix: str = "") -> str:
    cfg = get_settings()
    parts = []
    if prefix:
        parts += [prefix, ""]
    parts += [
        f"💳 <b>{title}</b>", "",
        f"Переведите ровно <b>{money(total_cents)} TMT</b> с баланса мобильного оператора на номер:",
        f"<code>{esc(number) if number else 'номер не настроен'}</code>",
    ]
    if cfg.pay_note:
        parts.append(esc(cfg.pay_note))
    parts += [
        "", "⚠️ Сумма должна совпадать <b>до копейки</b>.",
        "После перевода отправьте <b>скриншот</b> (чек или SMS) в этот чат.",
        f"⏱ Платёж действителен {ttl_min} мин.",
    ]
    return "\n".join(parts)


def profile(user_id: int, username: str | None, balance: int, total: int, stars: int, months: int, reg: str) -> str:
    return (
        "👤 <b>Ваш профиль</b>\n\n"
        f"Баланс: <b>{money(balance)} TMT</b>\n"
        f"Telegram ID: <code>{user_id}</code>\n"
        f"Username: {'@' + esc(username) if username else '—'}\n\n"
        "📊 <b>Статистика покупок:</b>\n"
        f"{tree([f'Всего покупок: {total}', f'Звёзд куплено: {stars}', f'Месяцев Premium: {months}'])}\n\n"
        f"📅 Регистрация: {reg}"
    )


def balance(cents: int) -> str:
    return f"👛 <b>Баланс в боте</b>\n\nНа счету: <b>{money(cents)} TMT</b>\n\nБаланс можно тратить на заказы без подтверждения."
