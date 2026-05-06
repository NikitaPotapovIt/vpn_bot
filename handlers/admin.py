"""Обработчики администратора с inline-кнопками и меню"""

import io
import logging
from datetime import datetime
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (Message, CallbackQuery,
                            InlineKeyboardMarkup, InlineKeyboardButton,
                            ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import ADMIN_IDS, SERVERS
from database import (add_client, get_all_clients, get_client_by_id,
                      update_payment_status, set_client_active, log_payment)
from ssh_manager import (get_server_status, get_all_peers_merged, add_peer,
                         remove_peer, disable_peer, enable_peer,
                         ping_server, speed_test)
from scheduler import notify_payment_claimed

router = Router()
logger = logging.getLogger(__name__)

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ─── Главное меню (ReplyKeyboard) ─────────────────────────────────────────────

def admin_main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👥 Клиенты"), KeyboardButton(text="🖥 Серверы")],
            [KeyboardButton(text="➕ Добавить клиента"), KeyboardButton(text="📊 Статистика")],
        ],
        resize_keyboard=True,
    )

def back_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="◀️ Назад"), KeyboardButton(text="🏠 Главное меню")]],
        resize_keyboard=True,
    )

# ─── /start и главное меню ────────────────────────────────────────────────────

@router.message(Command("start"))
@router.message(F.text == "🏠 Главное меню")
async def cmd_main_menu(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.clear()
    await msg.answer(
        "👋 <b>Добро пожаловать, админ!</b>\n\nВыбери раздел:",
        parse_mode="HTML",
        reply_markup=admin_main_kb()
    )

@router.message(Command("menu"))
async def cmd_menu(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await cmd_main_menu(msg, state)

# ─── Клиенты ──────────────────────────────────────────────────────────────────

@router.message(F.text == "👥 Клиенты")
@router.message(Command("clients"))
async def show_clients(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    clients = await get_all_clients()
    if not clients:
        await msg.answer("Клиентов нет.", reply_markup=admin_main_kb())
        return

    text = "<b>👥 Все клиенты:</b>\n\n"
    for c in clients:
        s = {"paid": "✅", "pending": "⏳", "waiting_confirm": "🔄", "overdue": "🔴"}.get(c.payment_status, "❓")
        a = "🟢" if c.active else "🔴"
        text += f"{a} <b>{c.name}</b> | {s} | {c.server_name} | {c.monthly_fee:.0f}₽\n"

    # Inline кнопки для каждого клиента
    buttons = [[InlineKeyboardButton(
        text=f"{'🟢' if c.active else '🔴'} {c.name}",
        callback_data=f"client_card:{c.id}"
    )] for c in clients]

    await msg.answer(text, parse_mode="HTML",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("client_card:"))
async def client_card(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer("Клиент не найден")
        return

    peer_info = ""
    if client.wg_pubkey:
        from ssh_manager import get_peer_status
        p = await get_peer_status(client.server_name, client.wg_pubkey)
        conn = "🟢 онлайн" if p["connected"] else "🔴 офлайн"
        peer_info = f"\n📡 VPN: {conn} | ↓{p['rx_mb']} MB ↑{p['tx_mb']} MB"

    text = (
        f"<b>👤 {client.name}</b>\n"
        f"TG: <code>{client.telegram_id}</code> | @{client.username or '-'}\n"
        f"Сервер: {client.server_name}\n"
        f"Устройств: {client.devices} | {client.monthly_fee:.0f} ₽/мес\n"
        f"Статус: {client.payment_status} | {'активен' if client.active else 'отключён'}\n"
        f"Последняя оплата: {client.payment_date or '—'}"
        f"{peer_info}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔴 Отключить" if client.active else "🟢 Включить",
                callback_data=f"toggle_client:{client.id}"
            ),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_client:{client.id}"),
        ],
        [InlineKeyboardButton(text="◀️ К списку", callback_data="back_to_clients")],
    ])
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "back_to_clients")
async def back_to_clients(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    clients = await get_all_clients()
    buttons = [[InlineKeyboardButton(
        text=f"{'🟢' if c.active else '🔴'} {c.name}",
        callback_data=f"client_card:{c.id}"
    )] for c in clients]
    await cb.message.edit_text("<b>👥 Клиенты:</b>", parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# ─── Серверы ──────────────────────────────────────────────────────────────────

@router.message(F.text == "🖥 Серверы")
@router.message(Command("servers"))
async def show_servers(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    buttons = [
        [InlineKeyboardButton(text=f"🖥 {s.name}", callback_data=f"srv:{s.name}")]
        for s in SERVERS
    ] + [[
        InlineKeyboardButton(text="📶 Пинг всех", callback_data="ping_all"),
        InlineKeyboardButton(text="⚡ Скорость всех", callback_data="speed_all"),
    ]]
    await msg.answer("Выбери сервер:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("srv:"))
async def server_detail(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    server_name = cb.data.split(":", 1)[1]
    await cb.message.edit_text(f"⏳ Загружаю {server_name}...")

    status = await get_server_status(server_name)
    if not status.get("online", False):
        err = status.get("error", "недоступен")
        await cb.message.edit_text(
            f"❌ <b>{server_name}</b> недоступен\n{err}",
            parse_mode="HTML",
            reply_markup=_server_kb(server_name)
        )
        return

    peers = await get_all_peers_merged(server_name)
    online = sum(1 for p in peers if p["connected"])

    mem_pct = round(status["mem_used"] / status["mem_total"] * 100) if status["mem_total"] else 0
    text = (
        f"🖥 <b>{status['name']}</b> ({status['host']})\n"
        f"⏱ Uptime: {status['uptime']}\n"
        f"📊 Load: {' '.join(status['load'])}\n"
        f"💾 RAM: {status['mem_used']}/{status['mem_total']} MB ({mem_pct}%)\n"
        f"🔗 WireGuard: {'✅ работает' if status['wg_running'] else '❌ не запущен'}\n"
        f"👥 Клиентов: {status['peers_count']} (онлайн: {online})"
    )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=_server_kb(server_name))

def _server_kb(server_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👥 Peer'ы", callback_data=f"peers:{server_name}"),
            InlineKeyboardButton(text="📶 Пинг", callback_data=f"ping:{server_name}"),
        ],
        [
            InlineKeyboardButton(text="⚡ Скорость", callback_data=f"speed:{server_name}"),
            InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_servers"),
        ],
    ])

@router.callback_query(F.data == "back_to_servers")
async def back_to_servers(cb: CallbackQuery):
    buttons = [
        [InlineKeyboardButton(text=f"🖥 {s.name}", callback_data=f"srv:{s.name}")]
        for s in SERVERS
    ] + [[
        InlineKeyboardButton(text="📶 Пинг всех", callback_data="ping_all"),
        InlineKeyboardButton(text="⚡ Скорость всех", callback_data="speed_all"),
    ]]
    await cb.message.edit_text("Выбери сервер:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("peers:"))
async def show_peers(cb: CallbackQuery):
    server_name = cb.data.split(":", 1)[1]
    await cb.message.edit_text(f"⏳ Загружаю peer'ы {server_name}...")
    peers = await get_all_peers_merged(server_name)
    if not peers:
        await cb.message.edit_text("Peer'ов нет.",
                                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                        InlineKeyboardButton(text="◀️ Назад", callback_data=f"srv:{server_name}")
                                    ]]))
        return

    lines = [f"<b>Peer'ы {server_name}:</b>\n"]
    for p in peers:
        icon = "🟢" if p["connected"] else "🔴"
        lines.append(f"{icon} <b>{p['name']}</b> {p['ip']}\n   ↓{p['rx_mb']} ↑{p['tx_mb']} MB")

    await cb.message.edit_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"srv:{server_name}")
        ]])
    )

