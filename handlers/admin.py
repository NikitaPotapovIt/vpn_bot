"""Обработчики администратора: клиенты, серверы, ключи и оплаты"""

import io
import calendar
import logging
import html
from datetime import datetime, date, timedelta
from typing import Optional, Tuple, List

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import ADMIN_IDS, SERVERS, DEVICE_MONTHLY_PRICE
from database import (
    add_client,
    get_all_clients,
    get_client_by_id,
    get_client_by_tg,
    update_payment_status,
    set_paid_until,
    set_client_active,
    log_payment,
    sync_server_keys,
    get_client_keys,
    get_unlinked_keys,
    get_linkable_keys,
    get_key_by_id,
    get_key_access_clients,
    is_key_linked_to_client,
    is_key_linked_any,
    upsert_client_key,
    assign_key_to_client,
    unassign_key,
    set_key_payer,
    set_key_billing_client,
    delete_client_record,
)
from ssh_manager import (
    get_server_status,
    get_all_peers_merged,
    add_peer,
    remove_peer,
    disable_peer,
    enable_peer,
    ping_server,
    speed_test_host,
    speed_test_vpn,
    speed_test_both,
    reboot_server,
)

router = Router()
logger = logging.getLogger(__name__)


# ─── Общие помощники ──────────────────────────────────────────────────────────


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


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


