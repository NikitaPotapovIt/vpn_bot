"""
Планировщик напоминаний об оплате.
Логика:
  День 1 (1-е число)    → напоминание всем активным клиентам
  День 2 (2-е число)    → повторное напоминание неоплатившим
  День 3 (3-е число)    → "оплатите или отключим через 5 дней"
  Дни 4–7               → ежедневные напоминания с обратным отсчётом
  День 8 (=3+5)         → автоотключение (disable_peer)
  
  Если клиент нажал "оплатил" → статус "waiting_confirm"
  Если ты подтвердил → статус "paid", напоминания прекращаются
  Если ты отклонил → статус обратно "pending", клиент получает уведомление
"""

import asyncio
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from database import (
    get_active_clients, update_payment_status, increment_reminder_day,
    set_disconnect_date, set_client_active, log_payment, reset_monthly_payments
)
from ssh_manager import disable_peer
from config import ADMIN_IDS

logger = logging.getLogger(__name__)

# Будет установлен при инициализации
_bot = None

def init_scheduler(bot) -> AsyncIOScheduler:
    global _bot
    _bot = bot
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    
    # 1-го числа каждого месяца в 10:00 — сброс и первое напоминание
    scheduler.add_job(monthly_reset_and_notify, CronTrigger(day=1, hour=10, minute=0))
    
    # Каждый день в 10:00 — проверка неоплатившх
    scheduler.add_job(daily_reminder_check, CronTrigger(hour=10, minute=0))
    
    # Каждый день в 09:00 — проверка клиентов на отключение
    scheduler.add_job(disconnect_check, CronTrigger(hour=9, minute=0))
    
    scheduler.start()
    logger.info("Scheduler started")
    return scheduler

async def monthly_reset_and_notify():
    """1-е число: сбрасываем статусы и шлём напоминания"""
    await reset_monthly_payments()
    clients = await get_active_clients()
    for client in clients:
        try:
            await _bot.send_message(
                client.telegram_id,
                f"👋 <b>Привет, {client.name}!</b>\n\n"
                f"Наступило 1-е число — время оплаты VPN.\n"
                f"💰 Сумма: <b>{client.monthly_fee:.0f} ₽</b> (устройств: {client.devices})\n"
                f"🖥 Сервер: {client.server_name}\n\n"
                f"Нажми кнопку ниже, когда переведёшь оплату:",
                parse_mode="HTML",
                reply_markup=_paid_button(client.id)
            )
        except Exception as e:
            logger.error(f"Failed to notify {client.name}: {e}")
    
    # Уведомить администратора
    for admin_id in ADMIN_IDS:
        try:
            await _bot.send_message(
                admin_id,
                f"📅 <b>Начало расчётного периода</b>\n"
                f"Разослано напоминаний: {len(clients)} клиентам",
                parse_mode="HTML"
            )
        except Exception:
            pass

async def daily_reminder_check():
    """Ежедневная проверка неоплатившх"""
    today = datetime.now().day
    # Не запускаем 1-го (там уже monthly_reset_and_notify)
    if today == 1:
        return
    
    clients = await get_active_clients()
    for client in clients:
        if client.payment_status in ("paid", "waiting_confirm"):
            continue
        
        await increment_reminder_day(client.id)
        day = client.reminder_day + 1  # уже инкрементировали
        
        try:
            if day == 2:
                await _bot.send_message(
                    client.telegram_id,
                    f"⚠️ <b>{client.name}</b>, напоминаю об оплате VPN.\n\n"
                    f"💰 Сумма: <b>{client.monthly_fee:.0f} ₽</b>\n"
                    f"Пожалуйста, оплати и нажми кнопку:",
                    parse_mode="HTML",
                    reply_markup=_paid_button(client.id)
                )
            elif day >= 3:
                days_left = max(0, 5 - (day - 3))
                if days_left > 0:
                    await _bot.send_message(
                        client.telegram_id,
                        f"🚨 <b>Последнее предупреждение, {client.name}!</b>\n\n"
                        f"Оплата не поступила. Если не оплатишь в течение "
                        f"<b>{days_left} дн.</b> — VPN будет отключён.\n\n"
                        f"💰 Сумма: <b>{client.monthly_fee:.0f} ₽</b>",
                        parse_mode="HTML",
                        reply_markup=_paid_button(client.id)
                    )
                    if day == 3:
                        disconnect_date = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
                        await set_disconnect_date(client.id, disconnect_date)
        except Exception as e:
            logger.error(f"Reminder failed for {client.name}: {e}")

async def disconnect_check():
    """Отключаем клиентов с истёкшей датой отключения"""
    from database import get_all_clients
    today = datetime.now().strftime("%Y-%m-%d")
    clients = await get_all_clients()
    
    for client in clients:
        if (client.active and client.disconnect_date and 
                client.disconnect_date <= today and
                client.payment_status not in ("paid", "waiting_confirm")):
            
            # Отключаем peer на сервере
            if client.wg_pubkey:
                success = await disable_peer(client.server_name, client.wg_pubkey)
            else:
                success = True  # нет ключа — просто меняем статус
            
            if success:
                await set_client_active(client.id, False)
                await log_payment(client.id, "disconnected", note="Auto-disconnect for non-payment")
                
                try:
                    await _bot.send_message(
                        client.telegram_id,
                        f"❌ <b>{client.name}</b>, ваш VPN был отключён в связи с неоплатой.\n\n"
                        f"Для восстановления доступа свяжитесь с администратором.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                
                for admin_id in ADMIN_IDS:
                    try:
                        await _bot.send_message(
                            admin_id,
                            f"🔴 Клиент <b>{client.name}</b> (@{client.username}) "
                            f"автоматически отключён (неоплата).",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

def _paid_button(client_id: int):
    """Inline кнопка 'Я оплатил'"""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid:{client_id}")
    ]])

async def notify_payment_claimed(bot, client):
    """Уведомляем администратора, что клиент сообщил об оплате"""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_pay:{client.id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_pay:{client.id}"),
    ]])
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"💳 <b>Заявка на оплату</b>\n\n"
                f"Клиент: <b>{client.name}</b> (@{client.username})\n"
                f"Сервер: {client.server_name}\n"
                f"Сумма: {client.monthly_fee:.0f} ₽\n"
                f"Устройств: {client.devices}",
                parse_mode="HTML",
                reply_markup=kb
            )
        except Exception as e:
            logger.error(f"Failed to notify admin: {e}")
            