# ─── Пинг ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("ping:"))
async def do_ping(cb: CallbackQuery):
    server_name = cb.data.split(":", 1)[1]
    await cb.message.edit_text(f"⏳ Пингую {server_name}...")
    result = await ping_server(server_name)
    if result["success"] and result["ms"]:
        emoji = "🟢" if result["ms"] < 50 else ("🟡" if result["ms"] < 150 else "🔴")
        text = f"{emoji} <b>{server_name}</b>\nПинг: <b>{result['ms']:.1f} мс</b>"
    else:
        text = f"❌ <b>{server_name}</b> не отвечает"
    await cb.message.edit_text(text, parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                    InlineKeyboardButton(text="◀️ Назад", callback_data=f"srv:{server_name}")
                                ]]))

@router.callback_query(F.data == "ping_all")
async def ping_all(cb: CallbackQuery):
    await cb.message.edit_text("⏳ Пингую все серверы...")
    lines = ["<b>📶 Пинг серверов:</b>\n"]
    for s in SERVERS:
        result = await ping_server(s.name)
        if result["success"] and result["ms"]:
            emoji = "🟢" if result["ms"] < 50 else ("🟡" if result["ms"] < 150 else "🔴")
            lines.append(f"{emoji} {s.name}: <b>{result['ms']:.1f} мс</b>")
        else:
            lines.append(f"❌ {s.name}: недоступен")
    await cb.message.edit_text("\n".join(lines), parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                    InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_servers")
                                ]]))