def _with_home(rows: List[List[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=rows + [[InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_home")]]
    )


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


def _format_last_seen(last_handshake: int) -> str:
    if not last_handshake:
        return "никогда"
    return datetime.fromtimestamp(last_handshake).strftime("%d.%m.%Y %H:%M")


def _client_status(client) -> str:
    if client.monthly_fee <= 0:
        return "🚫 оплата не требуется"
    today = date.today()
    paid_until = _parse_date(client.paid_until)
    if paid_until and paid_until >= today:
        return f"✅ до {paid_until.strftime('%d.%m.%Y')}"

    mapping = {
        "paid": "✅ оплачено",
        "pending": "⏳ ожидает оплаты",
        "waiting_confirm": "🔄 проверяется",
        "overdue": "🔴 просрочено",
    }
    return mapping.get(client.payment_status, client.payment_status)


def _device_price_text() -> str:
    return f"{DEVICE_MONTHLY_PRICE:.0f} ₽"


def _add_months(src: date, months: int) -> date:
    month = src.month - 1 + months
    year = src.year + month // 12
    month = month % 12 + 1
    day = min(src.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _extend_paid_until(current_paid_until: Optional[str], months: int) -> str:
    today = date.today()
    current = _parse_date(current_paid_until)
    if current and current >= today:
        start = current + timedelta(days=1)
    else:
        start = today
    new_until = _add_months(start, months) - timedelta(days=1)
    return new_until.strftime("%Y-%m-%d")


async def _sync_peers_all_servers() -> Tuple[str, int]:
    """Импорт peer'ов с серверов в БД.

    Возвращает (сообщение для UI, количество новых ключей)
    """
    lines = []
    total_new = 0
    for server in SERVERS:
        try:
            peers = await get_all_peers_merged(server.name)
            stats = await sync_server_keys(server.name, peers)
            total_new += stats["added"]
            lines.append(
                f"• {server.name}: {stats['total']} ключей, +{stats['added']} новых"
            )
        except Exception as e:
            logger.exception("sync failed for %s", server.name)
            lines.append(f"• {server.name}: ошибка ({e})")

    return "\n".join(lines), total_new


async def _build_clients_view_text() -> Tuple[str, InlineKeyboardMarkup]:
    clients = await get_all_clients()
    unlinked = await get_unlinked_keys()

    if not clients:
        text = (
            "<b>👥 Клиенты</b>\n\n"
            "Клиентов пока нет.\n"
            f"Непривязанные ключи: <b>{len(unlinked)}</b>"
        )
        kb = _with_home([
            [InlineKeyboardButton(text="➕ Создать клиента", callback_data="add_client_inline")],
            [InlineKeyboardButton(text="🔄 Импорт peer'ов", callback_data="sync_peers_now")],
            [InlineKeyboardButton(text="🧩 Непривязанные ключи", callback_data="show_unlinked_info")],
        ])
        return text, kb

    text_lines = ["<b>👥 Клиенты:</b>", ""]
    for c in clients:
        active_icon = "🟢" if c.active else "🔴"
        status = _client_status(c)
        text_lines.append(
            f"{active_icon} <b>{c.name}</b> | {status} | "
            f"ключей: {c.key_count} ({c.payable_key_count} платн.) | {c.monthly_fee:.0f} ₽"
        )

    text_lines.append("")
    text_lines.append(f"🧩 Непривязанных ключей: <b>{len(unlinked)}</b>")

    rows = [
        [InlineKeyboardButton(text="➕ Создать клиента", callback_data="add_client_inline")],
        [InlineKeyboardButton(text="🔄 Импорт peer'ов", callback_data="sync_peers_now")],
        [InlineKeyboardButton(text="🧩 Непривязанные ключи", callback_data="show_unlinked_info")],
    ]
    rows.extend(
        [
            InlineKeyboardButton(
                text=f"{'🟢' if c.active else '🔴'} {c.name}",
                callback_data=f"client_card:{c.id}",
            )
        ]
        for c in clients
    )

    return "\n".join(text_lines), _with_home(rows)


async def _render_client_card(client_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    client = await get_client_by_id(client_id)
    if not client:
        return "❌ Клиент не найден", _with_home([[InlineKeyboardButton(text="◀️ К клиентам", callback_data="back_to_clients")]])

    keys = await get_client_keys(client.id)
    active_keys = [k for k in keys if k.active]
    online_count = sum(1 for k in active_keys if k.connected)
    last_hs = max((k.last_handshake for k in keys), default=0)

    if online_count > 0:
        vpn_line = f"🟢 онлайн устройств: {online_count}/{len(active_keys) or len(keys)}"
    else:
        vpn_line = f"🔴 офлайн | был онлайн: {_format_last_seen(last_hs)}"

    paid_until = _parse_date(client.paid_until)
    paid_until_text = paid_until.strftime("%d.%m.%Y") if paid_until else "—"

    text = (
        f"<b>👤 {client.name}</b>\n"
        f"TG: <code>{client.telegram_id}</code> | @{client.username or '-'}\n"
        f"Сервер (основной): {client.server_name}\n"
        f"{vpn_line}\n"
        f"Ключей: {client.key_count} | платных: {client.payable_key_count} | неплательщиков: {client.nonpayable_key_count}\n"
        f"Тариф: <b>{_device_price_text()}</b> за устройство\n"
        f"К оплате в месяц: <b>{client.monthly_fee:.0f} ₽</b>\n"
        f"Статус оплаты: {_client_status(client)}\n"
        f"Оплачено до: <b>{paid_until_text}</b>\n"
        f"Последняя оплата: {client.payment_date or '—'}\n"
        f"VPN клиента: {'активен' if client.active else 'отключён'}"
    )

    kb = _with_home([
        [
            InlineKeyboardButton(
                text="🔴 Отключить" if client.active else "🟢 Включить",
                callback_data=f"toggle_client:{client.id}",
            ),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_client:{client.id}"),
        ],
        [
            InlineKeyboardButton(text="🔑 Ключи", callback_data=f"client_keys:{client.id}"),
            InlineKeyboardButton(text="💳 Подтвердить оплату", callback_data=f"pay_choose:{client.id}"),
        ],
        [InlineKeyboardButton(text="◀️ К списку", callback_data="back_to_clients")],
    ])
    return text, kb


async def _show_months_selector(message, client_id: int, edit: bool = True):
    client = await get_client_by_id(client_id)
    if not client:
        text = "❌ Клиент не найден"
        if edit:
            await message.edit_text(text)
        else:
            await message.answer(text)
        return

    text = (
        f"<b>💳 Подтверждение оплаты</b>\n\n"
        f"Клиент: <b>{client.name}</b>\n"
        f"Платных ключей: <b>{client.payable_key_count}</b>\n"
        f"Тариф: {_device_price_text()} за устройство\n"
        f"Ежемесячно: <b>{client.monthly_fee:.0f} ₽</b>\n\n"
        f"Выбери срок оплаты:"
    )

    kb = _with_home([
        [
            InlineKeyboardButton(text="1 мес", callback_data=f"confirm_pay_do:{client_id}:1"),
            InlineKeyboardButton(text="2 мес", callback_data=f"confirm_pay_do:{client_id}:2"),
            InlineKeyboardButton(text="3 мес", callback_data=f"confirm_pay_do:{client_id}:3"),
        ],
        [
            InlineKeyboardButton(text="6 мес", callback_data=f"confirm_pay_do:{client_id}:6"),
            InlineKeyboardButton(text="12 мес", callback_data=f"confirm_pay_do:{client_id}:12"),
        ],
        [
            InlineKeyboardButton(text="✍️ Другое", callback_data=f"confirm_pay_custom:{client_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_pay:{client_id}"),
        ],
        [InlineKeyboardButton(text="◀️ К клиенту", callback_data=f"client_card:{client_id}")],
    ])

    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _apply_payment_confirmation(bot, client_id: int, months: int, source_message):
    client = await get_client_by_id(client_id)
    if not client:
        await source_message.edit_text("❌ Клиент не найден")
        return

    months = max(1, months)
    new_paid_until = _extend_paid_until(client.paid_until, months)
    monthly_amount = float(client.monthly_fee)
    total_amount = monthly_amount * months

    await set_paid_until(client_id, new_paid_until)
    await update_payment_status(client_id, "paid", datetime.now().strftime("%Y-%m-%d"))
    await log_payment(
        client_id,
        "confirmed",
        total_amount,
        note=(
            f"months={months}; monthly={monthly_amount:.0f}; "
            f"devices_payable={client.payable_key_count}; paid_until={new_paid_until}"
        ),
    )

    until_dt = _parse_date(new_paid_until)
    until_text = until_dt.strftime("%d.%m.%Y") if until_dt else new_paid_until

    await source_message.edit_text(
        (
            f"✅ Оплата подтверждена\n\n"
            f"Клиент: <b>{client.name}</b>\n"
            f"Срок: <b>{months} мес.</b>\n"
            f"Сумма: <b>{total_amount:.0f} ₽</b>\n"
            f"Оплачено до: <b>{until_text}</b>"
        ),
        parse_mode="HTML",
        reply_markup=_with_home([
            [InlineKeyboardButton(text="👤 Открыть клиента", callback_data=f"client_card:{client.id}")]
        ]),
    )

    try:
        await bot.send_message(
            client.telegram_id,
            (
                f"✅ <b>Оплата подтверждена!</b>\n"
                f"Срок: <b>{months} мес.</b>\n"
                f"Оплачено до: <b>{until_text}</b>\n"
                f"Подключённых платных устройств: <b>{client.payable_key_count}</b>\n"
                f"Спасибо, {client.name}."
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("failed to notify client %s", client.id)


# ─── Главное меню ─────────────────────────────────────────────────────────────


@router.message(Command("start"))
@router.message(F.text == "🏠 Главное меню")
async def cmd_main_menu(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.clear()
    await msg.answer(
        "👋 <b>Добро пожаловать, админ!</b>\n\nВыбери раздел:",
        parse_mode="HTML",
        reply_markup=admin_main_kb(),
    )


@router.message(Command("menu"))
async def cmd_menu(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await cmd_main_menu(msg, state)


@router.callback_query(F.data == "menu_home")
async def cb_menu_home(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.clear()
    await cb.message.edit_text("🏠 Возврат в главное меню.")
    await cb.message.answer("Выбери раздел:", reply_markup=admin_main_kb())
    await cb.answer()


# ─── Клиенты ──────────────────────────────────────────────────────────────────


@router.message(F.text == "👥 Клиенты")
@router.message(Command("clients"))
async def show_clients(msg: Message):
    if not is_admin(msg.from_user.id):
        return

    await msg.answer("⏳ Обновляю peer'ы с серверов...")
    sync_text, _ = await _sync_peers_all_servers()

    text, kb = await _build_clients_view_text()
    await msg.answer(f"{text}\n\n<i>{html.escape(sync_text)}</i>", parse_mode="HTML", reply_markup=kb)


@router.message(Command("client"))
async def cmd_client(msg: Message):
    if not is_admin(msg.from_user.id):
        return

    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) == 1:
        await show_clients(msg)
        return

    try:
        client_id = int(parts[1].strip())
    except ValueError:
        await msg.answer("Использование: /client <id>")
        return

    await _sync_peers_all_servers()
    text, kb = await _render_client_card(client_id)
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "sync_peers_now")
async def sync_peers_now(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.edit_text("⏳ Импортирую peer'ы...")
    sync_text, _ = await _sync_peers_all_servers()
    text, kb = await _build_clients_view_text()
    await cb.message.edit_text(f"{text}\n\n<i>{html.escape(sync_text)}</i>", parse_mode="HTML", reply_markup=kb)
    await cb.answer("Синхронизировано")


@router.callback_query(F.data == "show_unlinked_info")
async def show_unlinked_info(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    keys = await get_unlinked_keys()
    if not keys:
        text = "🧩 Непривязанных ключей нет."
        await cb.message.edit_text(
            text,
            reply_markup=_with_home([[InlineKeyboardButton(text="◀️ К клиентам", callback_data="back_to_clients")]]),
        )
        return

    lines = [
        "<b>🧩 Непривязанные ключи</b>",
        "",
        f"Всего: <b>{len(keys)}</b>",
        "Можно создать клиента сразу из ключа, либо привязать к существующему клиенту.",
        "",
    ]

    for k in keys[:20]:
        online = "🟢" if k.connected else "🔴"
        payer = "💰" if k.payer else "🚫"
        lines.append(
            f"{online}{payer} <b>{k.key_name or 'Без имени'}</b> | {k.server_name} | {k.allowed_ips or '—'}"
        )

    if len(keys) > 20:
        lines.append(f"\n... и ещё {len(keys) - 20} ключей")

    rows = [[InlineKeyboardButton(text="➕ Создать клиента (вручную)", callback_data="add_client_inline")]]
    for k in keys[:10]:
        online = "🟢" if k.connected else "🔴"
        label = f"{online}➕ {k.key_name or k.wg_pubkey[:8]} | {k.server_name}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"create_from_key:{k.id}")])
    rows.append([InlineKeyboardButton(text="◀️ К клиентам", callback_data="back_to_clients")])

    await cb.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_with_home(rows),
    )


@router.callback_query(F.data == "back_to_clients")
async def back_to_clients(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    text, kb = await _build_clients_view_text()
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("client_card:"))
async def client_card(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    client_id = int(cb.data.split(":")[1])
    text, kb = await _render_client_card(client_id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("toggle_client:"))
async def toggle_client(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer("Клиент не найден")
        return

    new_state = not client.active
    await set_client_active(client_id, new_state)

    errors = 0
    keys = await get_client_keys(client_id)
    for key in keys:
        try:
            if new_state:
                if key.allowed_ips:
                    await enable_peer(key.server_name, key.wg_pubkey, key.allowed_ips)
            else:
                await disable_peer(key.server_name, key.wg_pubkey)
        except Exception:
            errors += 1

    status = "включён 🟢" if new_state else "отключён 🔴"
    suffix = "" if errors == 0 else f" ({errors} ключей с ошибкой)"
    await cb.answer(f"{client.name} {status}{suffix}")
    await client_card(cb)


@router.callback_query(F.data.startswith("del_client:"))
async def delete_client(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer("Клиент не найден")
        return

    kb = _with_home([
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"del_confirm:{client_id}"),
            InlineKeyboardButton(text="❌ Нет", callback_data=f"client_card:{client_id}"),
        ]
    ])
    await cb.message.edit_text(
        (
            f"Удалить <b>{client.name}</b> из БД?\n\n"
            "Ключи на сервере останутся и станут непривязанными."
        ),
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("del_confirm:"))
async def delete_confirm(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer("Клиент не найден")
        return

    await delete_client_record(client_id)
    await cb.message.edit_text(
        f"🗑 <b>{client.name}</b> удалён. Ключи отвязаны.",
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="◀️ К клиентам", callback_data="back_to_clients")]]),
    )


# ─── Ключи клиента ────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("client_keys:"))
async def client_keys(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer("Клиент не найден")
        return

    keys = await get_client_keys(client_id)
    if not keys:
        text = (
            f"<b>🔑 Ключи клиента {client.name}</b>\n\n"
            "Ключей пока нет.\n"
            "Импортируй peer'ы и привяжи ключи."
        )
        kb = _with_home([
            [InlineKeyboardButton(text="🆕 Создать ключ на сервере", callback_data=f"create_key_pick_server:{client_id}")],
            [InlineKeyboardButton(text="➕ Привязать ключ", callback_data=f"link_key_pick:{client_id}:0")],
            [InlineKeyboardButton(text="◀️ К клиенту", callback_data=f"client_card:{client_id}")],
        ])
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        return

    lines = [
        f"<b>🔑 Ключи клиента {client.name}</b>",
        "",
        f"Всего: <b>{len(keys)}</b> | платных: <b>{sum(1 for k in keys if k.payer)}</b>",
        f"К оплате: <b>{client.monthly_fee:.0f} ₽/мес</b>",
        "",
    ]

    rows = []
    for key in keys:
        state = "🟢" if key.connected else "🔴"
        payer = "💰" if key.payer else "🚫"
        label = f"{state}{payer} {key.key_name or key.wg_pubkey[:8]}"
        rows.append([
            InlineKeyboardButton(text=label[:60], callback_data=f"key_card:{key.id}:{client_id}")
        ])

    rows.append([InlineKeyboardButton(text="🆕 Создать ключ на сервере", callback_data=f"create_key_pick_server:{client_id}")])
    rows.append([InlineKeyboardButton(text="➕ Привязать ключ", callback_data=f"link_key_pick:{client_id}:0")])
    rows.append([InlineKeyboardButton(text="◀️ К клиенту", callback_data=f"client_card:{client_id}")])

    await cb.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_with_home(rows))


@router.callback_query(F.data.startswith("create_key_pick_server:"))
async def create_key_pick_server(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer("Клиент не найден")
        return

    rows = [
        [InlineKeyboardButton(text=f"🖥 {s.name}", callback_data=f"create_key_do:{client_id}:{s.name}")]
        for s in SERVERS
    ]
    rows.append([InlineKeyboardButton(text="◀️ К ключам", callback_data=f"client_keys:{client_id}")])

    await cb.message.edit_text(
        (
            f"<b>🆕 Создание нового ключа</b>\n\n"
            f"Клиент: <b>{client.name}</b>\n"
            f"Выбери сервер, где создать ключ:"
        ),
        parse_mode="HTML",
        reply_markup=_with_home(rows),
    )


@router.callback_query(F.data.startswith("create_key_do:"))
async def create_key_do(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    _, client_id_str, server_name = cb.data.split(":", 2)
    client_id = int(client_id_str)
    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer("Клиент не найден")
        return

    peer_name = f"{client.name}_{datetime.now().strftime('%d%m_%H%M')}"
    await cb.message.edit_text(f"⏳ Создаю ключ для <b>{client.name}</b> на сервере <b>{server_name}</b>...", parse_mode="HTML")
    wg_data = await add_peer(server_name, peer_name)
    if not wg_data:
        await cb.message.edit_text(
            f"❌ Не удалось создать ключ на сервере {server_name}.",
            reply_markup=_with_home([[InlineKeyboardButton(text="◀️ К ключам", callback_data=f"client_keys:{client_id}")]]),
        )
        return

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await upsert_client_key(
        server_name=server_name,
        wg_pubkey=wg_data["pubkey"],
        key_name=peer_name,
        allowed_ips=wg_data["client_ip"],
        created_at=created_at,
        connected=False,
        last_handshake=0,
        rx_bytes=0,
        tx_bytes=0,
        endpoint="",
        active=True,
        payer=True,
        client_id=client_id,
    )

    file_name = f"vpn_client{client.id}_{server_name}_{datetime.now().strftime('%Y%m%d_%H%M')}.conf"
    admin_config = io.BytesIO(wg_data["config_text"].encode())
    admin_config.name = file_name
    await cb.message.answer_document(
        admin_config,
        caption=(
            f"🔑 Новый ключ для <b>{client.name}</b>\n"
            f"Сервер: {server_name}\n"
            f"IP: {wg_data['client_ip']}"
        ),
        parse_mode="HTML",
    )

    delivered_to_client = False
    try:
        client_config = io.BytesIO(wg_data["config_text"].encode())
        client_config.name = file_name
        await cb.bot.send_document(
            client.telegram_id,
            client_config,
            caption=(
                f"🔑 <b>Твой новый VPN-ключ</b>\n"
                f"Сервер: {server_name}\n"
                f"IP: {wg_data['client_ip']}\n"
                f"Название: {peer_name}"
            ),
            parse_mode="HTML",
        )
        delivered_to_client = True
    except Exception:
        logger.exception("failed to send new key to client %s", client.id)

    delivery_text = "и клиенту" if delivered_to_client else "клиенту не доставлен (проверь, писал ли он боту)"
    await cb.message.edit_text(
        (
            f"✅ Ключ создан и отправлен админу.\n"
            f"Отправка клиенту: <b>{delivery_text}</b>"
        ),
        parse_mode="HTML",
        reply_markup=_with_home([
            [InlineKeyboardButton(text="🔑 Ключи клиента", callback_data=f"client_keys:{client_id}")],
            [InlineKeyboardButton(text="👤 Карточка клиента", callback_data=f"client_card:{client_id}")],
        ]),
    )


@router.callback_query(F.data.startswith("key_card:"))
async def key_card(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    _, key_id_str, client_id_str = cb.data.split(":")
    key_id = int(key_id_str)
    client_id = int(client_id_str)

    key = await get_key_by_id(key_id)
    if not key:
        await cb.answer("Ключ не найден")
        return

    if not await is_key_linked_to_client(key_id, client_id):
        await cb.answer("У этого клиента нет доступа к ключу", show_alert=True)
        return

    conn_text = "🟢 онлайн" if key.connected else f"🔴 офлайн (был: {_format_last_seen(key.last_handshake)})"
    if not key.payer:
        payer_text = "🚫 Неплательщик"
    elif key.billing_client_id == client_id:
        payer_text = "💰 Плательщик: этот клиент"
    elif key.billing_client_name:
        payer_text = f"💰 Плательщик: {key.billing_client_name}"
    else:
        payer_text = "💰 Плательщик не назначен"

    linked_clients = await get_key_access_clients(key_id)
    linked_names = ", ".join(c.name for c in linked_clients[:4])
    if len(linked_clients) > 4:
        linked_names += f" +{len(linked_clients) - 4}"

    text = (
        f"<b>🔑 Ключ</b>\n"
        f"Имя: <b>{key.key_name or 'Без имени'}</b>\n"
        f"Сервер: {key.server_name}\n"
        f"IP: {key.allowed_ips or '—'}\n"
        f"Статус: {conn_text}\n"
        f"Оплата: {payer_text}\n"
        f"Доступ у клиентов: <b>{len(linked_clients)}</b> ({linked_names or '—'})\n"
        f"RX/TX: {round(key.rx_bytes / 1_048_576, 2)} / {round(key.tx_bytes / 1_048_576, 2)} MB\n"
        f"PubKey: <code>{key.wg_pubkey}</code>"
    )

    rows = [[
        InlineKeyboardButton(
            text="🚫 Сделать неплательщиком" if key.payer else "💰 Сделать платным",
            callback_data=f"key_toggle_payer:{key.id}:{client_id}",
        ),
        InlineKeyboardButton(text="🔓 Отвязать", callback_data=f"key_unlink:{key.id}:{client_id}"),
    ]]
    if key.payer and key.billing_client_id != client_id:
        rows.append([InlineKeyboardButton(
            text="👤 Назначить плательщиком этого клиента",
            callback_data=f"key_set_billing:{key.id}:{client_id}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ К ключам", callback_data=f"client_keys:{client_id}")])

    kb = _with_home(rows)

    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("key_toggle_payer:"))
async def key_toggle_payer(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    _, key_id_str, client_id_str = cb.data.split(":")
    key_id = int(key_id_str)
    client_id = int(client_id_str)

    key = await get_key_by_id(key_id)
    if not key:
        await cb.answer("Ключ не найден")
        return

    new_payer = not key.payer
    await set_key_payer(key_id, new_payer)
    if new_payer and not key.billing_client_id:
        await set_key_billing_client(key_id, client_id)
    await cb.answer("Статус плательщика обновлён")
    await key_card(cb)


@router.callback_query(F.data.startswith("key_set_billing:"))
async def key_set_billing(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    _, key_id_str, client_id_str = cb.data.split(":")
    key_id = int(key_id_str)
    client_id = int(client_id_str)

    if not await is_key_linked_to_client(key_id, client_id):
        await cb.answer("Сначала привяжи ключ к клиенту", show_alert=True)
        return

    await set_key_billing_client(key_id, client_id)
    await set_key_payer(key_id, True)
    await cb.answer("Плательщик назначен")
    await key_card(cb)


@router.callback_query(F.data.startswith("key_unlink:"))
async def key_unlink(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    _, key_id_str, client_id_str = cb.data.split(":")
    key_id = int(key_id_str)
    client_id = int(client_id_str)

    await unassign_key(key_id, client_id)
    key_still_linked = await is_key_linked_any(key_id)
    await cb.answer("Ключ отвязан")
    await cb.message.edit_text(
        "✅ Доступ клиента к ключу удалён."
        + ("\nКлюч всё ещё связан с другими клиентами." if key_still_linked else "\nКлюч стал непривязанным."),
        reply_markup=_with_home([
            [InlineKeyboardButton(text="◀️ К ключам клиента", callback_data=f"client_keys:{client_id}")],
            [InlineKeyboardButton(text="🧩 Непривязанные ключи", callback_data="show_unlinked_info")],
        ]),
    )


@router.callback_query(F.data.startswith("link_key_pick:"))
async def link_key_pick(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    _, client_id_str, page_str = cb.data.split(":")
    client_id = int(client_id_str)
    page = int(page_str)

    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer("Клиент не найден")
        return

    keys = await get_linkable_keys(client_id)
    if not keys:
        await cb.message.edit_text(
            "Нет доступных ключей для привязки.\n(Все ключи уже связаны с этим клиентом.)",
            reply_markup=_with_home([
                [InlineKeyboardButton(text="◀️ К ключам", callback_data=f"client_keys:{client_id}")]
            ]),
        )
        return

    per_page = 12
    total_pages = (len(keys) + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))

    start = page * per_page
    chunk = keys[start:start + per_page]

    lines = [
        f"<b>🔗 Привязка ключа к {client.name}</b>",
        "",
        f"Доступно для привязки: <b>{len(keys)}</b>",
        f"Страница: <b>{page + 1}/{total_pages}</b>",
    ]

    rows = []
    for key in chunk:
        state = "🟢" if key.connected else "🔴"
        payer = "💰" if key.payer else "🚫"
        linked = f"👥{key.linked_clients}" if key.linked_clients else "🧩"
        label = f"{state}{payer}{linked} {key.key_name or key.wg_pubkey[:8]} | {key.server_name}"
        rows.append([
            InlineKeyboardButton(text=label[:60], callback_data=f"link_key_do:{client_id}:{key.id}")
        ])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"link_key_pick:{client_id}:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"link_key_pick:{client_id}:{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton(text="◀️ К ключам", callback_data=f"client_keys:{client_id}")])

    await cb.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_with_home(rows))


@router.callback_query(F.data.startswith("link_key_do:"))
async def link_key_do(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    _, client_id_str, key_id_str = cb.data.split(":")
    client_id = int(client_id_str)
    key_id = int(key_id_str)

    await assign_key_to_client(key_id, client_id)
    await cb.answer("Доступ к ключу добавлен")
    await client_keys(cb)


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
        InlineKeyboardButton(text="⚡ Скорость всех (host)", callback_data="speed_all_host"),
    ], [
        InlineKeyboardButton(text="🔐 Скорость всех (vpn)", callback_data="speed_all_vpn"),
    ]]
    await msg.answer("Выбери сервер:", reply_markup=_with_home(buttons))


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
            reply_markup=_server_kb(server_name),
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
    return _with_home([
        [
            InlineKeyboardButton(text="👥 Peer'ы", callback_data=f"peers:{server_name}"),
            InlineKeyboardButton(text="📶 Пинг", callback_data=f"ping:{server_name}"),
        ],
        [
            InlineKeyboardButton(text="⚡ Скорость host", callback_data=f"speed_host:{server_name}"),
            InlineKeyboardButton(text="🔐 Скорость VPN", callback_data=f"speed_vpn:{server_name}"),
        ],
        [
            InlineKeyboardButton(text="🔄 Перезагрузить сервер", callback_data=f"reboot_ask:{server_name}"),
            InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_servers"),
        ],
    ])


@router.callback_query(F.data == "back_to_servers")
async def back_to_servers(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    buttons = [
        [InlineKeyboardButton(text=f"🖥 {s.name}", callback_data=f"srv:{s.name}")]
        for s in SERVERS
    ] + [[
        InlineKeyboardButton(text="📶 Пинг всех", callback_data="ping_all"),
        InlineKeyboardButton(text="⚡ Скорость всех (host)", callback_data="speed_all_host"),
    ], [
        InlineKeyboardButton(text="🔐 Скорость всех (vpn)", callback_data="speed_all_vpn"),
    ]]
    await cb.message.edit_text("Выбери сервер:", reply_markup=_with_home(buttons))


@router.callback_query(F.data.startswith("peers:"))
async def show_peers(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    server_name = cb.data.split(":", 1)[1]
    await cb.message.edit_text(f"⏳ Загружаю peer'ы {server_name}...")

    peers = await get_all_peers_merged(server_name)
    if not peers:
        await cb.message.edit_text(
            "Peer'ов нет.",
            reply_markup=_with_home([[InlineKeyboardButton(text="◀️ Назад", callback_data=f"srv:{server_name}")]]),
        )
        return

    lines = [f"<b>Peer'ы {server_name}:</b>", ""]
    for p in peers:
        if p["connected"]:
            status = "🟢 онлайн"
        elif p.get("last_handshake", 0):
            status = f"🔴 офлайн (был: {_format_last_seen(p['last_handshake'])})"
        else:
            status = "⚪ ни разу не подключался"

        lines.append(
            f"<b>{p['name']}</b> {p['ip']}\n"
            f"   {status} | ↓{p['rx_mb']} ↑{p['tx_mb']} MB"
        )

    await cb.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="◀️ Назад", callback_data=f"srv:{server_name}")]]),
    )


# ─── Пинг ─────────────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("ping:"))
async def do_ping(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    server_name = cb.data.split(":", 1)[1]
    await cb.message.edit_text(f"⏳ Пингую {server_name}...")
    result = await ping_server(server_name)
    if result["success"] and result["ms"]:
        emoji = "🟢" if result["ms"] < 50 else ("🟡" if result["ms"] < 150 else "🔴")
        text = f"{emoji} <b>{server_name}</b>\nПинг: <b>{result['ms']:.1f} мс</b>"
    else:
        text = f"❌ <b>{server_name}</b> не отвечает"
    await cb.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="◀️ Назад", callback_data=f"srv:{server_name}")]]),
    )


@router.callback_query(F.data == "ping_all")
async def ping_all(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.edit_text("⏳ Пингую все серверы...")
    lines = ["<b>📶 Пинг серверов:</b>", ""]
    for s in SERVERS:
        result = await ping_server(s.name)
        if result["success"] and result["ms"]:
            emoji = "🟢" if result["ms"] < 50 else ("🟡" if result["ms"] < 150 else "🔴")
            lines.append(f"{emoji} {s.name}: <b>{result['ms']:.1f} мс</b>")
        else:
            lines.append(f"❌ {s.name}: недоступен")
    await cb.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_servers")]]),
    )


# ─── Скорость ──────────────────────────────────────────────────────────────────


def _format_speed_result(server_name: str, result: dict, label: str) -> str:
    if result.get("success"):
        dl = result.get("download_mbps", "—")
        ul = result.get("upload_mbps", "—")
        ping = result.get("ping_ms", "—")
        method = result.get("method", "")
        return (
            f"{label} <b>{server_name}</b>\n"
            f"⬇️ Download: <b>{dl} Mbit/s</b>\n"
            f"⬆️ Upload: <b>{ul} Mbit/s</b>\n"
            f"📶 Ping: {ping} мс\n"
            f"<i>метод: {method}</i>"
        )
    diag = result.get("diagnostic")
    diag_line = f"\n<i>{diag}</i>" if diag else ""
    return f"{label} <b>{server_name}</b>\n❌ {result.get('error', 'ошибка')}{diag_line}"


@router.callback_query(F.data.startswith("speed_host:"))
async def do_speed_host(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    server_name = cb.data.split(":", 1)[1]
    await cb.message.edit_text(f"⏳ Тестирую скорость host {server_name}... (может занять 30 сек)")
    result = await speed_test_host(server_name)
    text = _format_speed_result(server_name, result, "⚡ Host")
    await cb.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="◀️ Назад", callback_data=f"srv:{server_name}")]]),
    )


