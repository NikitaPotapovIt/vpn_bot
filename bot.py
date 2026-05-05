import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode

from config import TELEGRAM_TOKEN, ADMIN_IDS
from database import init_db
from scheduler import init_scheduler
from handlers.admin import router as admin_router
from handlers.client import router as client_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

async def main():
    # Инициализация БД
    await init_db()
    logger.info("Database initialized")
    
    # Создание бота
    bot = Bot(token=TELEGRAM_TOKEN, parse_mode=ParseMode.HTML)
    dp = Dispatcher()
    
    # Подключение роутеров
    dp.include_router(client_router)
    dp.include_router(admin_router)
    
    # Запуск планировщика
    scheduler = init_scheduler(bot)
    
    # Уведомить администратора о запуске
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, "🤖 <b>VPN Bot запущен!</b>\n\nКоманды:\n"
                "/clients — список клиентов\n"
                "/add_client — добавить клиента\n"
                "/servers — статус всех серверов\n"
                "/server <имя> — детально по серверу\n"
                "/client <id> — карточка клиента")
        except Exception:
            pass
    
    logger.info("Starting polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    