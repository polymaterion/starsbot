from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app import db, deps, i18n
from app.emoji import PremiumEmojiMiddleware
from app.config import get_settings
from app.handlers import admin, flow, menu, payments
from app.middleware import UserMiddleware
from app.services import fulfillment
from app.services.hex_client import HexClient
from app.services.pricing import Catalog

log = logging.getLogger("app")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # не светим URL с данными в логах
    cfg = get_settings()

    langs = i18n.available_languages()
    for lang in {cfg.bot_lang, cfg.admin_lang}:
        if lang not in langs:
            log.error("Язык %r не найден в %s (есть: %s). Используется %s.", lang, i18n.LOCALES_DIR, ", ".join(langs), i18n.BASE_LANG)
    for problem in i18n.check_locales():
        log.warning("locales: %s", problem)
    log.info("language: клиенты=%s, админы=%s", cfg.bot_lang, cfg.admin_lang)

    await db.init_db(cfg.async_database_url)
    deps.hexc = HexClient(cfg.hex_api_key, cfg.hex_base_url)
    deps.catalog = Catalog(deps.hexc)

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    if cfg.premium_emoji:
        bot.session.middleware(PremiumEmojiMiddleware())
    if not cfg.support_link:
        log.warning("SUPPORT_URL пуст или некорректен: кнопка «Поддержка» скрыта")
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(UserMiddleware())
    dp.callback_query.outer_middleware(UserMiddleware())
    # порядок важен: команды админа и платежи раньше общих текстовых обработчиков
    dp.include_routers(admin.router, payments.router, flow.router, menu.router)

    worker = asyncio.create_task(fulfillment.run(bot))
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        log.info("bot started")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        worker.cancel()
        await deps.hexc.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