@router.callback_query(F.data.startswith("speed_vpn:"))
async def do_speed_vpn(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    server_name = cb.data.split(":", 1)[1]
    await cb.message.edit_text(f"⏳ Тестирую скорость VPN-контейнера {server_name}... (может занять 30-60 сек)")
    result = await speed_test_vpn(server_name)
    text = _format_speed_result(server_name, result, "🔐 VPN")
    await cb.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="◀️ Назад", callback_data=f"srv:{server_name}")]]),
    )


@router.callback_query(F.data.startswith("speed:"))
async def do_speed_legacy(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    server_name = cb.data.split(":", 1)[1]
    await cb.message.edit_text(f"⏳ Тестирую host и VPN {server_name}... (до 60 сек)")
    both = await speed_test_both(server_name)
    text = (
        f"{_format_speed_result(server_name, both['host'], '⚡ Host')}\n\n"
        f"{_format_speed_result(server_name, both['vpn'], '🔐 VPN')}"
    )
    await cb.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="◀️ Назад", callback_data=f"srv:{server_name}")]]),
    )


@router.callback_query(F.data.in_({"speed_all", "speed_all_host"}))
async def speed_all_host(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.edit_text("⏳ Тестирую скорость всех серверов (host)... (до 90 сек)")
    lines = ["<b>⚡ Скорость всех серверов (host):</b>", ""]
    for s in SERVERS:
        result = await speed_test_host(s.name)
        if result.get("success"):
            dl = result.get("download_mbps", "—")
            ul = result.get("upload_mbps", "—")
            lines.append(f"✅ {s.name}: ⬇️ <b>{dl}</b> ⬆️ <b>{ul}</b> Mbit/s")
        else:
            lines.append(f"❌ {s.name}: {result.get('error', 'ошибка')}")
    await cb.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_servers")]]),
    )


