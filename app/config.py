from __future__ import annotations

import re
from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    bot_name: str = "Stars Bot"
    admin_ids: str = ""
    admin_group_id: int
    support_url: str = ""
    premium_emoji: bool = True

    database_url: str

    hex_api_key: str
    hex_base_url: str = "https://hexpay.live/api/reseller/v1"

    pay_numbers: str = ""
    pay_note: str = ""

    default_rate_tmt_per_rub: Decimal = Decimal("0.045")
    default_markup_percent: Decimal = Decimal("10")
    price_guard_percent: Decimal = Decimal("3")

    payment_ttl_minutes: int = 30
    max_proof_attempts: int = 3
    stars_min: int = 50
    stars_max: int = 100_000
    topup_min_tmt: Decimal = Decimal("10")
    topup_max_tmt: Decimal = Decimal("2000")
    hex_wait_hours: int = 6
    hex_low_balance_rub: Decimal = Decimal("2000")

    worker_interval_sec: int = 5
    poll_min_interval_sec: int = 8

    @property
    def admin_id_list(self) -> list[int]:
        return [int(x) for x in self.admin_ids.replace(" ", "").split(",") if x]

    @property
    def support_link(self) -> str:
        """Прямая ссылка для URL-кнопки «Поддержка» (пусто — кнопка скрыта)."""
        v = self.support_url.strip()
        if not v:
            return ""
        if v.startswith("@"):
            v = v[1:]
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", v):
            return f"https://t.me/{v}"
        if re.match(r"^(https?://|tg://)\S+$", v):
            return v
        return ""

    @property
    def pay_number_list(self) -> list[str]:
        return [x.strip() for x in self.pay_numbers.split(",") if x.strip()]

    @property
    def async_database_url(self) -> str:
        url = self.database_url
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
