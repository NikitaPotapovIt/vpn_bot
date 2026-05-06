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
    set_disconnect_date, set_client_active, log_payment, reset_monthly_payments,
    get_client_keys,
)
from ssh_manager import disable_peer
from config import ADMIN_IDS, DEVICE_MONTHLY_PRICE

logger = logging.getLogger(__name__)

# Будет установлен при инициализации
_bot = None


def _parse_date(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


def _is_paid_now(client, today=None) -> bool:
    if today is None:
        today = datetime.now().date()
    paid_until = _parse_date(client.paid_until)
    return bool(paid_until and paid_until >= today)


async def _servers_line(client) -> str:
    keys = await get_client_keys(client.id)
    servers = sorted({k.server_name for k in keys if k.active})
    if not servers:
        servers = [client.server_name]
    if len(servers) == 1:
        return f"🖥 Сервер: {servers[0]}"
    return f"🖥 Серверы: {', '.join(servers)}"

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
    due_clients = []
    for client in clients:
        if client.payable_key_count <= 0:
            continue
        if _is_paid_now(client):
            continue
        if client.payment_status == "waiting_confirm":
            continue
        due_clients.append(client)
        try:
            servers_line = await _servers_line(client)
            await _bot.send_message(
                client.telegram_id,
                f"👋 <b>Привет, {client.name}!</b>\n\n"
                f"Наступило 1-е число — время оплаты VPN.\n"
                f"💰 Сумма: <b>{client.monthly_fee:.0f} ₽</b> (устройств: {client.devices})\n"
                f"{servers_line}\n\n"
                f"Перевод на карту T-Bank, по номеру +79625700040\n\n"
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
                f"Разослано напоминаний: {len(due_clients)} клиентам",
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
    
    today_date = datetime.now().date()
    clients = await get_active_clients()
    for client in clients:
        if client.payable_key_count <= 0:
            continue
        if _is_paid_now(client, today_date):
            continue
        if client.payment_status in ("paid", "waiting_confirm"):
            continue
        
        await increment_reminder_day(client.id)
        day = client.reminder_day + 1  # уже инкрементировали
        
        try:
            servers_line = await _servers_line(client)
            if day == 2:
                await _bot.send_message(
                    client.telegram_id,
                    f"⚠️ <b>{client.name}</b>, напоминаю об оплате VPN.\n\n"
                    f"💰 Сумма: <b>{client.monthly_fee:.0f} ₽</b>\n"
                    f"{servers_line}\n"
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
                        f"💰 Сумма: <b>{client.monthly_fee:.0f} ₽</b>\n"
                        f"{servers_line}",
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
    today_date = datetime.now().date()
    clients = await get_all_clients()
    
    for client in clients:
        if client.payable_key_count <= 0:
            continue
        if _is_paid_now(client, today_date):
            continue
        if (client.active and client.disconnect_date and 
                client.disconnect_date <= today and
                client.payment_status not in ("paid", "waiting_confirm")):
            
            # Отключаем peer на сервере
            keys = await get_client_keys(client.id)
            if keys:
                # Отключаем только те ключи, за которые отвечает этот плательщик.
                # Иначе можно затронуть shared-ключи, оплачиваемые другим клиентом.
                keys_to_disable = [k for k in keys if k.payer and k.billing_client_id == client.id]
                if not keys_to_disable:
                    success = True
                    keys_to_disable = []
                failures = 0
                for key in keys_to_disable:
                    try:
                        ok = await disable_peer(key.server_name, key.wg_pubkey)
                        if not ok:
                            failures += 1
                    except Exception:
                        failures += 1
                success = failures == 0
            elif client.wg_pubkey:
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
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_pay:{client.id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_pay:{client.id}"),
        ],
        [
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_home"),
        ],
    ])
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"💳 <b>Заявка на оплату</b>\n\n"
                f"Клиент: <b>{client.name}</b> (@{client.username})\n"
                f"Сервер: {client.server_name}\n"
                f"Платных ключей: {client.payable_key_count}\n"
                f"Тариф: {DEVICE_MONTHLY_PRICE:.0f} ₽/устройство\n"
                f"Сумма за месяц: {client.monthly_fee:.0f} ₽",
                parse_mode="HTML",
                reply_markup=kb
            )
        except Exception as e:
            logger.error(f"Failed to notify admin: {e}")
            
