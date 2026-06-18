#!/usr/bin/env python3
"""
TaskBot — точка входа.
Запуск: python main.py
"""
import asyncio
import logging
import os
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("main")


async def run():
    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.error("❌ BOT_TOKEN не найден в .env!")
        logger.error("   Создай файл .env и добавь: BOT_TOKEN=твой_токен")
        return

    from database import Database
    from scheduler import TaskScheduler
    from bot import build_app
    from telegram import Update

    db = Database("tasks.db")
    app = build_app(token)
    sched = TaskScheduler(app.bot, db)

    # Inject scheduler into bot module
    import bot as bot_module
    bot_module.scheduler = sched

    async with app:
        await sched.start()

        # Re-schedule all pending tasks after restart
        pending = db.get_all_pending()
        for task in pending:
            sched.schedule_task(task)
        logger.info(f"♻️  Восстановлено {len(pending)} задач из БД.")

        logger.info("🤖 TaskBot запущен и ждёт сообщений…")
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

        # Keep running
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            await app.updater.stop()
            await app.stop()
            logger.info("🛑 Бот остановлен.")


if __name__ == "__main__":
    asyncio.run(run())
