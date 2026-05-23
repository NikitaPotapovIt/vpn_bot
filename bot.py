import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from config import TELEGRAM_TOKEN, ADMIN_IDS
from database import init_db, get_user_lang
from scheduler import init_scheduler
from handlers.admin import router as admin_router
from handlers.client import router as client_router
from i18n import tr, normalize_lang

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

async def main():
    # Initialize DB
    await init_db()
    logger.info("Database initialized")
    
    # Create bot
    bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    
    # Attach routers
    dp.include_router(client_router)
    dp.include_router(admin_router)
    
    # Start scheduler
    scheduler = init_scheduler(bot)
    
    # Notify admins on startup
    for admin_id in ADMIN_IDS:
        try:
            lang = normalize_lang(await get_user_lang(admin_id))
            await bot.send_message(admin_id, tr(lang, "bot_started"))
        except Exception:
            pass
    
    logger.info("Starting polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
