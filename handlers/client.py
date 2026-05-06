"""Обработчики для клиентов VPN"""

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (Message, CallbackQuery,
                            ReplyKeyboardMarkup, KeyboardButton,
                            InlineKeyboardMarkup, InlineKeyboardButton)
from database import get_client_by_tg, update_payment_status
from scheduler import notify_payment_claimed
from config import ADMIN_IDS
import logging
from datetime import datetime, date

router = Router()
logger = logging.getLogger(__name__)


def _parse_date(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


def _status_human(client) -> str:
    if client.monthly_fee <= 0:
        return "🚫 Оплата не требуется"
    paid_until = _parse_date(client.paid_until)
    if paid_until and paid_until >= date.today():
        return f"✅ Оплачено до {paid_until.strftime('%d.%m.%Y')}"
    if client.payment_status == "paid":
        return "⏳ Ожидает продления"
    return {
        "paid": "✅ Оплачено",
        "pending": "⏳ Ожидает оплаты",
        "waiting_confirm": "🔄 Проверяется",
        "overdue": "🔴 Просрочено",
    }.get(client.payment_status, client.payment_status)

def client_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Мой статус"), KeyboardButton(text="✅ Я оплатил")],
        ],
        resize_keyboard=True,
    )

@router.message(Command("start"))
async def cmd_start(msg: Message):
    # Администраторы обрабатываются в admin.py
    if msg.from_user.id in ADMIN_IDS:
        return

    client = await get_client_by_tg(msg.from_user.id)
    if client:
        status_text = _status_human(client)

        await msg.answer(
            f"👋 <b>Привет, {client.name}!</b>\n\n"
            f"🖥 Сервер: {client.server_name}\n"
            f"📱 Устройств: {client.devices}\n"
            f"💰 Оплата: {client.monthly_fee:.0f} ₽/мес\n"
            f"Статус: {status_text}",
            parse_mode="HTML",
            reply_markup=client_kb()
        )
    else:
        await msg.answer(
            "Привет! Ты не зарегистрирован в системе.\n"
            "Обратись к администратору для подключения."
        )

@router.message(F.text == "📊 Мой статус")
@router.message(Command("status"))
async def cmd_status(msg: Message):
    if msg.from_user.id in ADMIN_IDS:
        return
    client = await get_client_by_tg(msg.from_user.id)
    if not client:
        await msg.answer("❌ Ты не зарегистрирован.")
        return

    active = "🟢 активен" if client.active else "🔴 отключён"
    disc = f"\n⚠️ Плановое отключение: {client.disconnect_date}" if client.disconnect_date else ""
    paid_until = _parse_date(client.paid_until)
    paid_line = f"\n📅 Оплачено до: {paid_until.strftime('%d.%m.%Y')}" if paid_until else ""

    kb = None
    needs_payment = client.monthly_fee > 0 and (
        client.payment_status in ("pending", "overdue")
        or (client.payment_status == "paid" and (not paid_until or paid_until < date.today()))
    )
    if needs_payment:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid:{client.id}")
        ]])

    await msg.answer(
        f"📊 <b>Статус подписки</b>\n\n"
        f"Сервер: {client.server_name}\n"
        f"VPN: {active}\n"
        f"Оплата: {_status_human(client)}"
        f"{paid_line}"
        f"{disc}",
        parse_mode="HTML",
        reply_markup=kb or client_kb()
    )

@router.message(F.text == "✅ Я оплатил")
async def btn_paid(msg: Message):
    if msg.from_user.id in ADMIN_IDS:
        return
    client = await get_client_by_tg(msg.from_user.id)
    if not client:
        await msg.answer("❌ Ты не зарегистрирован.")
        return
    await _process_paid(msg.bot, client, reply=msg)

@router.callback_query(F.data.startswith("paid:"))
async def client_paid(cb: CallbackQuery):
    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_tg(cb.from_user.id)
    if not client or client.id != client_id:
        await cb.answer("❌ Ошибка авторизации", show_alert=True)
        return
    await _process_paid(cb.bot, client, callback=cb)

async def _process_paid(bot, client, reply=None, callback=None):
    if client.payment_status == "waiting_confirm":
        text = "Оплата уже отправлена на проверку! Ожидай подтверждения. 🔄"
        if callback:
            await callback.answer(text, show_alert=True)
        else:
            await reply.answer(text)
        return

    if client.payment_status == "paid":
        text = "Твоя оплата уже подтверждена ✅"
        if callback:
            await callback.answer(text, show_alert=True)
        else:
            await reply.answer(text)
        return

    await update_payment_status(client.id, "waiting_confirm")
    await notify_payment_claimed(bot, client)

    text = (
        f"🔄 <b>Заявка отправлена!</b>\n\n"
        f"Администратор проверит и подтвердит оплату."
    )
    if callback:
        await callback.message.edit_text(text, parse_mode="HTML")
        await callback.answer("Заявка отправлена!")
    else:
        await reply.answer(text, parse_mode="HTML", reply_markup=client_kb())
        
