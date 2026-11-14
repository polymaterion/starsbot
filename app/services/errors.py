"""Человекочитаемые причины для клиента по error_code Hex и нашим внутренним кодам."""
from __future__ import annotations

_MESSAGES = {
    "recipient_invalid": "Получатель не найден или не может принять этот товар.",
    "out_of_stock": "Товар временно закончился у поставщика.",
    "product_unavailable": "Товар временно недоступен.",
    "invalid_order_data": "Данные заказа не прошли проверку.",
    "invalid_quantity": "Недопустимое количество.",
    "delivery_rejected": "Доставка отклонена.",
    "delivery_failed": "Доставка не удалась.",
    "temporary_failure": "Временный сбой у поставщика.",
    "order_cancelled": "Заказ был отменён.",
    "wait_timeout": "Не удалось выполнить заказ в разумный срок.",
    "price_changed": "Цена у поставщика изменилась.",
    "price_declined": "Цена у поставщика заметно выросла, и заказ не может быть выполнен по прежней стоимости.",
}
_DEFAULT = "Не удалось выполнить заказ."


def reason_text(code: str | None) -> str:
    return _MESSAGES.get(code or "", _DEFAULT)
