"""Обработчики для клиентов VPN"""

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (Message, CallbackQuery,
                            ReplyKeyboardMarkup, KeyboardButton,
                            InlineKeyboardMarkup, InlineKeyboardButton)
from database import get_client_by_tg, update_payment_status, get_client_server_names
from scheduler import notify_payment_claimed
from config import ADMIN_IDS
import logging
import html
from datetime import datetime, date
from support_dialog import (
    SUPPORT_CLIENT_OPEN_TEXT,
    SUPPORT_CLOSE_TEXT,
    open_client_dialog,
    close_client_dialog,
    is_client_dialog_open,
)

router = Router()
logger = logging.getLogger(__name__)

# Важно: не перехватываем апдейты админа в клиентском роутере.
# Иначе текстовые кнопки админ-меню могут "съедаться" этим роутером
# (он подключается раньше admin_router).
router.message.filter(~F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(~F.from_user.id.in_(ADMIN_IDS))


def _parse_date(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


def _status_human(client) -> str:
    if not _has_payable_keys(client):
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


def _has_payable_keys(client) -> bool:
    return int(getattr(client, "payable_key_count", 0) or 0) > 0


def client_kb(show_pay_button: bool = True, support_mode: bool = False) -> ReplyKeyboardMarkup:
    row = [KeyboardButton(text="📊 Мой статус")]
    if show_pay_button:
        row.append(KeyboardButton(text="✅ Я оплатил"))
    keyboard = [row, [KeyboardButton(text=SUPPORT_CLIENT_OPEN_TEXT)]]
    if support_mode:
        keyboard.append([KeyboardButton(text=SUPPORT_CLOSE_TEXT)])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
    )


def _admin_support_kb(client_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✍️ Ответить", callback_data=f"support_reply:{client_id}"),
        InlineKeyboardButton(text="❌ Закрыть диалог", callback_data=f"support_close:{client_id}"),
    ]])


async def _notify_admins_support_message(bot, client, text: str):
    username = f"@{html.escape(client.username)}" if client.username else "—"
    safe_text = html.escape(text.strip())[:3500]
    payload = (
        "💬 <b>Сообщение в поддержку</b>\n\n"
        f"От: <b>{html.escape(client.name)}</b>\n"
        f"TG: <code>{client.telegram_id}</code> | {username}\n\n"
        f"{safe_text}"
    )
    kb = _admin_support_kb(client.id)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, payload, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.exception("failed to forward support message to admin %s", admin_id)


async def _servers_line(client) -> str:
    servers = await get_client_server_names(client.id, active_only=True)
    if not servers:
        servers = [client.server_name]
    if len(servers) == 1:
        return f"🖥 Сервер: {servers[0]}"
    return f"🖥 Серверы: {', '.join(servers)}"

