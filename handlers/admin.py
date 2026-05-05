"""Обработчики для администратора"""

import io
import logging
from datetime import datetime
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import ADMIN_IDS, SERVERS
from database import (
    add_client, get_all_clients, get_client_by_id,
    update_payment_status, set_client_active, log_payment
)
from ssh_manager import (
    get_server_status, get_all_peers_status, add_peer,
    remove_peer, disable_peer, enable_peer, ping_server
)
from scheduler import notify_payment_claimed

router = Router()
logger = logging.getLogger(__name__)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ─── FSM для добавления клиента ──────────────────────────────────────────────

class AddClientForm(StatesGroup):
    telegram_id = State()
    name = State()
    username = State()
    server = State()
    devices = State()
    fee = State()
    confirm = State()

@router.message(Command("add_client"))
async def cmd_add_client(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await msg.answer("Введи <b>Telegram ID</b> нового клиента:", parse_mode="HTML")
    await state.set_state(AddClientForm.telegram_id)

@router.message(AddClientForm.telegram_id)
async def add_client_tg_id(msg: Message, state: FSMContext):
    try:
        tg_id = int(msg.text.strip())
        await state.update_data(telegram_id=tg_id)
        await msg.answer("Введи <b>имя</b> клиента (как будет отображаться):", parse_mode="HTML")
        await state.set_state(AddClientForm.name)
    except ValueError:
        await msg.answer("❌ Неверный формат ID. Введи число.")

@router.message(AddClientForm.name)
async def add_client_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text.strip())
    await msg.answer("Введи <b>@username</b> (без @, или '-' если нет):", parse_mode="HTML")
    await state.set_state(AddClientForm.username)

@router.message(AddClientForm.username)
async def add_client_username(msg: Message, state: FSMContext):
    username = msg.text.strip().lstrip("@")
    if username == "-":
        username = None
    await state.update_data(username=username)
    
    # Выбор сервера
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=s.name, callback_data=f"sel_srv:{s.name}")]
        for s in SERVERS
    ])
    await msg.answer("Выбери <b>сервер</b> для клиента:", parse_mode="HTML", reply_markup=kb)
    await state.set_state(AddClientForm.server)

@router.callback_query(AddClientForm.server, F.data.startswith("sel_srv:"))
async def add_client_server(cb: CallbackQuery, state: FSMContext):
    server_name = cb.data.split(":", 1)[1]
    await state.update_data(server=server_name)
    await cb.message.edit_text(f"Сервер: <b>{server_name}</b>\n\nСколько <b>устройств</b>?", parse_mode="HTML")
    await state.set_state(AddClientForm.devices)

@router.message(AddClientForm.devices)
async def add_client_devices(msg: Message, state: FSMContext):
    try:
        devices = int(msg.text.strip())
        await state.update_data(devices=devices)
        await msg.answer("Введи <b>ежемесячную сумму</b> (₽):", parse_mode="HTML")
        await state.set_state(AddClientForm.fee)
    except ValueError:
        await msg.answer("❌ Введи число.")

@router.message(AddClientForm.fee)
async def add_client_fee(msg: Message, state: FSMContext):
    try:
        fee = float(msg.text.strip())
        data = await state.get_data()
        await state.update_data(fee=fee)
        
        summary = (
            f"<b>Проверь данные:</b>\n\n"
            f"Telegram ID: <code>{data['telegram_id']}</code>\n"
            f"Имя: {data['name']}\n"
            f"Username: @{data.get('username') or '-'}\n"
            f"Сервер: {data['server']}\n"
            f"Устройств: {data['devices']}\n"
            f"Оплата: {fee:.0f} ₽/мес\n\n"
            f"Создать WireGuard-конфиг автоматически?"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Создать конфиг", callback_data="add_with_wg"),
            InlineKeyboardButton(text="📝 Без конфига", callback_data="add_no_wg"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="add_cancel"),
        ]])
        await msg.answer(summary, parse_mode="HTML", reply_markup=kb)
        await state.set_state(AddClientForm.confirm)
    except ValueError:
        await msg.answer("❌ Введи число (например 300).")