@router.callback_query(F.data == "speed_all_vpn")
async def speed_all_vpn(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.edit_text("⏳ Тестирую скорость всех серверов (VPN-контейнер)... (до 120 сек)")
    lines = ["<b>🔐 Скорость всех серверов (VPN):</b>", ""]
    for s in SERVERS:
        result = await speed_test_vpn(s.name)
        if result.get("success"):
            dl = result.get("download_mbps", "—")
            ul = result.get("upload_mbps", "—")
            lines.append(f"✅ {s.name}: ⬇️ <b>{dl}</b> ⬆️ <b>{ul}</b> Mbit/s")
        else:
            lines.append(f"❌ {s.name}: {result.get('error', 'ошибка')}")
    await cb.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_servers")]]),
    )


# ─── Перезагрузка сервера ──────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("reboot_ask:"))
async def reboot_ask(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    server_name = cb.data.split(":", 1)[1]
    await cb.message.edit_text(
        (
            f"⚠️ <b>Подтверждение перезагрузки</b>\n\n"
            f"Сервер: <b>{server_name}</b>\n"
            "Сервер станет недоступен на 1-3 минуты.\n"
            "Продолжить?"
        ),
        parse_mode="HTML",
        reply_markup=_with_home([
            [
                InlineKeyboardButton(text="✅ Да, перезагрузить", callback_data=f"reboot_do:{server_name}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"srv:{server_name}"),
            ]
        ]),
    )


@router.callback_query(F.data.startswith("reboot_do:"))
async def reboot_do(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    server_name = cb.data.split(":", 1)[1]
    await cb.message.edit_text(f"⏳ Отправляю команду перезагрузки на {server_name}...")
    result = await reboot_server(server_name)
    if result.get("success"):
        text = (
            f"✅ Команда перезагрузки отправлена: <b>{server_name}</b>\n"
            "Проверить доступность можно через 1-3 минуты."
        )
    else:
        text = f"❌ Не удалось перезагрузить {server_name}: {result.get('error', 'ошибка')}"
    await cb.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="◀️ К серверам", callback_data="back_to_servers")]]),
    )