@router.message(Command("start"))
async def cmd_start(msg: Message):
    # Администраторы обрабатываются в admin.py
    if msg.from_user.id in ADMIN_IDS:
        return

    client = await get_client_by_tg(msg.from_user.id)
    if client:
        status_text = _status_human(client)
        has_payable_keys = _has_payable_keys(client)
        servers_line = await _servers_line(client)

        await msg.answer(
            f"👋 <b>Привет, {client.name}!</b>\n\n"
            f"{servers_line}\n"
            f"📱 Устройств: {client.devices}\n"
            f"💰 Оплата: {client.monthly_fee:.0f} ₽/мес\n"
            f"Статус: {status_text}",
            parse_mode="HTML",
            reply_markup=client_kb(
                show_pay_button=has_payable_keys,
                support_mode=is_client_dialog_open(msg.from_user.id),
            ),
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
    has_payable_keys = _has_payable_keys(client)
    servers_line = await _servers_line(client)

    kb = None
    needs_payment = has_payable_keys and (
        client.payment_status in ("pending", "overdue")
        or (client.payment_status == "paid" and (not paid_until or paid_until < date.today()))
    )
    if needs_payment:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid:{client.id}")
        ]])

    await msg.answer(
        f"📊 <b>Статус подписки</b>\n\n"
        f"{servers_line}\n"
        f"VPN: {active}\n"
        f"Оплата: {_status_human(client)}"
        f"{paid_line}"
        f"{disc}",
        parse_mode="HTML",
        reply_markup=kb or client_kb(
            show_pay_button=has_payable_keys,
            support_mode=is_client_dialog_open(msg.from_user.id),
        ),
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
    if not _has_payable_keys(client):
        text = "Для этого аккаунта оплата не требуется."
        if callback:
            await callback.answer(text, show_alert=True)
        else:
            await reply.answer(
                text,
                reply_markup=client_kb(show_pay_button=False, support_mode=is_client_dialog_open(client.telegram_id)),
            )
        return

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
        await reply.answer(
            text,
            parse_mode="HTML",
            reply_markup=client_kb(
                show_pay_button=_has_payable_keys(client),
                support_mode=is_client_dialog_open(client.telegram_id),
            ),
        )


@router.message(F.text == SUPPORT_CLIENT_OPEN_TEXT)
async def open_support_dialog(msg: Message):
    if msg.from_user.id in ADMIN_IDS:
        return
    client = await get_client_by_tg(msg.from_user.id)
    if not client:
        await msg.answer("❌ Ты не зарегистрирован.")
        return

    already_open = is_client_dialog_open(client.telegram_id)
    open_client_dialog(client.telegram_id)

    if not already_open:
        open_text = (
            "💬 <b>Клиент открыл диалог с поддержкой</b>\n\n"
            f"Клиент: <b>{html.escape(client.name)}</b>\n"
            f"TG: <code>{client.telegram_id}</code>\n"
            f"Username: @{html.escape(client.username) if client.username else '-'}"
        )
        kb = _admin_support_kb(client.id)
        for admin_id in ADMIN_IDS:
            try:
                await msg.bot.send_message(admin_id, open_text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                logger.exception("failed to notify admin %s about support open", admin_id)

    await msg.answer(
        "✅ Диалог с поддержкой открыт.\nНапиши сообщение, и администратор ответит здесь.",
        reply_markup=client_kb(show_pay_button=_has_payable_keys(client), support_mode=True),
    )


@router.message(F.text == SUPPORT_CLOSE_TEXT)
async def close_support_dialog_client(msg: Message):
    if msg.from_user.id in ADMIN_IDS:
        return
    client = await get_client_by_tg(msg.from_user.id)
    if not client:
        await msg.answer("❌ Ты не зарегистрирован.")
        return

    was_open = is_client_dialog_open(client.telegram_id)
    close_client_dialog(client.telegram_id)
    await msg.answer(
        "✅ Диалог с поддержкой закрыт.",
        reply_markup=client_kb(show_pay_button=_has_payable_keys(client), support_mode=False),
    )
    if was_open:
        notice = (
            "ℹ️ <b>Клиент закрыл диалог поддержки</b>\n\n"
            f"Клиент: <b>{html.escape(client.name)}</b>\n"
            f"TG: <code>{client.telegram_id}</code>"
        )
        for admin_id in ADMIN_IDS:
            try:
                await msg.bot.send_message(admin_id, notice, parse_mode="HTML")
            except Exception:
                logger.exception("failed to notify admin %s about support close", admin_id)


@router.message(F.text)
async def support_text_from_client(msg: Message):
    if msg.from_user.id in ADMIN_IDS:
        return
    client = await get_client_by_tg(msg.from_user.id)
    if not client:
        return
    if not is_client_dialog_open(client.telegram_id):
        return

    text = (msg.text or "").strip()
    if not text or text in {"📊 Мой статус", "✅ Я оплатил", SUPPORT_CLIENT_OPEN_TEXT, SUPPORT_CLOSE_TEXT}:
        return

    await _notify_admins_support_message(msg.bot, client, text)
    await msg.answer(
        "📨 Сообщение отправлено в поддержку.",
        reply_markup=client_kb(show_pay_button=_has_payable_keys(client), support_mode=True),
    )