@router.callback_query(AddClientForm.confirm, F.data.in_({"add_with_wg", "add_no_wg", "add_cancel"}))
async def add_client_confirm(cb: CallbackQuery, state: FSMContext):
    if cb.data == "add_cancel":
        await state.clear()
        await cb.message.edit_text("❌ Отменено.")
        return
    
    data = await state.get_data()
    await state.clear()
    await cb.message.edit_text("⏳ Создаю клиента...")
    
    wg_data = None
    if cb.data == "add_with_wg":
        wg_data = await add_peer(data["server"], data["name"])
        if not wg_data:
            await cb.message.edit_text("❌ Не удалось создать WireGuard конфиг. Клиент добавлен без конфига.")
    
    client_id = await add_client(
        telegram_id=data["telegram_id"],
        name=data["name"],
        username=data.get("username"),
        server_name=data["server"],
        devices=data["devices"],
        monthly_fee=data["fee"],
        wg_pubkey=wg_data["pubkey"] if wg_data else None,
        wg_peer_id=wg_data["pubkey"] if wg_data else None,
    )
    
    result = f"✅ Клиент <b>{data['name']}</b> добавлен (ID: {client_id})\n"
    
    if wg_data:
        # Отправить конфиг файлом
        config_bytes = wg_data["config_text"].encode()
        config_file = io.BytesIO(config_bytes)
        config_file.name = f"vpn_{data['name'].replace(' ', '_')}.conf"
        await cb.message.answer_document(
            config_file,
            caption=f"🔑 WireGuard конфиг для <b>{data['name']}</b>\n"
                    f"IP: {wg_data['client_ip']}\nСервер: {data['server']}",
            parse_mode="HTML"
        )
        result += f"IP клиента: {wg_data['client_ip']}"
    
    await cb.message.edit_text(result, parse_mode="HTML")

# ─── Список клиентов ──────────────────────────────────────────────────────────

@router.message(Command("clients"))
async def cmd_clients(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    clients = await get_all_clients()
    if not clients:
        await msg.answer("Клиентов нет.")
        return
    
    text = "<b>📋 Клиенты:</b>\n\n"
    for c in clients:
        status_emoji = {"paid": "✅", "pending": "⏳", "waiting_confirm": "🔄", "overdue": "🔴"}.get(c.payment_status, "❓")
        active_emoji = "🟢" if c.active else "🔴"
        text += (
            f"{active_emoji} <b>{c.name}</b> (@{c.username or '-'})\n"
            f"   {status_emoji} {c.payment_status} | {c.server_name} | {c.monthly_fee:.0f}₽ | {c.devices} уст.\n"
        )
    
    await msg.answer(text, parse_mode="HTML")

@router.message(Command("client"))
async def cmd_client_detail(msg: Message):
    """Детальная карточка клиента: /client 5"""
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) < 2:
        await msg.answer("Использование: /client <id>")
        return
    try:
        client_id = int(parts[1])
    except ValueError:
        await msg.answer("❌ Неверный ID")
        return
    
    client = await get_client_by_id(client_id)
    if not client:
        await msg.answer("Клиент не найден")
        return
    
    # Статус peer'а
    peer_info = ""
    if client.wg_pubkey:
        from ssh_manager import get_peer_status
        p = await get_peer_status(client.server_name, client.wg_pubkey)
        conn = "🟢 онлайн" if p["connected"] else "🔴 офлайн"
        peer_info = f"\nВпн: {conn} | ↓{p['rx_mb']} MB ↑{p['tx_mb']} MB"
    
    text = (
        f"<b>👤 {client.name}</b>\n"
        f"TG: <code>{client.telegram_id}</code> | @{client.username or '-'}\n"
        f"Сервер: {client.server_name}\n"
        f"Устройств: {client.devices} | Оплата: {client.monthly_fee:.0f} ₽\n"
        f"Статус: {client.payment_status} | Активен: {'да' if client.active else 'нет'}\n"
        f"Последняя оплата: {client.payment_date or '-'}\n"
        f"Отключение: {client.disconnect_date or '-'}"
        f"{peer_info}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔴 Отключить" if client.active else "🟢 Включить",
                                 callback_data=f"toggle_client:{client.id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_client:{client.id}"),
        ]
    ])
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)

# ─── Серверы ──────────────────────────────────────────────────────────────────

