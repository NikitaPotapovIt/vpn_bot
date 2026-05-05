"""Обработчики для клиентов VPN"""

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from database import get_client_by_tg, update_payment_status
from scheduler import notify_payment_claimed
import logging

router = Router()
logger = logging.getLogger(__name__)

@router.message(Command("start"))
async def cmd_start(msg: Message):
    client = await get_client_by_tg(msg.from_user.id)
    if client:
        status_text = {
            "paid": "✅ Оплачено",
            "pending": "⏳ Ожидает оплаты",
            "waiting_confirm": "🔄 Проверяется",
            "overdue": "🔴 Просрочено",
        }.get(client.payment_status, client.payment_status)
        
        await msg.answer(
            f"👋 <b>Привет, {client.name}!</b>\n\n"
            f"🖥 Сервер: {client.server_name}\n"
            f"📱 Устройств: {client.devices}\n"
            f"💰 Оплата: {client.monthly_fee:.0f} ₽/мес\n"
            f"Статус: {status_text}",
            parse_mode="HTML"
        )
    else:
        await msg.answer(
            "Привет! Ты не зарегистрирован в системе.\n"
            "Обратись к администратору для подключения."
        )

@router.message(Command("status"))
async def cmd_status(msg: Message):
    client = await get_client_by_tg(msg.from_user.id)
    if not client:
        await msg.answer("❌ Ты не зарегистрирован.")
        return
    
    active = "🟢 активен" if client.active else "🔴 отключён"
    disc = f"\n⚠️ Плановое отключение: {client.disconnect_date}" if client.disconnect_date else ""
    
    await msg.answer(
        f"📊 <b>Статус подписки</b>\n\n"
        f"Сервер: {client.server_name}\n"
        f"VPN: {active}\n"
        f"Оплата: {client.payment_status}"
        f"{disc}",
        parse_mode="HTML"
    )

@router.callback_query(F.data.startswith("paid:"))
async def client_paid(cb: CallbackQuery):
    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_tg(cb.from_user.id)
    
    if not client or client.id != client_id:
        await cb.answer("❌ Ошибка авторизации", show_alert=True)
        return
    
    if client.payment_status == "waiting_confirm":
        await cb.answer("Оплата уже отправлена на проверку! Ожидай подтверждения.", show_alert=True)
        return
    
    if client.payment_status == "paid":
        await cb.answer("Твоя оплата уже подтверждена ✅", show_alert=True)
        return
    
    await update_payment_status(client_id, "waiting_confirm")
    await notify_payment_claimed(cb.bot, client)
    
    await cb.message.edit_text(
        f"🔄 <b>Заявка отправлена!</b>\n\n"
        f"Администратор проверит оплату и подтвердит. "
        f"Обычно это занимает несколько минут.",
        parse_mode="HTML"
    )
    await cb.answer("Заявка отправлена!")