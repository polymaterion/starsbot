"""Причины неудачи для клиента по error_code Hex и нашим внутренним кодам (тексты: locales, ключи err.*)."""
from __future__ import annotations

from app.i18n import t

_KNOWN = {
    "recipient_invalid", "out_of_stock", "product_unavailable", "invalid_order_data", "invalid_quantity",
    "delivery_rejected", "delivery_failed", "temporary_failure", "order_cancelled", "wait_timeout",
    "price_changed", "price_declined",
}


def reason_text(code: str | None) -> str:
    return t(f"err.{code}") if code in _KNOWN else t("err.default")