# ─── Статистика ───────────────────────────────────────────────────────────────


@router.message(F.text == "📊 Статистика")
async def show_stats(msg: Message):
    if not is_admin(msg.from_user.id):
        return

    clients = await get_all_clients()
    total = len(clients)
    active = sum(1 for c in clients if c.active)
    paid = sum(1 for c in clients if _parse_date(c.paid_until) and _parse_date(c.paid_until) >= date.today())
    waiting = sum(1 for c in clients if c.payment_status == "waiting_confirm")
    pending = total - paid - waiting

    total_keys = sum(c.key_count for c in clients)
    payable_keys = sum(c.payable_key_count for c in clients)
    monthly = sum(c.monthly_fee for c in clients if c.active)

    text = (
        f"<b>📊 Статистика</b>\n\n"
        f"Всего клиентов: <b>{total}</b>\n"
        f"Активных клиентов: <b>{active}</b>\n\n"
        f"🔑 Ключей всего: <b>{total_keys}</b>\n"
        f"💰 Платных ключей: <b>{payable_keys}</b>\n"
        f"📦 Тариф за устройство: <b>{_device_price_text()}</b>\n\n"
        f"✅ Оплачено: <b>{paid}</b>\n"
        f"🔄 На проверке: <b>{waiting}</b>\n"
        f"⏳ Не оплачено: <b>{pending}</b>\n\n"
        f"💰 Ожидаемый доход в месяц: <b>{monthly:.0f} ₽</b>"
    )
    await msg.answer(text, parse_mode="HTML", reply_markup=admin_main_kb())


