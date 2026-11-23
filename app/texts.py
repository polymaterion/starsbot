"""Сборка экранов. Все слова берутся из locales/*.json через i18n.t()."""
from __future__ import annotations

import html
from decimal import Decimal

from app.config import get_settings
from app.i18n import t


def months_label(m: int) -> str:
    return t(f"months.{m}")


def status(s: str, admin: bool = False) -> str:
    """Подпись статуса заказа (admin=True — на языке админа)."""
    key = ("adm." if admin else "") + f"status.{s}"
    label = t(key)
    return s if label == key else label


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
    r = t("order.recipient", value=f"@{esc(recipient)}" if recipient else t("order.recipient.none"))
    if kind == "stars":
        return [t("order.type.stars"), r,
                t("order.quantity", value=qty if qty else t("order.quantity.none"))]
    return [t("order.type.premium"), r,
            t("order.period", value=months_label(months) if months else t("order.period.none"))]


def head(kind: str, step: int) -> str:
    icon = "⭐" if kind == "stars" else "🎁"
    return t("order.head", icon=icon, step=step)


def main_menu() -> str:
    return t("main.welcome", bot_name=esc(get_settings().bot_name))


def step(kind: str, n: int, recipient=None, qty=None, months=None, prompt: str = "", note: str = "") -> str:
    parts = [head(kind, n), "", tree(order_lines(kind, recipient, qty, months))]
    if note:
        parts += ["", note]
    if prompt:
        parts += ["", prompt]
    return "\n".join(parts)


def payment_methods(kind, recipient, qty, months, base_cents: int, note: str = "") -> str:
    parts = [head(kind, 3), "", tree(order_lines(kind, recipient, qty, months)), "",
             t("pay.cost", price=money(base_cents)), t("pay.tail_note")]
    if note:
        parts += ["", note]
    parts += ["", t("pay.choose")]
    return "\n".join(parts)


def requisites(title: str, total_cents: int, number: str | None, ttl_min: int, prefix: str = "") -> str:
    cfg = get_settings()
    parts = []
    if prefix:
        parts += [prefix, ""]
    parts += [
        f"💳 <b>{title}</b>", "",
        t("req.transfer", amount=money(total_cents)),
        f"<code>{esc(number) if number else t('req.no_number')}</code>",
    ]
    if cfg.pay_note:
        parts.append(esc(cfg.pay_note))
    parts += ["", t("req.exact"), t("req.send_proof"), t("req.ttl", ttl=ttl_min)]
    return "\n".join(parts)


def profile(user_id: int, username: str | None, balance: int, total: int, stars: int, months: int, reg: str) -> str:
    stats = tree([t("profile.stat.total", n=total), t("profile.stat.stars", n=stars),
                  t("profile.stat.months", n=months)])
    return t("profile.screen", balance=money(balance), id=user_id,
             username="@" + esc(username) if username else "—", stats=stats, reg=reg)


def balance(cents: int) -> str:
    return t("balance.screen", amount=money(cents))