@router.message(Command("servers"))
async def cmd_servers(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    await msg.answer("⏳ Опрашиваю серверы...")
    
    for server in SERVERS:
        try:
            status = await get_server_status(server.name)
            peers = await get_all_peers_status(server.name)
            online = sum(1 for p in peers if p["connected"])
            
            text = (
                f"🖥 <b>{status['name']}</b>\n"
                f"IP: {status['host']}\n"
                f"Uptime: {status['uptime']}\n"
                f"Load: {' '.join(status['load'])}\n"
                f"WireGuard: {'✅ работает' if status['wg_running'] else '❌ не запущен'}\n"
                f"Peer'ов: {status['peers_count']} (онлайн: {online})"
            )
            await msg.answer(text, parse_mode="HTML")
        except Exception as e:
            await msg.answer(f"❌ <b>{server.name}</b>: ошибка — {e}", parse_mode="HTML")

@router.message(Command("server"))
async def cmd_server_detail(msg: Message):
    """Детально по одному серверу: /server Server 1 (DE)"""
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        names = ", ".join(s.name for s in SERVERS)
        await msg.answer(f"Использование: /server <имя>\nДоступные: {names}")
        return
    
    server_name = parts[1]
    peers = await get_all_peers_status(server_name)
    if not peers:
        await msg.answer("Сервер не найден или peer'ы отсутствуют.")
        return
    
    lines = [f"<b>Peer'ы на {server_name}:</b>\n"]
    for p in peers:
        icon = "🟢" if p["connected"] else "🔴"
        lines.append(f"{icon} {p['pubkey']} | ↓{p['rx_mb']} ↑{p['tx_mb']} MB")
    
    await msg.answer("\n".join(lines), parse_mode="HTML")

# ─── Подтверждение/отклонение оплаты ─────────────────────────────────────────

@router.callback_query(F.data.startswith("confirm_pay:"))
async def confirm_payment(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer("Клиент не найден")
        return
    
    await update_payment_status(client_id, "paid", datetime.now().strftime("%Y-%m-%d"))
    await log_payment(client_id, "confirmed", client.monthly_fee)
    
    await cb.message.edit_text(
        f"✅ Оплата от <b>{client.name}</b> подтверждена.",
        parse_mode="HTML"
    )
    try:
        await cb.bot.send_message(
            client.telegram_id,
            f"✅ <b>Оплата подтверждена!</b>\n\nСпасибо, {client.name}. "
            f"Ваш VPN активен до конца месяца.",
            parse_mode="HTML"
        )
    except Exception:
        pass

@router.callback_query(F.data.startswith("reject_pay:"))
async def reject_payment(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer("Клиент не найден")
        return
    
    await update_payment_status(client_id, "pending")
    await log_payment(client_id, "rejected", note="Admin rejected")
    
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    await cb.message.edit_text(
        f"❌ Оплата от <b>{client.name}</b> отклонена.",
        parse_mode="HTML"
    )
    try:
        await cb.bot.send_message(
            client.telegram_id,
            f"❌ <b>Оплата не подтверждена.</b>\n\n"
            f"Пожалуйста, проверь правильность перевода и попробуй снова.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid:{client.id}")
            ]])
        )
    except Exception:
        pass

@router.callback_query(F.data.startswith("toggle_client:"))
async def toggle_client(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        return
    
    new_state = not client.active
    await set_client_active(client_id, new_state)
    
    if client.wg_pubkey:
        if new_state:
            await enable_peer(client.server_name, client.wg_pubkey, f"10.8.0.X/32")
        else:
            await disable_peer(client.server_name, client.wg_pubkey)
    
    status = "включён" if new_state else "отключён"
    await cb.answer(f"Клиент {client.name} {status}")
    await cb.message.edit_text(f"{'🟢' if new_state else '🔴'} Клиент <b>{client.name}</b> {status}.", parse_mode="HTML")

@router.callback_query(F.data.startswith("del_client:"))
async def delete_client(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        return
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"del_confirm:{client_id}"),
        InlineKeyboardButton(text="❌ Нет", callback_data="del_no"),
    ]])
    await cb.message.edit_text(
        f"Удалить <b>{client.name}</b> и его peer с сервера?",
        parse_mode="HTML", reply_markup=kb
    )

@router.callback_query(F.data.startswith("del_confirm:"))
async def delete_client_confirm(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        return
    
    if client.wg_pubkey:
        await remove_peer(client.server_name, client.wg_pubkey)
    
    async with __import__("aiosqlite").connect("vpn_bot.db") as db:
        await db.execute("DELETE FROM clients WHERE id = ?", (client_id,))
        await db.commit()
    
    await cb.message.edit_text(f"🗑 Клиент <b>{client.name}</b> удалён.", parse_mode="HTML")

@router.callback_query(F.data == "del_no")
async def delete_cancel(cb: CallbackQuery):
    await cb.message.edit_text("Отменено.")
    