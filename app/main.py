import asyncio
import logging
import os

from alembic import command
from alembic.config import Config
from sqlalchemy.exc import OperationalError

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.router import setup_router
from app.config import settings
from app.db.seed import seed_reference_data
from app.services.scheduler import delivery_notifier
from app.db.session import SessionLocal


def _run_migrations() -> None:
    config_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    alembic_cfg = Config(config_path)
    command.upgrade(alembic_cfg, "head")


async def _run_migrations_with_retry() -> None:
    for attempt in range(10):
        try:
            await asyncio.to_thread(_run_migrations)
            return
        except OperationalError:
            if attempt == 9:
                raise
            await asyncio.sleep(2)


async def on_startup(bot: Bot) -> None:
    await _run_migrations_with_retry()
    async with SessionLocal() as session:
        await seed_reference_data(session)
    if settings.approval_override_tg_id:
        try:
            await bot.send_message(
                settings.approval_override_tg_id,
                "Тест: бот запущен, согласования будут приходить сюда.",
            )
        except Exception:
            logging.exception("Failed to send override startup message")
    asyncio.create_task(delivery_notifier(bot))


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(setup_router())
    dp.startup.register(on_startup)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