# ─── Скорость ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("speed:"))
async def do_speed(cb: CallbackQuery):
    server_name = cb.data.split(":", 1)[1]
    await cb.message.edit_text(f"⏳ Тестирую скорость {server_name}... (может занять 30 сек)")
    result = await speed_test(server_name)
    if result["success"]:
        dl = result.get("download_mbps", "—")
        ul = result.get("upload_mbps", "—")
        ping = result.get("ping_ms", "—")
        method = result.get("method", "")
        text = (
            f"⚡ <b>{server_name}</b>\n"
            f"⬇️ Download: <b>{dl} Mbit/s</b>\n"
            f"⬆️ Upload: <b>{ul} Mbit/s</b>\n"
            f"📶 Ping: {ping} мс\n"
            f"<i>метод: {method}</i>"
        )
    else:
        text = f"❌ {server_name}: {result.get('error', 'ошибка')}"
    await cb.message.edit_text(text, parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                    InlineKeyboardButton(text="◀️ Назад", callback_data=f"srv:{server_name}")
                                ]]))

@router.callback_query(F.data == "speed_all")
async def speed_all(cb: CallbackQuery):
    await cb.message.edit_text("⏳ Тестирую скорость всех серверов... (до 90 сек)")
    lines = ["<b>⚡ Скорость серверов:</b>\n"]
    for s in SERVERS:
        result = await speed_test(s.name)
        if result["success"]:
            dl = result.get("download_mbps", "—")
            lines.append(f"✅ {s.name}: ⬇️ <b>{dl} Mbit/s</b>")
        else:
            lines.append(f"❌ {s.name}: {result.get('error', 'ошибка')}")
    await cb.message.edit_text("\n".join(lines), parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                    InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_servers")
                                ]]))

# ─── Статистика ───────────────────────────────────────────────────────────────

