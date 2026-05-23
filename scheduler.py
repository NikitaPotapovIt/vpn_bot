"""
Payment reminder scheduler.
Flow:
  Day 1              -> notify all active clients
  Day 2              -> second reminder for unpaid clients
  Day 3              -> warning: pay or disconnect in 5 days
  Days 4-7           -> daily countdown reminders
  Day 8 (=3+5)       -> auto-disconnect (disable_peer)

  If client taps "I paid" -> status "waiting_confirm"
  If admin confirms       -> status "paid", reminders stop
  If admin rejects        -> status back to "pending", client is notified
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
    get_client_server_names,
    get_user_lang,
)
from ssh_manager import disable_peer
from config import ADMIN_IDS, DEVICE_MONTHLY_PRICE
from i18n import normalize_lang

logger = logging.getLogger(__name__)

# Set during initialization
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
    lang = normalize_lang(await get_user_lang(client.telegram_id))
    servers = await get_client_server_names(client.id, active_only=True)
    if not servers:
        servers = [client.server_name]
    if len(servers) == 1:
        return f"🖥 Server: {servers[0]}" if lang == "en" else f"🖥 Сервер: {servers[0]}"
    return f"🖥 Servers: {', '.join(servers)}" if lang == "en" else f"🖥 Серверы: {', '.join(servers)}"

def init_scheduler(bot) -> AsyncIOScheduler:
    global _bot
    _bot = bot
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    
    # Day 1 of each month at 10:00: reset and first reminder
    scheduler.add_job(monthly_reset_and_notify, CronTrigger(day=1, hour=10, minute=0))
    
    # Daily at 10:00: unpaid check
    scheduler.add_job(daily_reminder_check, CronTrigger(hour=10, minute=0))
    
    # Daily at 09:00: disconnect check
    scheduler.add_job(disconnect_check, CronTrigger(hour=9, minute=0))
    
    scheduler.start()
    logger.info("Scheduler started")
    return scheduler

async def monthly_reset_and_notify():
    """Day 1: reset statuses and send reminders."""
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
            lang = normalize_lang(await get_user_lang(client.telegram_id))
            await _bot.send_message(
                client.telegram_id,
                (
                    f"👋 <b>Hello, {client.name}!</b>\n\n"
                    f"It's the 1st day of month - time to pay for VPN.\n"
                    f"💰 Amount: <b>{client.monthly_fee:.0f} ₽</b> (devices: {client.devices})\n"
                    f"{servers_line}\n\n"
                    f"Transfer to T-Bank card, phone number +79625700040\n\n"
                    f"Press the button below after payment:"
                    if lang == "en"
                    else
                    f"👋 <b>Привет, {client.name}!</b>\n\n"
                    f"Наступило 1-е число — время оплаты VPN.\n"
                    f"💰 Сумма: <b>{client.monthly_fee:.0f} ₽</b> (устройств: {client.devices})\n"
                    f"{servers_line}\n\n"
                    f"Перевод на карту T-Bank, по номеру +79625700040\n\n"
                    f"Нажми кнопку ниже, когда переведёшь оплату:"
                ),
                parse_mode="HTML",
                reply_markup=_paid_button(client.id)
            )
        except Exception as e:
            logger.error(f"Failed to notify {client.name}: {e}")
    
    # Notify admins
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
    """Daily unpaid check."""
    today = datetime.now().day
    # Skip day 1 (handled by monthly_reset_and_notify)
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
        day = client.reminder_day + 1  # already incremented
        
        try:
            servers_line = await _servers_line(client)
            lang = normalize_lang(await get_user_lang(client.telegram_id))
            if day == 2:
                await _bot.send_message(
                    client.telegram_id,
                    (
                        f"⚠️ <b>{client.name}</b>, reminder about VPN payment.\n\n"
                        f"💰 Amount: <b>{client.monthly_fee:.0f} ₽</b>\n"
                        f"{servers_line}\n"
                        f"Please pay and press the button:"
                        if lang == "en"
                        else
                        f"⚠️ <b>{client.name}</b>, напоминаю об оплате VPN.\n\n"
                        f"💰 Сумма: <b>{client.monthly_fee:.0f} ₽</b>\n"
                        f"{servers_line}\n"
                        f"Пожалуйста, оплати и нажми кнопку:"
                    ),
                    parse_mode="HTML",
                    reply_markup=_paid_button(client.id)
                )
            elif day >= 3:
                days_left = max(0, 5 - (day - 3))
                if days_left > 0:
                    await _bot.send_message(
                        client.telegram_id,
                        (
                            f"🚨 <b>Final warning, {client.name}!</b>\n\n"
                            f"Payment not received. If you don't pay within "
                            f"<b>{days_left} days</b>, VPN will be disabled.\n\n"
                            f"💰 Amount: <b>{client.monthly_fee:.0f} ₽</b>\n"
                            f"{servers_line}"
                            if lang == "en"
                            else
                            f"🚨 <b>Последнее предупреждение, {client.name}!</b>\n\n"
                            f"Оплата не поступила. Если не оплатишь в течение "
                            f"<b>{days_left} дн.</b> — VPN будет отключён.\n\n"
                            f"💰 Сумма: <b>{client.monthly_fee:.0f} ₽</b>\n"
                            f"{servers_line}"
                        ),
                        parse_mode="HTML",
                        reply_markup=_paid_button(client.id)
                    )
                    if day == 3:
                        disconnect_date = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
                        await set_disconnect_date(client.id, disconnect_date)
        except Exception as e:
            logger.error(f"Reminder failed for {client.name}: {e}")

async def disconnect_check():
    """Disconnect clients with expired disconnect date."""
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
            
            # Disable peer on server
            keys = await get_client_keys(client.id)
            if keys:
                # Disable only keys billed to this payer.
                # Otherwise shared keys paid by another client may be affected.
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
                success = True  # no key: just update status
            
            if success:
                await set_client_active(client.id, False)
                await log_payment(client.id, "disconnected", note="Auto-disconnect for non-payment")
                
                try:
                    lang = normalize_lang(await get_user_lang(client.telegram_id))
                    await _bot.send_message(
                        client.telegram_id,
                        (
                            f"❌ <b>{client.name}</b>, your VPN has been disabled due to non-payment.\n\n"
                            f"Contact administrator to restore access."
                            if lang == "en"
                            else
                            f"❌ <b>{client.name}</b>, ваш VPN был отключён в связи с неоплатой.\n\n"
                            f"Для восстановления доступа свяжитесь с администратором."
                        ),
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
    """Inline 'I paid' button."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Я оплатил / I paid", callback_data=f"paid:{client_id}")
    ]])

async def notify_payment_claimed(bot, client):
    """Notify admins that client claimed payment."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_pay:{client.id}"),
            InlineKeyboardButton(text="🎁 Тестовый период", callback_data=f"trial_until_month_end:{client.id}"),
        ],
        [
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_pay:{client.id}"),
        ],
        [
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_home"),
        ],
    ])
    servers_line = await _servers_line(client)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"💳 <b>Заявка на оплату</b>\n\n"
                f"Клиент: <b>{client.name}</b> (@{client.username})\n"
                f"{servers_line}\n"
                f"Платных ключей: {client.payable_key_count}\n"
                f"Тариф: {DEVICE_MONTHLY_PRICE:.0f} ₽/устройство\n"
                f"Сумма за месяц: {client.monthly_fee:.0f} ₽",
                parse_mode="HTML",
                reply_markup=kb
            )
        except Exception as e:
            logger.error(f"Failed to notify admin: {e}")
            