# ─── Добавление клиента (FSM) ─────────────────────────────────────────────────


class AddClientForm(StatesGroup):
    telegram_id = State()
    name = State()
    username = State()
    server = State()
    confirm = State()


class PaymentMonthsForm(StatesGroup):
    months = State()


@router.callback_query(F.data == "add_client_inline")
async def add_client_inline(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.clear()
    await cb.message.answer(
        "Введи <b>Telegram ID</b> нового клиента:",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )
    await state.set_state(AddClientForm.telegram_id)
    await cb.answer()


@router.callback_query(F.data.startswith("create_from_key:"))
async def create_client_from_key(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return

    key_id = int(cb.data.split(":")[1])
    key = await get_key_by_id(key_id)
    if not key:
        await cb.answer("Ключ не найден", show_alert=True)
        return
    if await is_key_linked_any(key_id):
        await cb.answer("Ключ уже привязан. Обнови список.", show_alert=True)
        return

    await state.clear()
    await state.update_data(
        bind_key_id=key.id,
        server=key.server_name,
        suggested_name=(key.key_name or f"Client-{key.id}"),
        key_preview=(key.key_name or key.wg_pubkey[:8]),
    )
    await state.set_state(AddClientForm.telegram_id)

    await cb.message.answer(
        (
            f"🔗 Создание клиента из ключа <b>{key.key_name or key.wg_pubkey[:8]}</b>\n"
            f"Сервер: <b>{key.server_name}</b>\n\n"
            "Введи <b>Telegram ID</b> клиента:"
        ),
        parse_mode="HTML",
        reply_markup=back_kb(),
    )
    await cb.answer()


@router.message(F.text == "➕ Добавить клиента")
@router.message(Command("add_client"))
async def cmd_add_client(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return

    await msg.answer(
        "Введи <b>Telegram ID</b> нового клиента:",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )
    await state.set_state(AddClientForm.telegram_id)


@router.message(F.text == "◀️ Назад")
async def go_back(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return

    current = await state.get_state()
    if current is None:
        await msg.answer("Ты уже в главном меню.", reply_markup=admin_main_kb())
        return

    states_order = [
        AddClientForm.telegram_id,
        AddClientForm.name,
        AddClientForm.username,
        AddClientForm.server,
        AddClientForm.confirm,
        PaymentMonthsForm.months,
    ]
    prompts = {
        str(AddClientForm.telegram_id): "Введи <b>Telegram ID</b>:",
        str(AddClientForm.name): "Введи <b>имя</b> клиента:",
        str(AddClientForm.username): "Введи <b>@username</b> (без @, или '-'):",
        str(AddClientForm.server): "Выбери <b>сервер</b>:",
        str(PaymentMonthsForm.months): "Введи количество месяцев оплаты (целое число, от 1):",
    }

    if current == str(PaymentMonthsForm.months):
        await state.clear()
        await msg.answer("Отменено.", reply_markup=admin_main_kb())
        return

    data = await state.get_data()
    if current == str(AddClientForm.confirm) and data.get("bind_key_id"):
        await state.set_state(AddClientForm.username)
        await msg.answer(
            "Введи <b>@username</b> (без @, или '-'):",
            parse_mode="HTML",
            reply_markup=back_kb(),
        )
        return

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

    prompt = prompts.get(str(prev_state), "Шаг назад")
    await msg.answer(prompt, parse_mode="HTML", reply_markup=back_kb())


@router.message(AddClientForm.telegram_id)
async def add_tg_id(msg: Message, state: FSMContext):
    if msg.text in ("◀️ Назад", "🏠 Главное меню"):
        return

    try:
        tg_id = int(msg.text.strip())
    except ValueError:
        await msg.answer("❌ Неверный формат. Введи число.")
        return

    existing = await get_client_by_tg(tg_id)
    if existing:
        await msg.answer(
            f"❌ Клиент с таким Telegram ID уже есть: <b>{existing.name}</b> (id={existing.id})",
            parse_mode="HTML",
        )
        return

    await state.update_data(telegram_id=tg_id)
    data = await state.get_data()
    suggested_name = data.get("suggested_name")
    prompt = "Введи <b>имя</b> клиента:"
    if suggested_name:
        prompt = f"Введи <b>имя</b> клиента (например: <code>{suggested_name}</code>):"
    await msg.answer(prompt, parse_mode="HTML", reply_markup=back_kb())
    await state.set_state(AddClientForm.name)


@router.message(AddClientForm.name)
async def add_name(msg: Message, state: FSMContext):
    if msg.text in ("◀️ Назад", "🏠 Главное меню"):
        return

    await state.update_data(name=msg.text.strip())
    await msg.answer(
        "Введи <b>@username</b> (без @, или '-' если нет):",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )
    await state.set_state(AddClientForm.username)


@router.message(AddClientForm.username)
async def add_username(msg: Message, state: FSMContext):
    if msg.text in ("◀️ Назад", "🏠 Главное меню"):
        return

    username = msg.text.strip().lstrip("@")
    await state.update_data(username=None if username == "-" else username)

    data = await state.get_data()
    bind_key_id = data.get("bind_key_id")
    if bind_key_id:
        key = await get_key_by_id(int(bind_key_id))
        if not key:
            await state.clear()
            await msg.answer("❌ Ключ не найден. Начни снова.", reply_markup=admin_main_kb())
            return
        if await is_key_linked_any(int(bind_key_id)):
            await state.clear()
            await msg.answer("❌ Ключ уже привязан к другому клиенту.", reply_markup=admin_main_kb())
            return

        await state.update_data(server=key.server_name)
        data = await state.get_data()
        summary = (
            f"<b>Проверь данные:</b>\n\n"
            f"Telegram ID: <code>{data['telegram_id']}</code>\n"
            f"Имя: {data['name']}\n"
            f"Username: @{data.get('username') or '-'}\n"
            f"Сервер: {key.server_name}\n"
            f"Ключ: {key.key_name or key.wg_pubkey[:8]} ({key.allowed_ips or '—'})\n\n"
            f"Тариф по умолчанию: {_device_price_text()} за платное устройство\n"
            f"(сумма считается по связанным ключам)\n\n"
            f"Создать клиента и привязать этот ключ?"
        )
        kb = _with_home([
            [InlineKeyboardButton(text="✅ Создать и привязать ключ", callback_data="add_bind_key")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="add_cancel")],
        ])
        await msg.answer(summary, parse_mode="HTML", reply_markup=kb)
        await state.set_state(AddClientForm.confirm)
        return

    kb = _with_home(
        [[InlineKeyboardButton(text=s.name, callback_data=f"sel_srv:{s.name}")] for s in SERVERS]
    )
    await msg.answer("Выбери <b>сервер</b>:", parse_mode="HTML", reply_markup=kb)
    await state.set_state(AddClientForm.server)


@router.callback_query(AddClientForm.server, F.data.startswith("sel_srv:"))
async def add_server(cb: CallbackQuery, state: FSMContext):
    server_name = cb.data.split(":", 1)[1]
    await state.update_data(server=server_name)

    data = await state.get_data()
    summary = (
        f"<b>Проверь данные:</b>\n\n"
        f"Telegram ID: <code>{data['telegram_id']}</code>\n"
        f"Имя: {data['name']}\n"
        f"Username: @{data.get('username') or '-'}\n"
        f"Сервер: {server_name}\n\n"
        f"Тариф по умолчанию: {_device_price_text()} за платное устройство\n"
        f"(сумма считается по связанным ключам)\n\n"
        f"Создать WireGuard ключ сразу?"
    )

    kb = _with_home([
        [
            InlineKeyboardButton(text="✅ Создать ключ", callback_data="add_with_wg"),
            InlineKeyboardButton(text="📝 Без ключа", callback_data="add_no_wg"),
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="add_cancel")],
    ])

    await cb.message.edit_text(summary, parse_mode="HTML", reply_markup=kb)
    await state.set_state(AddClientForm.confirm)


@router.callback_query(AddClientForm.confirm, F.data.in_({"add_with_wg", "add_no_wg", "add_bind_key", "add_cancel"}))
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
            await cb.message.answer("⚠️ Не удалось создать ключ. Добавляю клиента без ключа.")

    try:
        if cb.data == "add_bind_key":
            bind_key_id = int(data.get("bind_key_id", 0))
            key = await get_key_by_id(bind_key_id)
            if not key:
                await cb.message.edit_text("❌ Ключ не найден. Попробуй снова.")
                return
            if await is_key_linked_any(bind_key_id):
                await cb.message.edit_text("❌ Ключ уже привязан к другому клиенту.")
                return

            client_id = await add_client(
                telegram_id=data["telegram_id"],
                name=data["name"],
                username=data.get("username"),
                server_name=key.server_name,
                devices=0,
                monthly_fee=0,
                wg_pubkey=key.wg_pubkey,
                wg_peer_id=key.allowed_ips,
            )
        else:
            client_id = await add_client(
                telegram_id=data["telegram_id"],
                name=data["name"],
                username=data.get("username"),
                server_name=data["server"],
                devices=0,
                monthly_fee=0,
                wg_pubkey=wg_data["pubkey"] if wg_data else None,
                wg_peer_id=wg_data["client_ip"] if wg_data else None,
            )
    except Exception as e:
        logger.exception("add client failed")
        await cb.message.edit_text(f"❌ Не удалось добавить клиента: {e}")
        return

    ok_text = f"✅ <b>{data['name']}</b> добавлен (id={client_id})!"
    if cb.data == "add_bind_key":
        ok_text += "\nКлюч успешно привязан."
    await cb.message.edit_text(ok_text, parse_mode="HTML")
    await cb.message.answer("Главное меню:", reply_markup=admin_main_kb())

    if wg_data:
        config_file = io.BytesIO(wg_data["config_text"].encode())
        config_file.name = f"vpn_{data['name'].replace(' ', '_')}.conf"
        await cb.message.answer_document(
            config_file,
            caption=f"🔑 Конфиг для <b>{data['name']}</b>\nIP: {wg_data['client_ip']}",
            parse_mode="HTML",
        )


# ─── Подтверждение/отклонение оплаты ─────────────────────────────────────────


@router.callback_query(F.data.startswith("pay_choose:"))
async def pay_choose(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    client_id = int(cb.data.split(":")[1])
    await _show_months_selector(cb.message, client_id, edit=True)


@router.callback_query(F.data.startswith("confirm_pay:"))
async def confirm_pay_select_month(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    client_id = int(cb.data.split(":")[1])
    await _show_months_selector(cb.message, client_id, edit=True)


@router.callback_query(F.data.startswith("confirm_pay_custom:"))
async def confirm_pay_custom(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return

    client_id = int(cb.data.split(":")[1])
    await state.set_state(PaymentMonthsForm.months)
    await state.update_data(pay_client_id=client_id)
    await cb.message.answer(
        "Введи количество месяцев оплаты (целое число, от 1):",
        reply_markup=back_kb(),
    )
    await cb.answer()


@router.message(PaymentMonthsForm.months)
async def payment_months_input(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    if msg.text in ("◀️ Назад", "🏠 Главное меню"):
        return

    try:
        months = int(msg.text.strip())
    except ValueError:
        await msg.answer("❌ Введи целое число месяцев (например 4).")
        return

    if months < 1 or months > 60:
        await msg.answer("❌ Допустимо от 1 до 60 месяцев.")
        return

    data = await state.get_data()
    client_id = int(data.get("pay_client_id"))
    await state.clear()

    waiting = await msg.answer("⏳ Подтверждаю оплату...")
    await _apply_payment_confirmation(msg.bot, client_id, months, waiting)
    await msg.answer("Главное меню:", reply_markup=admin_main_kb())


@router.callback_query(F.data.startswith("confirm_pay_do:"))
async def confirm_payment(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    _, client_id_str, months_str = cb.data.split(":")
    client_id = int(client_id_str)
    months = int(months_str)

    await _apply_payment_confirmation(cb.bot, client_id, months, cb.message)


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

    await cb.message.edit_text(
        f"❌ Оплата от <b>{client.name}</b> отклонена.",
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="👤 Открыть клиента", callback_data=f"client_card:{client.id}")]]),
    )

    try:
        await cb.bot.send_message(
            client.telegram_id,
            "❌ <b>Оплата не подтверждена.</b>\nПроверь перевод и нажми кнопку снова.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid:{client.id}")]]
            ),
        )
    except Exception:
        logger.exception("failed to notify rejected payment for client %s", client.id)


# ─── Удаление peer вручную (опционально) ─────────────────────────────────────


@router.callback_query(F.data.startswith("remove_peer:"))
async def remove_peer_manual(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    _, server_name, pubkey = cb.data.split(":", 2)
    ok = await remove_peer(server_name, pubkey)
    if ok:
        await cb.answer("Peer удалён")
    else:
        await cb.answer("Не удалось удалить peer")