@router.message(F.text == "📊 Статистика")
async def show_stats(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    clients = await get_all_clients()
    total = len(clients)
    active = sum(1 for c in clients if c.active)
    paid = sum(1 for c in clients if c.payment_status == "paid")
    pending = sum(1 for c in clients if c.payment_status == "pending")
    waiting = sum(1 for c in clients if c.payment_status == "waiting_confirm")
    monthly = sum(c.monthly_fee for c in clients if c.active)

    text = (
        f"<b>📊 Статистика</b>\n\n"
        f"Всего клиентов: <b>{total}</b>\n"
        f"Активных: <b>{active}</b>\n\n"
        f"✅ Оплатили: <b>{paid}</b>\n"
        f"🔄 Ожидают подтверждения: <b>{waiting}</b>\n"
        f"⏳ Не оплатили: <b>{pending}</b>\n\n"
        f"💰 Ежемесячный доход: <b>{monthly:.0f} ₽</b>"
    )
    await msg.answer(text, parse_mode="HTML", reply_markup=admin_main_kb())

# ─── Добавление клиента (FSM) ─────────────────────────────────────────────────

class AddClientForm(StatesGroup):
    telegram_id = State()
    name = State()
    username = State()
    server = State()
    devices = State()
    fee = State()
    confirm = State()

@router.message(F.text == "➕ Добавить клиента")
@router.message(Command("add_client"))
async def cmd_add_client(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await msg.answer("Введи <b>Telegram ID</b> нового клиента:", parse_mode="HTML",
                     reply_markup=back_kb())
    await state.set_state(AddClientForm.telegram_id)

@router.message(F.text == "◀️ Назад")
async def go_back(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    current = await state.get_state()
    if current is None:
        await msg.answer("Ты уже в главном меню.", reply_markup=admin_main_kb())
        return
    # Определяем на какой шаг вернуться
    states_order = [
        AddClientForm.telegram_id, AddClientForm.name, AddClientForm.username,
        AddClientForm.server, AddClientForm.devices, AddClientForm.fee, AddClientForm.confirm
    ]
    prompts = [
        "Введи <b>Telegram ID</b>:",
        "Введи <b>имя</b> клиента:",
        "Введи <b>@username</b> (без @, или '-'):",
        "Выбери <b>сервер</b>:",
        "Сколько <b>устройств</b>?",
        "Введи <b>ежемесячную сумму</b> (₽):",
    ]
    try:
        idx = [str(s) for s in states_order].index(current)
    except ValueError:
        idx = 0
    if idx <= 0:
        await state.clear()
        await msg.answer("Добавление отменено.", reply_markup=admin_main_kb())
        return
    prev_state = states_order[idx - 1]
    await state.set_state(prev_state)
    await msg.answer(prompts[idx - 1], parse_mode="HTML", reply_markup=back_kb())

@router.message(AddClientForm.telegram_id)
async def add_tg_id(msg: Message, state: FSMContext):
    if msg.text in ("◀️ Назад", "🏠 Главное меню"):
        return
    try:
        await state.update_data(telegram_id=int(msg.text.strip()))
        await msg.answer("Введи <b>имя</b> клиента:", parse_mode="HTML", reply_markup=back_kb())
        await state.set_state(AddClientForm.name)
    except ValueError:
        await msg.answer("❌ Неверный формат. Введи число.")

@router.message(AddClientForm.name)
async def add_name(msg: Message, state: FSMContext):
    if msg.text in ("◀️ Назад", "🏠 Главное меню"):
        return
    await state.update_data(name=msg.text.strip())
    await msg.answer("Введи <b>@username</b> (без @, или '-' если нет):", parse_mode="HTML",
                     reply_markup=back_kb())
    await state.set_state(AddClientForm.username)

@router.message(AddClientForm.username)
async def add_username(msg: Message, state: FSMContext):
    if msg.text in ("◀️ Назад", "🏠 Главное меню"):
        return
    username = msg.text.strip().lstrip("@")
    await state.update_data(username=None if username == "-" else username)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=s.name, callback_data=f"sel_srv:{s.name}")]
        for s in SERVERS
    ])
    await msg.answer("Выбери <b>сервер</b>:", parse_mode="HTML", reply_markup=kb)
    await state.set_state(AddClientForm.server)

@router.callback_query(AddClientForm.server, F.data.startswith("sel_srv:"))
async def add_server(cb: CallbackQuery, state: FSMContext):
    await state.update_data(server=cb.data.split(":", 1)[1])
    await cb.message.edit_text(f"Сервер: <b>{cb.data.split(':', 1)[1]}</b>", parse_mode="HTML")
    await cb.message.answer("Сколько <b>устройств</b>?", parse_mode="HTML", reply_markup=back_kb())
    await state.set_state(AddClientForm.devices)

@router.message(AddClientForm.devices)
async def add_devices(msg: Message, state: FSMContext):
    if msg.text in ("◀️ Назад", "🏠 Главное меню"):
        return
    try:
        await state.update_data(devices=int(msg.text.strip()))
        await msg.answer("Введи <b>ежемесячную сумму</b> (₽):", parse_mode="HTML",
                         reply_markup=back_kb())
        await state.set_state(AddClientForm.fee)
    except ValueError:
        await msg.answer("❌ Введи число.")

@router.message(AddClientForm.fee)
async def add_fee(msg: Message, state: FSMContext):
    if msg.text in ("◀️ Назад", "🏠 Главное меню"):
        return
    try:
        fee = float(msg.text.strip())
        data = await state.update_data(fee=fee)
        summary = (
            f"<b>Проверь данные:</b>\n\n"
            f"Telegram ID: <code>{data['telegram_id']}</code>\n"
            f"Имя: {data['name']}\n"
            f"Username: @{data.get('username') or '-'}\n"
            f"Сервер: {data['server']}\n"
            f"Устройств: {data['devices']}\n"
            f"Оплата: {fee:.0f} ₽/мес\n\n"
            f"Создать WireGuard конфиг автоматически?"
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
async def add_confirm(cb: CallbackQuery, state: FSMContext):
    if cb.data == "add_cancel":
        await state.clear()
        await cb.message.edit_text("❌ Отменено.")
        await cb.message.answer("Главное меню:", reply_markup=admin_main_kb())
        return

    data = await state.get_data()
    await state.clear()
    await cb.message.edit_text("⏳ Создаю клиента...")

    wg_data = None
    if cb.data == "add_with_wg":
        wg_data = await add_peer(data["server"], data["name"])
        if not wg_data:
            await cb.message.answer("⚠️ Не удалось создать WireGuard конфиг. Добавляю без конфига.")

    client_id = await add_client(
        telegram_id=data["telegram_id"], name=data["name"],
        username=data.get("username"), server_name=data["server"],
        devices=data["devices"], monthly_fee=data["fee"],
        wg_pubkey=wg_data["pubkey"] if wg_data else None,
        wg_peer_id=wg_data["pubkey"] if wg_data else None,
    )

    await cb.message.edit_text(f"✅ <b>{data['name']}</b> добавлен!", parse_mode="HTML")
    await cb.message.answer("Главное меню:", reply_markup=admin_main_kb())

    if wg_data:
        config_file = io.BytesIO(wg_data["config_text"].encode())
        config_file.name = f"vpn_{data['name'].replace(' ', '_')}.conf"
        await cb.message.answer_document(
            config_file,
            caption=f"🔑 Конфиг для <b>{data['name']}</b>\nIP: {wg_data['client_ip']}",
            parse_mode="HTML"
        )

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
    await cb.message.edit_text(f"✅ Оплата от <b>{client.name}</b> подтверждена.", parse_mode="HTML")
    try:
        await cb.bot.send_message(client.telegram_id,
            f"✅ <b>Оплата подтверждена!</b>\nСпасибо, {client.name}. VPN активен до конца месяца.",
            parse_mode="HTML")
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
    await cb.message.edit_text(f"❌ Оплата от <b>{client.name}</b> отклонена.", parse_mode="HTML")
    try:
        await cb.bot.send_message(client.telegram_id,
            f"❌ <b>Оплата не подтверждена.</b>\nПроверь правильность перевода и попробуй снова.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid:{client.id}")
            ]]))
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
            await enable_peer(client.server_name, client.wg_pubkey,
                              client.wg_peer_id or "10.8.1.2/32")
        else:
            await disable_peer(client.server_name, client.wg_pubkey)
    status = "включён 🟢" if new_state else "отключён 🔴"
    await cb.answer(f"{client.name} {status}")
    await client_card(cb)

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
        InlineKeyboardButton(text="❌ Нет", callback_data=f"client_card:{client_id}"),
    ]])
    await cb.message.edit_text(
        f"Удалить <b>{client.name}</b> и его peer с сервера?",
        parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("del_confirm:"))
async def delete_confirm(cb: CallbackQuery):
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
    await cb.message.edit_text(f"🗑 <b>{client.name}</b> удалён.", parse_mode="HTML")
    