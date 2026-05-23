"""Admin handlers: clients, servers, keys, and payments."""

import asyncio
import calendar
import logging
import html
from datetime import datetime, date, timedelta
from typing import Optional, Tuple, List

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    Message,
    CallbackQuery,
    BufferedInputFile,
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
    get_client_by_username,
    update_payment_status,
    set_paid_until,
    set_client_active,
    log_payment,
    sync_server_keys,
    get_client_keys,
    get_unlinked_keys,
    get_linkable_keys,
    get_key_by_id,
    get_key_by_server_pubkey,
    get_key_access_clients,
    is_key_linked_to_client,
    is_key_linked_any,
    upsert_client_key,
    assign_key_to_client,
    unassign_key,
    delete_key_record,
    set_key_payer,
    set_key_paused,
    set_key_billing_client,
    delete_client_record,
    get_global_key_stats,
    get_last_payment_log,
    get_payment_logs_for_day,
    get_client_server_names,
    get_user_lang,
    set_user_lang,
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
from support_dialog import (
    SUPPORT_BROADCAST_TARGET,
    open_client_dialog,
    close_client_dialog,
    set_admin_target,
    get_admin_target,
    clear_admin_target,
)
from i18n import tr, normalize_lang

router = Router()
logger = logging.getLogger(__name__)
CLIENT_KEY_MESSAGE_TTL_SEC = 3600

ADMIN_CLIENTS_TEXTS = {"👥 Клиенты", "👥 Clients"}
ADMIN_SERVERS_TEXTS = {"🖥 Серверы", "🖥 Servers"}
ADMIN_ADD_CLIENT_TEXTS = {"➕ Добавить клиента", "➕ Add Client"}
ADMIN_STATS_TEXTS = {"📊 Статистика", "📊 Statistics"}
ADMIN_SUPPORT_MENU_TEXTS = {"💬 Поддержка", "💬 Support"}
SUPPORT_OPEN_TEXTS = {"💬 Написать в поддержку", "💬 Contact support"}
SUPPORT_CLOSE_TEXTS = {"❌ Закрыть диалог", "❌ Close dialog"}
HOME_MENU_TEXTS = {"🏠 Главное меню", "🏠 Main Menu"}
BACK_TEXTS = {"◀️ Назад", "◀️ Back"}
LANG_BUTTON_TEXTS = {"🌐 Язык", "🌐 Language"}


async def _lang_for_user(user) -> str:
    saved = await get_user_lang(user.id)
    if saved:
        return normalize_lang(saved)
    detected = normalize_lang(getattr(user, "language_code", None))
    await set_user_lang(user.id, detected)
    return detected


# ─── Common helpers ───────────────────────────────────────────────────────────


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def admin_main_kb(lang: str = "ru") -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=tr(lang, "admin_clients")), KeyboardButton(text=tr(lang, "admin_servers"))],
            [KeyboardButton(text=tr(lang, "admin_add_client")), KeyboardButton(text=tr(lang, "admin_stats"))],
            [KeyboardButton(text=tr(lang, "support_menu")), KeyboardButton(text=tr(lang, "lang_button"))],
        ],
        resize_keyboard=True,
    )


def back_kb(lang: str = "ru") -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=tr(lang, "back")), KeyboardButton(text=tr(lang, "home_menu"))]],
        resize_keyboard=True,
    )


def _with_home(rows: List[List[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=rows + [[InlineKeyboardButton(text="🏠 Home", callback_data="menu_home")]]
    )


def _admin_support_dialog_kb(lang: str = "ru") -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=tr(lang, "support_close")), KeyboardButton(text=tr(lang, "home_menu"))]],
        resize_keyboard=True,
    )


def _client_support_kb_for_admin_send(lang: str = "ru", show_pay_button: bool = True) -> ReplyKeyboardMarkup:
    row = [KeyboardButton(text=tr(lang, "client_status_btn"))]
    if show_pay_button:
        row.append(KeyboardButton(text=tr(lang, "client_paid_btn")))
    return ReplyKeyboardMarkup(
        keyboard=[
            row,
            [KeyboardButton(text=tr(lang, "support_open"))],
            [KeyboardButton(text=tr(lang, "support_close"))],
        ],
        resize_keyboard=True,
    )


async def _delete_message_later(bot, chat_id: int, message_id: int, delay_sec: int = CLIENT_KEY_MESSAGE_TTL_SEC):
    await asyncio.sleep(delay_sec)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        # Message might have been deleted manually or bot access might be lost.
        pass


async def _notify_client_key_deleted(bot, client_id: int, key_name: str, server_name: str):
    client = await get_client_by_id(client_id)
    if not client:
        return False
    try:
        lang = normalize_lang(await get_user_lang(client.telegram_id))
        await bot.send_message(
            client.telegram_id,
            (
                f"❌ <b>VPN key was deleted by admin</b>\n\nName: <b>{key_name}</b>\nServer: {server_name}"
                if lang == "en"
                else f"❌ <b>VPN-ключ удалён администратором</b>\n\nНазвание: <b>{key_name}</b>\nСервер: {server_name}"
            ),
            parse_mode="HTML",
        )
        return True
    except Exception:
        logger.exception("failed to notify key deletion for client %s", client_id)
        return False


async def _notify_client_key_unlinked(bot, client_id: int, key_name: str, server_name: str):
    client = await get_client_by_id(client_id)
    if not client:
        return
    try:
        lang = normalize_lang(await get_user_lang(client.telegram_id))
        await bot.send_message(
            client.telegram_id,
            (
                f"ℹ️ <b>Access to VPN key is disabled</b>\n\nName: <b>{key_name}</b>\nServer: {server_name}"
                if lang == "en"
                else f"ℹ️ <b>Доступ к VPN-ключу отключён</b>\n\nНазвание: <b>{key_name}</b>\nСервер: {server_name}"
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("failed to notify key unlink for client %s", client_id)


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


def _format_last_seen(last_handshake: int, lang: str = "ru") -> str:
    if not last_handshake:
        return "never" if lang == "en" else "никогда"
    return datetime.fromtimestamp(last_handshake).strftime("%d.%m.%Y %H:%M")


def _client_status(client, lang: str = "ru") -> str:
    if client.monthly_fee <= 0:
        return "🚫 payment not required" if lang == "en" else "🚫 оплата не требуется"
    today = date.today()
    paid_until = _parse_date(client.paid_until)
    if paid_until and paid_until >= today:
        return (f"✅ until {paid_until.strftime('%d.%m.%Y')}" if lang == "en" else f"✅ до {paid_until.strftime('%d.%m.%Y')}")

    mapping = {
        "paid": "✅ paid" if lang == "en" else "✅ оплачено",
        "pending": "⏳ awaiting payment" if lang == "en" else "⏳ ожидает оплаты",
        "waiting_confirm": "🔄 under review" if lang == "en" else "🔄 проверяется",
        "overdue": "🔴 overdue" if lang == "en" else "🔴 просрочено",
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


def _parse_payment_log_note(note: str) -> dict:
    parsed = {}
    if not note:
        return parsed
    for part in note.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        parsed[k.strip()] = v.strip()
    return parsed


def _extract_base_paid_until_from_log(log_row: Optional[dict]) -> Tuple[bool, Optional[str]]:
    """Try to restore original paid_until (before month extension)."""
    if not log_row:
        return False, None

    note_data = _parse_payment_log_note(log_row.get("note", ""))

    if "base_paid_until" in note_data:
        raw = (note_data.get("base_paid_until") or "").strip()
        if raw.lower() in ("none", "null", ""):
            return True, None
        base_dt = _parse_date(raw[:10])
        return (True, base_dt.strftime("%Y-%m-%d")) if base_dt else (False, None)

    # Backward-compatible path for older notes without base_paid_until:
    # derive it from "paid_until" and "months".
    try:
        months = int((note_data.get("months") or "").strip())
    except Exception:
        return False, None
    if months < 1:
        return False, None

    paid_until_dt = _parse_date((note_data.get("paid_until") or "").strip()[:10])
    if not paid_until_dt:
        return False, None

    start = paid_until_dt + timedelta(days=1)
    base_dt = _add_months(start, -months) - timedelta(days=1)
    return True, base_dt.strftime("%Y-%m-%d")


async def _sync_peers_all_servers(lang: str = "ru") -> Tuple[str, int]:
    """Import peers from servers into DB.

    Returns (UI message, number of new keys).
    """
    lines = []
    total_new = 0
    for server in SERVERS:
        try:
            peers = await get_all_peers_merged(server.name)
            stats = await sync_server_keys(server.name, peers)
            total_new += stats["added"]
            lines.append(
                (
                    f"• {server.name}: {stats['total']} keys, +{stats['added']} new"
                    if lang == "en"
                    else f"• {server.name}: {stats['total']} ключей, +{stats['added']} новых"
                )
            )
        except Exception as e:
            logger.exception("sync failed for %s", server.name)
            lines.append(f"• {server.name}: error ({e})" if lang == "en" else f"• {server.name}: ошибка ({e})")

    return "\n".join(lines), total_new


async def _build_clients_view_text(lang: str = "ru") -> Tuple[str, InlineKeyboardMarkup]:
    clients = await get_all_clients()
    unlinked = await get_unlinked_keys()

    if not clients:
        text = (
            "<b>👥 Clients</b>\n\nNo clients yet.\n"
            f"Unlinked keys: <b>{len(unlinked)}</b>"
            if lang == "en"
            else "<b>👥 Клиенты</b>\n\nКлиентов пока нет.\n"
            f"Непривязанные ключи: <b>{len(unlinked)}</b>"
        )
        kb = _with_home([
            [InlineKeyboardButton(text="➕ Создать клиента / Create client", callback_data="add_client_inline")],
            [InlineKeyboardButton(text="🔄 Импорт peer'ов / Import peers", callback_data="sync_peers_now")],
            [InlineKeyboardButton(text="🧩 Непривязанные ключи / Unlinked keys", callback_data="show_unlinked_info")],
        ])
        return text, kb

    text_lines = (["<b>👥 Clients:</b>", ""] if lang == "en" else ["<b>👥 Клиенты:</b>", ""])
    for c in clients:
        active_icon = "🟢" if c.active else "🔴"
        status = _client_status(c, lang)
        text_lines.append(
            f"{active_icon} <b>{c.name}</b> | {status} | "
            +
            (
                f"keys: {c.key_count} ({c.payable_key_count} billable) | {c.monthly_fee:.0f} ₽"
                if lang == "en"
                else f"ключей: {c.key_count} ({c.payable_key_count} платн.) | {c.monthly_fee:.0f} ₽"
            )
        )

    text_lines.append("")
    text_lines.append(f"🧩 Unlinked keys: <b>{len(unlinked)}</b>" if lang == "en" else f"🧩 Непривязанных ключей: <b>{len(unlinked)}</b>")

    rows = [
        [InlineKeyboardButton(text="➕ Создать клиента / Create client", callback_data="add_client_inline")],
        [InlineKeyboardButton(text="🔄 Импорт peer'ов / Import peers", callback_data="sync_peers_now")],
        [InlineKeyboardButton(text="🧩 Непривязанные ключи / Unlinked keys", callback_data="show_unlinked_info")],
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


async def _render_client_card(client_id: int, lang: str = "ru") -> Tuple[str, InlineKeyboardMarkup]:
    client = await get_client_by_id(client_id)
    if not client:
        return "❌ Клиент не найден / Client not found", _with_home([[InlineKeyboardButton(text="◀️ К клиентам / To clients", callback_data="back_to_clients")]])

    keys = await get_client_keys(client.id)
    active_keys = [k for k in keys if k.active]
    online_count = sum(1 for k in active_keys if k.connected)
    last_hs = max((k.last_handshake for k in keys), default=0)

    if online_count > 0:
        vpn_line = (
            f"🟢 online devices: {online_count}/{len(active_keys) or len(keys)}"
            if lang == "en"
            else f"🟢 онлайн устройств: {online_count}/{len(active_keys) or len(keys)}"
        )
    else:
        vpn_line = (
            f"🔴 offline | last seen: {_format_last_seen(last_hs, lang)}"
            if lang == "en"
            else f"🔴 офлайн | был онлайн: {_format_last_seen(last_hs, lang)}"
        )

    paid_until = _parse_date(client.paid_until)
    paid_until_text = paid_until.strftime("%d.%m.%Y") if paid_until else "—"
    server_names = await get_client_server_names(client.id, active_only=True)
    if not server_names:
        server_names = [client.server_name]
    if len(server_names) == 1:
        server_line = f"Server: {server_names[0]}" if lang == "en" else f"Сервер: {server_names[0]}"
    else:
        server_line = f"Servers: {', '.join(server_names)}" if lang == "en" else f"Серверы: {', '.join(server_names)}"

    text = (
        f"<b>👤 {client.name}</b>\n"
        f"TG: <code>{client.telegram_id}</code> | @{client.username or '-'}\n"
        f"{server_line}\n"
        f"{vpn_line}\n"
        + (
            f"Keys: {client.key_count} | billable: {client.payable_key_count} | non-payers: {client.nonpayable_key_count}\n"
            f"Tariff: <b>{_device_price_text()}</b> per device\n"
            f"Monthly due: <b>{client.monthly_fee:.0f} ₽</b>\n"
            f"Payment status: {_client_status(client, lang)}\n"
            f"Paid until: <b>{paid_until_text}</b>\n"
            f"Last payment: {client.payment_date or '—'}\n"
            f"Client VPN: {'active' if client.active else 'disabled'}"
            if lang == "en"
            else
            f"Ключей: {client.key_count} | платных: {client.payable_key_count} | неплательщиков: {client.nonpayable_key_count}\n"
            f"Тариф: <b>{_device_price_text()}</b> за устройство\n"
            f"К оплате в месяц: <b>{client.monthly_fee:.0f} ₽</b>\n"
            f"Статус оплаты: {_client_status(client, lang)}\n"
            f"Оплачено до: <b>{paid_until_text}</b>\n"
            f"Последняя оплата: {client.payment_date or '—'}\n"
            f"VPN клиента: {'активен' if client.active else 'отключён'}"
        )
    )

    kb = _with_home([
        [
            InlineKeyboardButton(
                text=("🔴 Disable" if client.active else "🟢 Enable") if lang == "en" else ("🔴 Отключить" if client.active else "🟢 Включить"),
                callback_data=f"toggle_client:{client.id}",
            ),
            InlineKeyboardButton(text="🗑 Удалить / Delete", callback_data=f"del_client:{client.id}"),
        ],
        [
            InlineKeyboardButton(text="🔑 Ключи / Keys", callback_data=f"client_keys:{client.id}"),
            InlineKeyboardButton(text="💳 Подтвердить оплату / Confirm payment", callback_data=f"pay_choose:{client.id}"),
        ],
        [InlineKeyboardButton(text="◀️ К списку / To list", callback_data="back_to_clients")],
    ])
    return text, kb


async def _show_months_selector(message, client_id: int, edit: bool = True, lang: str = "ru"):
    client = await get_client_by_id(client_id)
    if not client:
        text = "❌ Client not found" if lang == "en" else "❌ Клиент не найден"
        if edit:
            await message.edit_text(text)
        else:
            await message.answer(text)
        return

    text = (
        f"<b>💳 Payment confirmation</b>\n\n"
        f"Client: <b>{client.name}</b>\n"
        f"Billable keys: <b>{client.payable_key_count}</b>\n"
        f"Tariff: {_device_price_text()} per device\n"
        f"Monthly: <b>{client.monthly_fee:.0f} ₽</b>\n\n"
        f"Choose payment period:"
        if lang == "en"
        else
        f"<b>💳 Подтверждение оплаты</b>\n\n"
        f"Клиент: <b>{client.name}</b>\n"
        f"Платных ключей: <b>{client.payable_key_count}</b>\n"
        f"Тариф: {_device_price_text()} за устройство\n"
        f"Ежемесячно: <b>{client.monthly_fee:.0f} ₽</b>\n\n"
        f"Выбери срок оплаты:"
    )

    kb = _with_home([
        [
            InlineKeyboardButton(text="1 мес / 1 mo", callback_data=f"confirm_pay_do:{client_id}:1"),
            InlineKeyboardButton(text="2 мес / 2 mo", callback_data=f"confirm_pay_do:{client_id}:2"),
            InlineKeyboardButton(text="3 мес / 3 mo", callback_data=f"confirm_pay_do:{client_id}:3"),
        ],
        [
            InlineKeyboardButton(text="6 мес / 6 mo", callback_data=f"confirm_pay_do:{client_id}:6"),
            InlineKeyboardButton(text="12 мес / 12 mo", callback_data=f"confirm_pay_do:{client_id}:12"),
        ],
        [
            InlineKeyboardButton(text="✍️ Другое / Custom", callback_data=f"confirm_pay_custom:{client_id}"),
            InlineKeyboardButton(text="❌ Отклонить / Reject", callback_data=f"reject_pay:{client_id}"),
        ],
        [InlineKeyboardButton(text="🎁 Тестовый период / Trial (until month end)", callback_data=f"trial_until_month_end:{client_id}")],
        [InlineKeyboardButton(text="◀️ К клиенту / To client", callback_data=f"client_card:{client_id}")],
    ])

    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _apply_payment_confirmation(bot, client_id: int, months: int, source_message):
    client = await get_client_by_id(client_id)
    client_lang = normalize_lang(await get_user_lang(client.telegram_id)) if client else "ru"
    if not client:
        await source_message.edit_text("❌ Client not found" if client_lang == "en" else "❌ Клиент не найден")
        return

    months = max(1, months)
    today_str = datetime.now().strftime("%Y-%m-%d")
    payment_date_str = (client.payment_date or "").strip()[:10]
    is_correction = client.payment_status == "paid" and (payment_date_str == today_str)

    base_paid_until = client.paid_until
    if is_correction:
        found_base = False
        today_logs = await get_payment_logs_for_day(
            client_id,
            today_str,
            actions=["confirmed", "confirmed_corrected"],
        )
        if today_logs:
            # Use the first operation of the day as correction anchor.
            found_base, restored_base = _extract_base_paid_until_from_log(today_logs[0])
            if found_base:
                base_paid_until = restored_base
        if not found_base:
            last_log = await get_last_payment_log(client_id, actions=["confirmed", "confirmed_corrected"])
            found_base, restored_base = _extract_base_paid_until_from_log(last_log)
            if found_base:
                base_paid_until = restored_base

    new_paid_until = _extend_paid_until(base_paid_until, months)
    monthly_amount = float(client.monthly_fee)
    total_amount = monthly_amount * months

    await set_paid_until(client_id, new_paid_until)
    await update_payment_status(client_id, "paid", today_str)
    await log_payment(
        client_id,
        "confirmed_corrected" if is_correction else "confirmed",
        total_amount,
        note=(
            f"months={months}; monthly={monthly_amount:.0f}; "
            f"devices_payable={client.payable_key_count}; paid_until={new_paid_until}; "
            f"base_paid_until={base_paid_until or 'none'}"
        ),
    )

    until_dt = _parse_date(new_paid_until)
    until_text = until_dt.strftime("%d.%m.%Y") if until_dt else new_paid_until

    await source_message.edit_text(
        (
            (
                f"{'✅ Payment updated' if is_correction else '✅ Payment confirmed'}\n\n"
                f"Client: <b>{client.name}</b>\n"
                f"Period: <b>{months} mo.</b>\n"
                f"Amount: <b>{total_amount:.0f} ₽</b>\n"
                f"Paid until: <b>{until_text}</b>"
            )
            if client_lang == "en"
            else
            f"{'✅ Оплата обновлена' if is_correction else '✅ Оплата подтверждена'}\n\n"
            f"Клиент: <b>{client.name}</b>\n"
            f"Срок: <b>{months} мес.</b>\n"
            f"Сумма: <b>{total_amount:.0f} ₽</b>\n"
            f"Оплачено до: <b>{until_text}</b>"
        ),
        parse_mode="HTML",
        reply_markup=_with_home([
            [InlineKeyboardButton(text="👤 Открыть клиента / Open client", callback_data=f"client_card:{client.id}")]
        ]),
    )

    try:
        await bot.send_message(
            client.telegram_id,
            (
                (
                    f"{'✅ <b>Payment updated by admin</b>' if is_correction else '✅ <b>Payment confirmed!</b>'}\n"
                    f"Period: <b>{months} mo.</b>\n"
                    f"Paid until: <b>{until_text}</b>\n"
                    f"Connected billable devices: <b>{client.payable_key_count}</b>\n"
                    f"Thank you, {client.name}."
                )
                if client_lang == "en"
                else
                f"{'✅ <b>Оплата обновлена администратором</b>' if is_correction else '✅ <b>Оплата подтверждена!</b>'}\n"
                f"Срок: <b>{months} мес.</b>\n"
                f"Оплачено до: <b>{until_text}</b>\n"
                f"Подключённых платных устройств: <b>{client.payable_key_count}</b>\n"
                f"Спасибо, {client.name}."
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("failed to notify client %s", client.id)


def _trial_until_month_end(today: date) -> date:
    if today.month == 12:
        first_next_month = date(today.year + 1, 1, 1)
    else:
        first_next_month = date(today.year, today.month + 1, 1)
    return first_next_month - timedelta(days=1)


@router.callback_query(F.data.startswith("trial_until_month_end:"))
async def grant_trial_until_month_end(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)

    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer(tr(lang, "client_not_found"), show_alert=True)
        return

    today = date.today()
    today_str = today.strftime("%Y-%m-%d")
    trial_until_dt = _trial_until_month_end(today)
    trial_until = trial_until_dt.strftime("%Y-%m-%d")

    current_paid_until_dt = _parse_date(client.paid_until)
    effective_until_dt = trial_until_dt
    if current_paid_until_dt and current_paid_until_dt > trial_until_dt:
        effective_until_dt = current_paid_until_dt
    effective_until = effective_until_dt.strftime("%Y-%m-%d")

    await set_paid_until(client_id, effective_until)
    await update_payment_status(client_id, "paid", today_str)
    await log_payment(
        client_id,
        "trial_granted",
        0,
        note=(
            "trial_until_month_end=1; "
            f"trial_until={trial_until}; "
            f"effective_until={effective_until}; "
            f"previous_paid_until={client.paid_until or 'none'}"
        ),
    )

    until_text = effective_until_dt.strftime("%d.%m.%Y")
    await cb.message.edit_text(
        (
            "🎁 Тестовый период выдан\n\n"
            f"Клиент: <b>{client.name}</b>\n"
            f"Действует до: <b>{until_text}</b>"
        ),
        parse_mode="HTML",
        reply_markup=_with_home([
            [InlineKeyboardButton(text="👤 Открыть клиента / Open client", callback_data=f"client_card:{client.id}")]
        ]),
    )
    await cb.answer("Тестовый период выдан / Trial granted")

    try:
        await cb.bot.send_message(
            client.telegram_id,
            (
                "🎁 <b>Тебе выдан тестовый период</b>\n"
                f"Доступ активен до: <b>{until_text}</b>."
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("failed to notify trial grant for client %s", client.id)


# ─── Main menu ────────────────────────────────────────────────────────────────


@router.message(Command("start"))
@router.message(F.text.in_(HOME_MENU_TEXTS))
async def cmd_main_menu(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    lang = await _lang_for_user(msg.from_user)
    await state.clear()
    await msg.answer(
        f"👋 <b>{'Добро пожаловать, админ!' if lang == 'ru' else 'Welcome, admin!'}</b>\n\n{tr(lang, 'admin_pick_section')}",
        parse_mode="HTML",
        reply_markup=admin_main_kb(lang),
    )


@router.message(Command("menu"))
async def cmd_menu(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await cmd_main_menu(msg, state)


@router.message(F.text.in_(LANG_BUTTON_TEXTS))
@router.message(Command("language"))
async def admin_language_menu(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    lang = await _lang_for_user(msg.from_user)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Русский / Russian", callback_data="lang_set:ru"),
                InlineKeyboardButton(text="English", callback_data="lang_set:en"),
            ]
        ]
    )
    await msg.answer(tr(lang, "choose_language"), reply_markup=kb)


@router.callback_query(F.data.startswith("lang_set:"))
async def admin_language_set(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = normalize_lang(cb.data.split(":", 1)[1])
    await set_user_lang(cb.from_user.id, lang)
    await cb.message.answer(
        tr(lang, "lang_saved_ru" if lang == "ru" else "lang_saved_en"),
        reply_markup=admin_main_kb(lang),
    )
    await cb.answer()


@router.callback_query(F.data == "menu_home")
async def cb_menu_home(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)
    await state.clear()
    await cb.message.edit_text(tr(lang, "admin_menu_back"))
    await cb.message.answer(tr(lang, "admin_pick_section"), reply_markup=admin_main_kb(lang))
    await cb.answer()


# ─── Support ──────────────────────────────────────────────────────────────────


def _support_menu_kb(lang: str = "ru") -> InlineKeyboardMarkup:
    return _with_home([
        [InlineKeyboardButton(text=("👤 Message client" if lang == "en" else "👤 Написать клиенту"), callback_data="support_pick_client")],
        [InlineKeyboardButton(text=("📢 Broadcast" if lang == "en" else "📢 Написать всем"), callback_data="support_set_broadcast")],
        [InlineKeyboardButton(text="❌ Close", callback_data="support_close_active")],
    ])


async def _support_target_text(admin_id: int, lang: str = "ru") -> str:
    target = get_admin_target(admin_id)
    if not target:
        return "Current mode: <b>not selected</b>" if lang == "en" else "Текущий режим: <b>не выбран</b>"
    if target == SUPPORT_BROADCAST_TARGET:
        return "Current mode: <b>broadcast to all clients</b>" if lang == "en" else "Текущий режим: <b>рассылка всем клиентам</b>"
    client = await get_client_by_tg(int(target))
    if client:
        return (
            ("Current mode: " if lang == "en" else "Текущий режим: ")
            + f"<b>{'dialog with' if lang == 'en' else 'диалог с'} {html.escape(client.name)}</b> "
            f"(<code>{client.telegram_id}</code>)"
        )
    return "Current mode: <b>not selected</b>" if lang == "en" else "Текущий режим: <b>не выбран</b>"


async def _open_support_menu(message, admin_id: int, edit: bool = False):
    lang = await _lang_for_user(message.from_user)
    text = (
        "<b>💬 Support</b>\n\nChoose action:\n• message specific client\n• enable broadcast mode\n• close current dialog\n\n"
        f"{await _support_target_text(admin_id, lang)}"
        if lang == "en"
        else "<b>💬 Поддержка</b>\n\nВыбери действие:\n• написать конкретному клиенту\n• включить режим рассылки всем клиентам\n• закрыть текущий диалог\n\n"
        f"{await _support_target_text(admin_id, lang)}"
    )
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=_support_menu_kb(lang))
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=_support_menu_kb(lang))


@router.message(F.text.in_(ADMIN_SUPPORT_MENU_TEXTS))
async def support_menu(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    await _open_support_menu(msg, msg.from_user.id, edit=False)


@router.callback_query(F.data == "support_menu")
async def support_menu_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await _open_support_menu(cb.message, cb.from_user.id, edit=True)
    await cb.answer()


@router.callback_query(F.data == "support_pick_client")
async def support_pick_client(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    lang = await _lang_for_user(cb.from_user)
    clients = await get_all_clients()
    if not clients:
        await cb.message.edit_text(
            "<b>💬 Support</b>\n\nNo clients yet." if lang == "en" else "<b>💬 Поддержка</b>\n\nКлиентов пока нет.",
            parse_mode="HTML",
            reply_markup=_with_home([[InlineKeyboardButton(text=("◀️ Back" if lang == "en" else "◀️ Назад"), callback_data="support_menu")]]),
        )
        await cb.answer()
        return

    clients.sort(key=lambda c: c.name.lower())
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'🟢' if c.active else '🔴'} {c.name}",
                callback_data=f"support_set_client:{c.id}",
            )
        ]
        for c in clients
    ]
    rows.append([InlineKeyboardButton(text=("◀️ Back" if lang == "en" else "◀️ Назад"), callback_data="support_menu")])

    await cb.message.edit_text(
        "<b>💬 Client selection</b>\n\nWho should receive your message:" if lang == "en" else "<b>💬 Выбор клиента</b>\n\nКому написать:",
        parse_mode="HTML",
        reply_markup=_with_home(rows),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("support_set_client:"))
async def support_set_client(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer(tr(lang, "client_not_found"), show_alert=True)
        return

    set_admin_target(cb.from_user.id, int(client.telegram_id))
    open_client_dialog(int(client.telegram_id))

    await cb.message.edit_text(
        (
            (
                "<b>💬 Reply mode enabled</b>\n\n"
                f"Client: <b>{html.escape(client.name)}</b>\n"
                f"TG: <code>{client.telegram_id}</code>\n\n"
                "Now just send a text message, and it will be forwarded to the client.\n"
                "To finish, press <b>❌ Close dialog</b>."
            )
            if lang == "en"
            else
            (
                "<b>💬 Режим ответа клиенту включён</b>\n\n"
                f"Клиент: <b>{html.escape(client.name)}</b>\n"
                f"TG: <code>{client.telegram_id}</code>\n\n"
                "Теперь просто отправь текст, и он уйдёт клиенту.\n"
                "Для завершения нажми <b>❌ Закрыть диалог</b>."
            )
        ),
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text=("◀️ To support" if lang == "en" else "◀️ К поддержке"), callback_data="support_menu")]]),
    )
    await cb.message.answer(
        (f"✍️ Write a message for {client.name}." if lang == "en" else f"✍️ Пиши сообщение для {client.name}."),
        reply_markup=_admin_support_dialog_kb(lang),
    )
    await cb.answer("Reply mode enabled" if lang == "en" else "Режим ответа активирован")


@router.callback_query(F.data == "support_set_broadcast")
async def support_set_broadcast(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    set_admin_target(cb.from_user.id, SUPPORT_BROADCAST_TARGET)
    await cb.message.edit_text(
        (
            "<b>📢 Broadcast mode enabled</b>\n\nThe next message will be sent to all clients.\nTo finish, press <b>❌ Close dialog</b>."
            if lang == "en"
            else "<b>📢 Режим рассылки включён</b>\n\nСледующее сообщение будет отправлено всем клиентам.\nДля завершения нажми <b>❌ Закрыть диалог</b>."
        ),
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text=("◀️ To support" if lang == "en" else "◀️ К поддержке"), callback_data="support_menu")]]),
    )
    await cb.message.answer("✍️ Write broadcast text." if lang == "en" else "✍️ Напиши текст рассылки.", reply_markup=_admin_support_dialog_kb(lang))
    await cb.answer()


@router.callback_query(F.data.startswith("support_reply:"))
async def support_reply_to_client(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)
    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer(tr(lang, "client_not_found"), show_alert=True)
        return

    set_admin_target(cb.from_user.id, int(client.telegram_id))
    open_client_dialog(int(client.telegram_id))
    await cb.message.answer(
        (
            (
                f"✍️ Reply mode: <b>{html.escape(client.name)}</b>\n"
                "Write a message to the client."
            )
            if lang == "en"
            else
            (
                f"✍️ Режим ответа: <b>{html.escape(client.name)}</b>\n"
                "Напиши сообщение клиенту."
            )
        ),
        parse_mode="HTML",
        reply_markup=_admin_support_dialog_kb(lang),
    )
    await cb.answer("Reply mode enabled" if lang == "en" else "Режим ответа активирован")


@router.callback_query(F.data.startswith("support_close:"))
async def support_close_dialog_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)
    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer(tr(lang, "client_not_found"), show_alert=True)
        return

    close_client_dialog(int(client.telegram_id))
    try:
        await cb.bot.send_message(
            client.telegram_id,
            tr(lang, "support_closed_by_admin"),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("failed to notify client %s about support close", client.id)

    await cb.message.answer(f"✅ Диалог с {client.name} закрыт." if lang == "ru" else f"✅ Dialog with {client.name} is closed.", reply_markup=admin_main_kb(lang))
    await cb.answer(tr(lang, "dialog_closed"))


@router.callback_query(F.data == "support_close_active")
async def support_close_active(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)
    target = get_admin_target(cb.from_user.id)
    clear_admin_target(cb.from_user.id)

    if target and target != SUPPORT_BROADCAST_TARGET:
        close_client_dialog(int(target))
        client = await get_client_by_tg(int(target))
        if client:
            try:
                await cb.bot.send_message(
                    client.telegram_id,
                    tr(lang, "support_closed_by_admin"),
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception("failed to notify client %s about support close", client.id)

    await cb.message.edit_text(
        "<b>💬 Support</b>\n\nCurrent dialog is closed." if lang == "en" else "<b>💬 Поддержка</b>\n\nТекущий диалог закрыт.",
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="◀️ To support" if lang == "en" else "◀️ К поддержке", callback_data="support_menu")]]),
    )
    await cb.message.answer(tr(lang, "main_menu_title"), reply_markup=admin_main_kb(lang))
    await cb.answer(tr(lang, "dialog_closed"))


@router.message(F.text.in_(SUPPORT_CLOSE_TEXTS))
async def support_close_dialog_msg(msg: Message):
    if not is_admin(msg.from_user.id):
        return

    target = get_admin_target(msg.from_user.id)
    if not target:
        lang = await _lang_for_user(msg.from_user)
        await msg.answer(tr(lang, "no_active_dialog"), reply_markup=admin_main_kb(lang))
        return

    clear_admin_target(msg.from_user.id)
    if target == SUPPORT_BROADCAST_TARGET:
        lang = await _lang_for_user(msg.from_user)
        await msg.answer(tr(lang, "broadcast_mode_closed"), reply_markup=admin_main_kb(lang))
        return

    close_client_dialog(int(target))
    client = await get_client_by_tg(int(target))
    if client:
        try:
            await msg.bot.send_message(
                client.telegram_id,
                tr(lang, "support_closed_by_admin"),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("failed to notify client %s about support close", client.id)
        lang = await _lang_for_user(msg.from_user)
        await msg.answer((f"✅ Диалог с {client.name} закрыт." if lang == "ru" else f"✅ Dialog with {client.name} is closed."), reply_markup=admin_main_kb(lang))
    else:
        lang = await _lang_for_user(msg.from_user)
        await msg.answer(f"✅ {tr(lang, 'dialog_closed')}.", reply_markup=admin_main_kb(lang))


# ─── Clients ──────────────────────────────────────────────────────────────────


@router.message(F.text.in_(ADMIN_CLIENTS_TEXTS))
@router.message(Command("clients"))
async def show_clients(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    lang = await _lang_for_user(msg.from_user)

    await msg.answer("⏳ Updating peers from servers..." if lang == "en" else "⏳ Обновляю peer'ы с серверов...")
    sync_text, _ = await _sync_peers_all_servers(lang)

    text, kb = await _build_clients_view_text(lang)
    await msg.answer(f"{text}\n\n<i>{html.escape(sync_text)}</i>", parse_mode="HTML", reply_markup=kb)


@router.message(Command("client"))
async def cmd_client(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    lang = await _lang_for_user(msg.from_user)

    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) == 1:
        await show_clients(msg)
        return

    try:
        client_id = int(parts[1].strip())
    except ValueError:
        await msg.answer("Usage: /client <id>" if lang == "en" else "Использование: /client <id>")
        return

    await _sync_peers_all_servers(lang)
    text, kb = await _render_client_card(client_id, lang)
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "sync_peers_now")
async def sync_peers_now(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)
    await cb.message.edit_text("⏳ Importing peers..." if lang == "en" else "⏳ Импортирую peer'ы...")
    sync_text, _ = await _sync_peers_all_servers(lang)
    text, kb = await _build_clients_view_text(lang)
    await cb.message.edit_text(f"{text}\n\n<i>{html.escape(sync_text)}</i>", parse_mode="HTML", reply_markup=kb)
    await cb.answer("Synced" if lang == "en" else "Синхронизировано")


@router.callback_query(F.data == "show_unlinked_info")
async def show_unlinked_info(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)

    keys = await get_unlinked_keys()
    if not keys:
        text = "🧩 No unlinked keys." if lang == "en" else "🧩 Непривязанных ключей нет."
        await cb.message.edit_text(
            text,
            reply_markup=_with_home([[InlineKeyboardButton(text=("◀️ To clients" if lang == "en" else "◀️ К клиентам"), callback_data="back_to_clients")]]),
        )
        return

    lines = [
        "<b>🧩 Unlinked keys</b>" if lang == "en" else "<b>🧩 Непривязанные ключи</b>",
        "",
        (f"Total: <b>{len(keys)}</b>" if lang == "en" else f"Всего: <b>{len(keys)}</b>"),
        ("You can create a client from a key or link it to an existing client." if lang == "en" else "Можно создать клиента сразу из ключа, либо привязать к существующему клиенту."),
        "",
    ]

    for k in keys[:20]:
        online = "🟢" if k.connected else "🔴"
        payer = "💰" if k.payer else "🚫"
        lines.append(
            f"{online}{payer} <b>{k.key_name or ('No name' if lang == 'en' else 'Без имени')}</b> | {k.server_name} | {k.allowed_ips or '—'}"
        )

    if len(keys) > 20:
        lines.append((f"\n... and {len(keys) - 20} more keys" if lang == "en" else f"\n... и ещё {len(keys) - 20} ключей"))

    rows = [[InlineKeyboardButton(text=("➕ Create client (manual)" if lang == "en" else "➕ Создать клиента (вручную)"), callback_data="add_client_inline")]]
    for k in keys[:10]:
        online = "🟢" if k.connected else "🔴"
        label = f"{online}➕ {k.key_name or k.wg_pubkey[:8]} | {k.server_name}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"create_from_key:{k.id}")])
    rows.append([InlineKeyboardButton(text=("◀️ To clients" if lang == "en" else "◀️ К клиентам"), callback_data="back_to_clients")])

    await cb.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_with_home(rows),
    )


@router.callback_query(F.data == "back_to_clients")
async def back_to_clients(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    text, kb = await _build_clients_view_text(lang)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("client_card:"))
async def client_card(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    client_id = int(cb.data.split(":")[1])
    text, kb = await _render_client_card(client_id, lang)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("toggle_client:"))
async def toggle_client(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)

    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer(tr(lang, "client_not_found"))
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

    status = ("enabled 🟢" if lang == "en" else "включён 🟢") if new_state else ("disabled 🔴" if lang == "en" else "отключён 🔴")
    suffix = "" if errors == 0 else (f" ({errors} keys with errors)" if lang == "en" else f" ({errors} ключей с ошибкой)")
    await cb.answer(f"{client.name} {status}{suffix}")
    await client_card(cb)


@router.callback_query(F.data.startswith("del_client:"))
async def delete_client(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)
    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer(tr(lang, "client_not_found"))
        return

    kb = _with_home([
        [
            InlineKeyboardButton(text=("✅ Yes, delete" if lang == "en" else "✅ Да, удалить"), callback_data=f"del_confirm:{client_id}"),
            InlineKeyboardButton(text=("❌ No" if lang == "en" else "❌ Нет"), callback_data=f"client_card:{client_id}"),
        ]
    ])
    await cb.message.edit_text(
        (
            (f"Delete <b>{client.name}</b> from DB?\n\nKeys on server will remain and become unlinked." if lang == "en" else
             f"Удалить <b>{client.name}</b> из БД?\n\nКлючи на сервере останутся и станут непривязанными.")
        ),
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("del_confirm:"))
async def delete_confirm(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)
    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer(tr(lang, "client_not_found"))
        return

    await delete_client_record(client_id)
    await cb.message.edit_text(
        (f"🗑 <b>{client.name}</b> deleted. Keys unlinked." if lang == "en" else f"🗑 <b>{client.name}</b> удалён. Ключи отвязаны."),
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text=("◀️ To clients" if lang == "en" else "◀️ К клиентам"), callback_data="back_to_clients")]]),
    )


# ─── Client keys ──────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("client_keys:"))
async def client_keys(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)

    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer(tr(lang, "client_not_found"))
        return

    keys = await get_client_keys(client_id)
    if not keys:
        text = (
            (f"<b>🔑 Client keys: {client.name}</b>\n\nNo keys yet.\nImport peers and link keys." if lang == "en" else
             f"<b>🔑 Ключи клиента {client.name}</b>\n\nКлючей пока нет.\nИмпортируй peer'ы и привяжи ключи.")
        )
        kb = _with_home([
            [InlineKeyboardButton(text=("🆕 Create key on server" if lang == "en" else "🆕 Создать ключ на сервере"), callback_data=f"create_key_pick_server:{client_id}")],
            [InlineKeyboardButton(text=("➕ Link key" if lang == "en" else "➕ Привязать ключ"), callback_data=f"link_key_pick:{client_id}:0")],
            [InlineKeyboardButton(text=("◀️ To client" if lang == "en" else "◀️ К клиенту"), callback_data=f"client_card:{client_id}")],
        ])
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        return

    lines = [
        (f"<b>🔑 Client keys: {client.name}</b>" if lang == "en" else f"<b>🔑 Ключи клиента {client.name}</b>"),
        "",
        (f"Total: <b>{len(keys)}</b> | billable: <b>{sum(1 for k in keys if k.payer)}</b>" if lang == "en" else f"Всего: <b>{len(keys)}</b> | платных: <b>{sum(1 for k in keys if k.payer)}</b>"),
        (f"Monthly due: <b>{client.monthly_fee:.0f} ₽/mo</b>" if lang == "en" else f"К оплате: <b>{client.monthly_fee:.0f} ₽/мес</b>"),
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

    rows.append([InlineKeyboardButton(text=("🆕 Create key on server" if lang == "en" else "🆕 Создать ключ на сервере"), callback_data=f"create_key_pick_server:{client_id}")])
    rows.append([InlineKeyboardButton(text=("➕ Link key" if lang == "en" else "➕ Привязать ключ"), callback_data=f"link_key_pick:{client_id}:0")])
    rows.append([InlineKeyboardButton(text=("◀️ To client" if lang == "en" else "◀️ К клиенту"), callback_data=f"client_card:{client_id}")])

    await cb.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_with_home(rows))


@router.callback_query(F.data.startswith("create_key_pick_server:"))
async def create_key_pick_server(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)

    client_id = int(cb.data.split(":")[1])
    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer(tr(lang, "client_not_found"))
        return

    rows = [
        [InlineKeyboardButton(text=f"🖥 {s.name}", callback_data=f"create_key_do:{client_id}:{s.name}")]
        for s in SERVERS
    ]
    rows.append([InlineKeyboardButton(text="◀️ To keys" if lang == "en" else "◀️ К ключам", callback_data=f"client_keys:{client_id}")])

    await cb.message.edit_text(
        (
            f"<b>🆕 Create new key</b>\n\n"
            f"Client: <b>{client.name}</b>\n"
            f"Choose server:"
            if lang == "en"
            else f"<b>🆕 Создание нового ключа</b>\n\nКлиент: <b>{client.name}</b>\nВыбери сервер, где создать ключ:"
        ),
        parse_mode="HTML",
        reply_markup=_with_home(rows),
    )


@router.callback_query(F.data.startswith("create_key_do:"))
async def create_key_do(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)

    _, client_id_str, server_name = cb.data.split(":", 2)
    client_id = int(client_id_str)
    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer(tr(lang, "client_not_found"))
        return

    peer_name = f"{client.name}_{datetime.now().strftime('%d%m_%H%M')}"
    await cb.message.edit_text(
        (f"⏳ Creating key for <b>{client.name}</b> on <b>{server_name}</b>..." if lang == "en" else
         f"⏳ Создаю ключ для <b>{client.name}</b> на сервере <b>{server_name}</b>..."),
        parse_mode="HTML",
    )
    wg_data = await add_peer(server_name, peer_name)
    if not wg_data:
        await cb.message.edit_text(
            (f"❌ Failed to create key on server {server_name}." if lang == "en" else f"❌ Не удалось создать ключ на сервере {server_name}."),
            reply_markup=_with_home([[InlineKeyboardButton(text=("◀️ To keys" if lang == "en" else "◀️ К ключам"), callback_data=f"client_keys:{client_id}")]]),
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
    vpn_uri = wg_data.get("vpn_uri", "")
    admin_config = BufferedInputFile(wg_data["config_text"].encode(), filename=file_name)
    await cb.message.answer_document(
        admin_config,
        caption=(
            (f"🔑 New key for <b>{client.name}</b>\n" if lang == "en" else f"🔑 Новый ключ для <b>{client.name}</b>\n")
            + (f"Key name: <b>{peer_name}</b>\n" if lang == "en" else f"Название ключа: <b>{peer_name}</b>\n")
            + (f"Server: {server_name}\n")
            +
            f"IP: {wg_data['client_ip']}"
        ),
        parse_mode="HTML",
    )
    if vpn_uri:
        await cb.message.answer(vpn_uri, disable_web_page_preview=True)

    delivered_vpn_to_client = False
    if vpn_uri:
        try:
            link_msg = await cb.bot.send_message(
                client.telegram_id,
                vpn_uri,
                disable_web_page_preview=True,
            )
            asyncio.create_task(
                _delete_message_later(
                    cb.bot,
                    chat_id=client.telegram_id,
                    message_id=link_msg.message_id,
                    delay_sec=CLIENT_KEY_MESSAGE_TTL_SEC,
                )
            )
            delivered_vpn_to_client = True
        except Exception:
            logger.exception("failed to send vpn:// link to client %s", client.id)

    vpn_delivery_text = (
        ("delivered (message will be deleted in 1 hour)" if lang == "en" else "доставлена (сообщение удалится через 1 час)")
        if delivered_vpn_to_client
        else (("not generated" if lang == "en" else "не сгенерирована") if not vpn_uri else ("not delivered" if lang == "en" else "не доставлена"))
    )
    await cb.message.edit_text(
        (
            ("✅ Key created.\n" if lang == "en" else "✅ Ключ создан.\n")
            + (f"Name: <b>{peer_name}</b>\n" if lang == "en" else f"Название: <b>{peer_name}</b>\n")
            + f"Server: <b>{server_name}</b>\n"
            f"IP: <b>{wg_data['client_ip']}</b>\n\n"
            + (f"vpn:// delivery to client: <b>{vpn_delivery_text}</b>" if lang == "en" else f"Отправка vpn:// клиенту: <b>{vpn_delivery_text}</b>")
        ),
        parse_mode="HTML",
        reply_markup=_with_home([
            [InlineKeyboardButton(text=("🔑 Client keys" if lang == "en" else "🔑 Ключи клиента"), callback_data=f"client_keys:{client_id}")],
            [InlineKeyboardButton(text=("👤 Client card" if lang == "en" else "👤 Карточка клиента"), callback_data=f"client_card:{client_id}")],
        ]),
    )


async def _show_key_card(cb: CallbackQuery, key_id: int, client_id: int):
    lang = await _lang_for_user(cb.from_user)
    key = await get_key_by_id(key_id)
    if not key:
        await cb.answer("Key not found" if lang == "en" else "Ключ не найден")
        return

    if not await is_key_linked_to_client(key_id, client_id):
        await cb.answer("This client has no access to the key" if lang == "en" else "У этого клиента нет доступа к ключу", show_alert=True)
        return

    if key.paused:
        conn_text = (f"⏸ paused (last seen: {_format_last_seen(key.last_handshake, lang)})" if lang == "en" else f"⏸ остановлен (был: {_format_last_seen(key.last_handshake, lang)})")
    else:
        conn_text = ("🟢 online" if lang == "en" else "🟢 онлайн") if key.connected else (f"🔴 offline (last seen: {_format_last_seen(key.last_handshake, lang)})" if lang == "en" else f"🔴 офлайн (был: {_format_last_seen(key.last_handshake, lang)})")
    if not key.payer:
        payer_text = "🚫 Non-payer" if lang == "en" else "🚫 Неплательщик"
    elif key.billing_client_id == client_id:
        payer_text = "💰 Payer: this client" if lang == "en" else "💰 Плательщик: этот клиент"
    elif key.billing_client_name:
        payer_text = f"💰 {'Payer' if lang == 'en' else 'Плательщик'}: {key.billing_client_name}"
    else:
        payer_text = "💰 Payer is not set" if lang == "en" else "💰 Плательщик не назначен"

    linked_clients = await get_key_access_clients(key_id)
    linked_names = ", ".join(c.name for c in linked_clients[:4])
    if len(linked_clients) > 4:
        linked_names += f" +{len(linked_clients) - 4}"

    text = (
        (f"<b>🔑 Key</b>\n" if lang == "en" else f"<b>🔑 Ключ</b>\n")
        + (f"Name: <b>{key.key_name or 'No name'}</b>\n" if lang == "en" else f"Имя: <b>{key.key_name or 'Без имени'}</b>\n")
        + f"Server: {key.server_name}\n"
        +
        f"IP: {key.allowed_ips or '—'}\n"
        + (f"Status: {conn_text}\n" if lang == "en" else f"Статус: {conn_text}\n")
        + (f"Billing: {payer_text}\n" if lang == "en" else f"Оплата: {payer_text}\n")
        + (f"Linked clients: <b>{len(linked_clients)}</b> ({linked_names or '—'})\n" if lang == "en" else f"Доступ у клиентов: <b>{len(linked_clients)}</b> ({linked_names or '—'})\n")
        +
        f"RX/TX: {round(key.rx_bytes / 1_048_576, 2)} / {round(key.tx_bytes / 1_048_576, 2)} MB\n"
        +
        f"PubKey: <code>{key.wg_pubkey}</code>"
    )

    rows = [[
        InlineKeyboardButton(
            text=("▶️ Start key" if lang == "en" else "▶️ Запустить ключ") if key.paused else ("⏸ Pause key" if lang == "en" else "⏸ Остановить ключ"),
            callback_data=f"key_toggle_pause:{key.id}:{client_id}",
        ),
    ], [
        InlineKeyboardButton(
            text=("🚫 Set as non-payer" if lang == "en" else "🚫 Сделать неплательщиком") if key.payer else ("💰 Set as billable" if lang == "en" else "💰 Сделать платным"),
            callback_data=f"key_toggle_payer:{key.id}:{client_id}",
        ),
        InlineKeyboardButton(text=("🔓 Unlink" if lang == "en" else "🔓 Отвязать"), callback_data=f"key_unlink:{key.id}:{client_id}"),
    ]]
    if key.payer and key.billing_client_id != client_id:
        rows.append([InlineKeyboardButton(
            text="👤 Set this client as payer" if lang == "en" else "👤 Назначить плательщиком этого клиента",
            callback_data=f"key_set_billing:{key.id}:{client_id}",
        )])
    rows.append([InlineKeyboardButton(
        text="🗑 Delete key from server" if lang == "en" else "🗑 Удалить ключ с сервера",
        callback_data=f"key_remove_ask:{key.id}:{client_id}",
    )])
    rows.append([InlineKeyboardButton(text=("◀️ To keys" if lang == "en" else "◀️ К ключам"), callback_data=f"client_keys:{client_id}")])

    kb = _with_home(rows)

    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("key_card:"))
async def key_card(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    _, key_id_str, client_id_str = cb.data.split(":")
    key_id = int(key_id_str)
    client_id = int(client_id_str)
    await _show_key_card(cb, key_id, client_id)


@router.callback_query(F.data.startswith("key_toggle_pause:"))
async def key_toggle_pause(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)

    _, key_id_str, client_id_str = cb.data.split(":")
    key_id = int(key_id_str)
    client_id = int(client_id_str)

    key = await get_key_by_id(key_id)
    if not key:
        await cb.answer("Key not found" if lang == "en" else "Ключ не найден")
        return

    if key.paused:
        if not key.allowed_ips or key.allowed_ips == "192.0.2.0/32":
            await cb.answer("Failed to resolve key IP to start" if lang == "en" else "Не удалось определить IP ключа для запуска", show_alert=True)
            return
        ok = await enable_peer(key.server_name, key.wg_pubkey, key.allowed_ips)
        if ok:
            await set_key_paused(key_id, False)
            await cb.answer("Key started" if lang == "en" else "Ключ запущен")
        else:
            await cb.answer("Failed to start key" if lang == "en" else "Не удалось запустить ключ", show_alert=True)
            return
    else:
        ok = await disable_peer(key.server_name, key.wg_pubkey)
        if ok:
            await set_key_paused(key_id, True)
            await cb.answer("Key paused" if lang == "en" else "Ключ остановлен")
        else:
            await cb.answer("Failed to pause key" if lang == "en" else "Не удалось остановить ключ", show_alert=True)
            return

    await _show_key_card(cb, key_id, client_id)


@router.callback_query(F.data.startswith("key_toggle_payer:"))
async def key_toggle_payer(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)

    _, key_id_str, client_id_str = cb.data.split(":")
    key_id = int(key_id_str)
    client_id = int(client_id_str)

    key = await get_key_by_id(key_id)
    if not key:
        await cb.answer("Key not found" if lang == "en" else "Ключ не найден")
        return

    new_payer = not key.payer
    await set_key_payer(key_id, new_payer)
    if new_payer and not key.billing_client_id:
        await set_key_billing_client(key_id, client_id)
    await cb.answer("Payer status updated" if lang == "en" else "Статус плательщика обновлён")
    await key_card(cb)


@router.callback_query(F.data.startswith("key_set_billing:"))
async def key_set_billing(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)

    _, key_id_str, client_id_str = cb.data.split(":")
    key_id = int(key_id_str)
    client_id = int(client_id_str)

    if not await is_key_linked_to_client(key_id, client_id):
        await cb.answer("Link key to client first" if lang == "en" else "Сначала привяжи ключ к клиенту", show_alert=True)
        return

    await set_key_billing_client(key_id, client_id)
    await set_key_payer(key_id, True)
    await cb.answer("Payer assigned" if lang == "en" else "Плательщик назначен")
    await key_card(cb)


@router.callback_query(F.data.startswith("key_unlink:"))
async def key_unlink(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)

    _, key_id_str, client_id_str = cb.data.split(":")
    key_id = int(key_id_str)
    client_id = int(client_id_str)
    key = await get_key_by_id(key_id)
    key_name = (key.key_name if key and key.key_name else f"key-{key_id}")
    key_server = (key.server_name if key else "—")

    await unassign_key(key_id, client_id)
    key_still_linked = await is_key_linked_any(key_id)
    await _notify_client_key_unlinked(cb.bot, client_id, key_name, key_server)
    await cb.answer("Key unlinked" if lang == "en" else "Ключ отвязан")
    await cb.message.edit_text(
        (
            "✅ Client access to key removed."
            + ("\nKey is still linked to other clients." if key_still_linked else "\nKey is now unlinked.")
            if lang == "en"
            else
            "✅ Доступ клиента к ключу удалён."
            + ("\nКлюч всё ещё связан с другими клиентами." if key_still_linked else "\nКлюч стал непривязанным.")
        ),
        reply_markup=_with_home([
            [InlineKeyboardButton(text="◀️ To client keys" if lang == "en" else "◀️ К ключам клиента", callback_data=f"client_keys:{client_id}")],
            [InlineKeyboardButton(text="🧩 Непривязанные ключи / Unlinked keys", callback_data="show_unlinked_info")],
        ]),
    )


@router.callback_query(F.data.startswith("key_remove_ask:"))
async def key_remove_ask(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)

    _, key_id_str, client_id_str = cb.data.split(":")
    key_id = int(key_id_str)
    client_id = int(client_id_str)

    key = await get_key_by_id(key_id)
    if not key:
        await cb.answer("Key not found" if lang == "en" else "Ключ не найден")
        return

    key_name = key.key_name or key.wg_pubkey[:8]
    await cb.message.edit_text(
        (
            (
                "⚠️ <b>Delete key</b>\n\n"
                f"Key: <b>{key_name}</b>\n"
                f"Server: <b>{key.server_name}</b>\n"
                f"IP: <b>{key.allowed_ips or '—'}</b>\n\n"
                "Key will be deleted from server and DB.\n"
                "All linked clients will be notified.\n\n"
                "Continue?"
                if lang == "en"
                else
                "⚠️ <b>Удаление ключа</b>\n\n"
                f"Ключ: <b>{key_name}</b>\n"
                f"Сервер: <b>{key.server_name}</b>\n"
                f"IP: <b>{key.allowed_ips or '—'}</b>\n\n"
                "Ключ будет удалён с сервера и из базы.\n"
                "Все связанные клиенты получат уведомление.\n\n"
                "Продолжить?"
            )
        ),
        parse_mode="HTML",
        reply_markup=_with_home([
            [
                InlineKeyboardButton(text="🗑 Да, удалить / Yes, delete", callback_data=f"key_remove_do:{key_id}:{client_id}"),
                InlineKeyboardButton(text="❌ Отмена / Cancel", callback_data=f"key_card:{key_id}:{client_id}"),
            ]
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("key_remove_do:"))
async def key_remove_do(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)

    _, key_id_str, client_id_str = cb.data.split(":")
    key_id = int(key_id_str)
    client_id = int(client_id_str)

    key = await get_key_by_id(key_id)
    if not key:
        await cb.answer("Key not found" if lang == "en" else "Ключ не найден")
        return

    key_name = key.key_name or key.wg_pubkey[:8]
    linked_clients = await get_key_access_clients(key_id)

    ok = await remove_peer(key.server_name, key.wg_pubkey)
    if not ok:
        await cb.message.edit_text(
            "❌ Failed to delete key on server." if lang == "en" else "❌ Не удалось удалить ключ на сервере.",
            reply_markup=_with_home([
                [InlineKeyboardButton(text="◀️ К ключам / To keys", callback_data=f"client_keys:{client_id}")],
                [InlineKeyboardButton(text="🔁 Открыть ключ / Open key", callback_data=f"key_card:{key_id}:{client_id}")],
            ]),
        )
        return

    await delete_key_record(key_id)

    notified = 0
    for linked in linked_clients:
        if await _notify_client_key_deleted(cb.bot, linked.client_id, key_name, key.server_name):
            notified += 1

    await cb.message.edit_text(
        (
            (
                "✅ Key deleted.\n\n"
                f"Name: <b>{key_name}</b>\n"
                f"Server: <b>{key.server_name}</b>\n"
                f"Clients notified: <b>{notified}/{len(linked_clients)}</b>"
                if lang == "en"
                else
                "✅ Ключ удалён.\n\n"
                f"Название: <b>{key_name}</b>\n"
                f"Сервер: <b>{key.server_name}</b>\n"
                f"Уведомлено клиентов: <b>{notified}/{len(linked_clients)}</b>"
            )
        ),
        parse_mode="HTML",
        reply_markup=_with_home([
            [InlineKeyboardButton(text="◀️ To client keys" if lang == "en" else "◀️ К ключам клиента", callback_data=f"client_keys:{client_id}")],
            [InlineKeyboardButton(text="🧩 Непривязанные ключи / Unlinked keys", callback_data="show_unlinked_info")],
        ]),
    )
    await cb.answer("Key deleted" if lang == "en" else "Ключ удалён")


@router.callback_query(F.data.startswith("link_key_pick:"))
async def link_key_pick(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)

    _, client_id_str, page_str = cb.data.split(":")
    client_id = int(client_id_str)
    page = int(page_str)

    client = await get_client_by_id(client_id)
    if not client:
        await cb.answer(tr(lang, "client_not_found"))
        return

    keys = await get_linkable_keys(client_id)
    if not keys:
        await cb.message.edit_text(
            "No keys available for linking.\n(All keys are already linked to this client.)"
            if lang == "en"
            else "Нет доступных ключей для привязки.\n(Все ключи уже связаны с этим клиентом.)",
            reply_markup=_with_home([
                [InlineKeyboardButton(text="◀️ To keys" if lang == "en" else "◀️ К ключам", callback_data=f"client_keys:{client_id}")]
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

    rows.append([InlineKeyboardButton(text="◀️ К ключам / To keys", callback_data=f"client_keys:{client_id}")])

    await cb.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_with_home(rows))


@router.callback_query(F.data.startswith("link_key_do:"))
async def link_key_do(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    _, client_id_str, key_id_str = cb.data.split(":")
    client_id = int(client_id_str)
    key_id = int(key_id_str)

    await assign_key_to_client(key_id, client_id)
    await cb.answer("Доступ к ключу добавлен / Key access added")
    await client_keys(cb)


# ─── Servers ──────────────────────────────────────────────────────────────────


@router.message(F.text.in_(ADMIN_SERVERS_TEXTS))
@router.message(Command("servers"))
async def show_servers(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    lang = await _lang_for_user(msg.from_user)
    buttons = [
        [InlineKeyboardButton(text=f"🖥 {s.name}", callback_data=f"srv:{s.name}")]
        for s in SERVERS
    ] + [[
        InlineKeyboardButton(text=("📶 Ping all" if lang == "en" else "📶 Пинг всех"), callback_data="ping_all"),
        InlineKeyboardButton(text=("⚡ Speed all (host)" if lang == "en" else "⚡ Скорость всех (host)"), callback_data="speed_all_host"),
    ], [
        InlineKeyboardButton(text=("🔐 Speed all (vpn)" if lang == "en" else "🔐 Скорость всех (vpn)"), callback_data="speed_all_vpn"),
    ]]
    await msg.answer("Choose server:" if lang == "en" else "Выбери сервер:", reply_markup=_with_home(buttons))


@router.callback_query(F.data.startswith("srv:"))
async def server_detail(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)

    server_name = cb.data.split(":", 1)[1]
    await cb.message.edit_text(f"⏳ {'Loading' if lang == 'en' else 'Загружаю'} {server_name}...")

    status = await get_server_status(server_name)
    if not status.get("online", False):
        err = status.get("error", "unavailable" if lang == "en" else "недоступен")
        await cb.message.edit_text(
            (f"❌ <b>{server_name}</b> unavailable\n{err}" if lang == "en" else f"❌ <b>{server_name}</b> недоступен\n{err}"),
            parse_mode="HTML",
            reply_markup=_server_kb(server_name, lang),
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
        f"🔗 WireGuard: {('✅ running' if lang == 'en' else '✅ работает') if status['wg_running'] else ('❌ not running' if lang == 'en' else '❌ не запущен')}\n"
        f"{'👥 Clients' if lang == 'en' else '👥 Клиентов'}: {status['peers_count']} ({'online' if lang == 'en' else 'онлайн'}: {online})"
    )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=_server_kb(server_name, lang))


def _server_kb(server_name: str, lang: str = "ru") -> InlineKeyboardMarkup:
    return _with_home([
        [
            InlineKeyboardButton(text=("👥 Peers" if lang == "en" else "👥 Peer'ы"), callback_data=f"peers:{server_name}"),
            InlineKeyboardButton(text=("📶 Ping" if lang == "en" else "📶 Пинг"), callback_data=f"ping:{server_name}"),
        ],
        [
            InlineKeyboardButton(text=("⚡ Host speed" if lang == "en" else "⚡ Скорость host"), callback_data=f"speed_host:{server_name}"),
            InlineKeyboardButton(text=("🔐 VPN speed" if lang == "en" else "🔐 Скорость VPN"), callback_data=f"speed_vpn:{server_name}"),
        ],
        [
            InlineKeyboardButton(text=("🔄 Reboot server" if lang == "en" else "🔄 Перезагрузить сервер"), callback_data=f"reboot_ask:{server_name}"),
            InlineKeyboardButton(text=("◀️ Back" if lang == "en" else "◀️ Назад"), callback_data="back_to_servers"),
        ],
    ])


@router.callback_query(F.data == "back_to_servers")
async def back_to_servers(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)
    buttons = [
        [InlineKeyboardButton(text=f"🖥 {s.name}", callback_data=f"srv:{s.name}")]
        for s in SERVERS
    ] + [[
        InlineKeyboardButton(text=("📶 Ping all" if lang == "en" else "📶 Пинг всех"), callback_data="ping_all"),
        InlineKeyboardButton(text=("⚡ Speed all (host)" if lang == "en" else "⚡ Скорость всех (host)"), callback_data="speed_all_host"),
    ], [
        InlineKeyboardButton(text=("🔐 Speed all (vpn)" if lang == "en" else "🔐 Скорость всех (vpn)"), callback_data="speed_all_vpn"),
    ]]
    await cb.message.edit_text("Choose server:" if lang == "en" else "Выбери сервер:", reply_markup=_with_home(buttons))


@router.callback_query(F.data.startswith("peers:"))
async def show_peers(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)
    server_name = cb.data.split(":", 1)[1]
    await cb.message.edit_text(f"⏳ {'Loading peers from' if lang == 'en' else 'Загружаю peers с сервера'} {server_name}...")

    peers = await get_all_peers_merged(server_name)
    if not peers:
        await cb.message.edit_text(
            "No peers.",
            reply_markup=_with_home([[InlineKeyboardButton(text="◀️ Назад / Back", callback_data=f"srv:{server_name}")]]),
        )
        return

    lines = [f"<b>{'Peers on' if lang == 'en' else 'Peers на сервере'} {server_name}:</b>", ""]
    for p in peers:
        if p["connected"]:
            status = "🟢 online" if lang == "en" else "🟢 онлайн"
        elif p.get("last_handshake", 0):
            status = (
                f"🔴 offline (last seen: {_format_last_seen(p['last_handshake'], lang)})"
                if lang == "en"
                else f"🔴 офлайн (был: {_format_last_seen(p['last_handshake'], lang)})"
            )
        else:
            status = "⚪ never connected" if lang == "en" else "⚪ ни разу не подключался"

        lines.append(
            f"<b>{p['name']}</b> {p['ip']}\n"
            f"   {status} | ↓{p['rx_mb']} ↑{p['tx_mb']} MB"
        )

    await cb.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="◀️ Назад / Back", callback_data=f"srv:{server_name}")]]),
    )


# ─── Ping ─────────────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("ping:"))
async def do_ping(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)
    server_name = cb.data.split(":", 1)[1]
    await cb.message.edit_text(f"⏳ {'Pinging' if lang == 'en' else 'Пингую'} {server_name}...")
    result = await ping_server(server_name)
    if result["success"] and result["ms"]:
        emoji = "🟢" if result["ms"] < 50 else ("🟡" if result["ms"] < 150 else "🔴")
        text = f"{emoji} <b>{server_name}</b>\n{'Ping' if lang == 'en' else 'Пинг'}: <b>{result['ms']:.1f} ms</b>"
    else:
        text = f"❌ <b>{server_name}</b> {'is not responding' if lang == 'en' else 'не отвечает'}"
    await cb.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="◀️ Назад", callback_data=f"srv:{server_name}")]]),
    )


@router.callback_query(F.data == "ping_all")
async def ping_all(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)
    await cb.message.edit_text("⏳ Pinging all servers..." if lang == "en" else "⏳ Пингую все серверы...")
    lines = ["<b>📶 Server ping:</b>", ""] if lang == "en" else ["<b>📶 Пинг серверов:</b>", ""]
    for s in SERVERS:
        result = await ping_server(s.name)
        if result["success"] and result["ms"]:
            emoji = "🟢" if result["ms"] < 50 else ("🟡" if result["ms"] < 150 else "🔴")
            lines.append(f"{emoji} {s.name}: <b>{result['ms']:.1f} ms</b>")
        else:
            lines.append(f"❌ {s.name}: {'unavailable' if lang == 'en' else 'недоступен'}")
    await cb.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="◀️ Назад / Back", callback_data="back_to_servers")]]),
    )


# ─── Speed ────────────────────────────────────────────────────────────────────


def _format_speed_result(server_name: str, result: dict, label: str, lang: str = "ru") -> str:
    if result.get("success"):
        dl = result.get("download_mbps", "—")
        ul = result.get("upload_mbps", "—")
        ping = result.get("ping_ms", "—")
        method = result.get("method", "")
        return (
            f"{label} <b>{server_name}</b>\n"
            f"⬇️ Download: <b>{dl} Mbit/s</b>\n"
            f"⬆️ Upload: <b>{ul} Mbit/s</b>\n"
            f"📶 Ping: {ping} ms\n"
            f"<i>{'method' if lang == 'en' else 'метод'}: {method}</i>"
        )
    diag = result.get("diagnostic")
    diag_line = f"\n<i>{diag}</i>" if diag else ""
    return f"{label} <b>{server_name}</b>\n❌ {result.get('error', 'error' if lang == 'en' else 'ошибка')}{diag_line}"


@router.callback_query(F.data.startswith("speed_host:"))
async def do_speed_host(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)
    server_name = cb.data.split(":", 1)[1]
    await cb.message.edit_text(f"⏳ {'Testing host speed on' if lang == 'en' else 'Тестирую скорость host'} {server_name}... ({'may take 30 sec' if lang == 'en' else 'может занять 30 сек'})")
    result = await speed_test_host(server_name)
    text = _format_speed_result(server_name, result, "⚡ Host", lang)
    await cb.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="◀️ Назад", callback_data=f"srv:{server_name}")]]),
    )


@router.callback_query(F.data.startswith("speed_vpn:"))
async def do_speed_vpn(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)
    server_name = cb.data.split(":", 1)[1]
    await cb.message.edit_text(f"⏳ {'Testing VPN-container speed on' if lang == 'en' else 'Тестирую скорость VPN-контейнера'} {server_name}... ({'may take 30-60 sec' if lang == 'en' else 'может занять 30-60 сек'})")
    result = await speed_test_vpn(server_name)
    text = _format_speed_result(server_name, result, "🔐 VPN", lang)
    await cb.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="◀️ Назад", callback_data=f"srv:{server_name}")]]),
    )


@router.callback_query(F.data.startswith("speed:"))
async def do_speed_legacy(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)
    server_name = cb.data.split(":", 1)[1]
    await cb.message.edit_text(f"⏳ {'Testing host and VPN on' if lang == 'en' else 'Тестирую host и VPN'} {server_name}... ({'up to 60 sec' if lang == 'en' else 'до 60 сек'})")
    both = await speed_test_both(server_name)
    text = (
        f"{_format_speed_result(server_name, both['host'], '⚡ Host', lang)}\n\n"
        f"{_format_speed_result(server_name, both['vpn'], '🔐 VPN', lang)}"
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
    lang = await _lang_for_user(cb.from_user)
    await cb.message.edit_text("⏳ Testing host speed on all servers... (up to 90s)" if lang == "en" else "⏳ Тестирую скорость всех серверов (host)... (до 90 сек)")
    lines = ["<b>⚡ Host speed on all servers:</b>", ""] if lang == "en" else ["<b>⚡ Скорость всех серверов (host):</b>", ""]
    for s in SERVERS:
        result = await speed_test_host(s.name)
        if result.get("success"):
            dl = result.get("download_mbps", "—")
            ul = result.get("upload_mbps", "—")
            lines.append(f"✅ {s.name}: ⬇️ <b>{dl}</b> ⬆️ <b>{ul}</b> Mbit/s")
        else:
            lines.append(f"❌ {s.name}: {result.get('error', 'error' if lang == 'en' else 'ошибка')}")
    await cb.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="◀️ Назад / Back", callback_data="back_to_servers")]]),
    )


@router.callback_query(F.data == "speed_all_vpn")
async def speed_all_vpn(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)
    await cb.message.edit_text("⏳ Testing VPN-container speed on all servers... (up to 120s)" if lang == "en" else "⏳ Тестирую скорость всех серверов (VPN-контейнер)... (до 120 сек)")
    lines = ["<b>🔐 VPN speed on all servers:</b>", ""] if lang == "en" else ["<b>🔐 Скорость всех серверов (VPN):</b>", ""]
    for s in SERVERS:
        result = await speed_test_vpn(s.name)
        if result.get("success"):
            dl = result.get("download_mbps", "—")
            ul = result.get("upload_mbps", "—")
            lines.append(f"✅ {s.name}: ⬇️ <b>{dl}</b> ⬆️ <b>{ul}</b> Mbit/s")
        else:
            lines.append(f"❌ {s.name}: {result.get('error', 'error' if lang == 'en' else 'ошибка')}")
    await cb.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="◀️ Назад / Back", callback_data="back_to_servers")]]),
    )


# ─── Server reboot ────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("reboot_ask:"))
async def reboot_ask(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)
    server_name = cb.data.split(":", 1)[1]
    await cb.message.edit_text(
        (
            f"⚠️ <b>Reboot confirmation</b>\n\n"
            f"Server: <b>{server_name}</b>\n"
            "Server will be unavailable for 1-3 minutes.\n"
            "Continue?"
            if lang == "en"
            else
            f"⚠️ <b>Подтверждение перезагрузки</b>\n\n"
            f"Сервер: <b>{server_name}</b>\n"
            "Сервер станет недоступен на 1-3 минуты.\n"
            "Продолжить?"
        ),
        parse_mode="HTML",
        reply_markup=_with_home([
            [
                InlineKeyboardButton(text="✅ Да, перезагрузить / Yes, reboot", callback_data=f"reboot_do:{server_name}"),
                InlineKeyboardButton(text="❌ Отмена / Cancel", callback_data=f"srv:{server_name}"),
            ]
        ]),
    )


@router.callback_query(F.data.startswith("reboot_do:"))
async def reboot_do(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)
    server_name = cb.data.split(":", 1)[1]
    await cb.message.edit_text(f"⏳ {'Sending reboot command to' if lang == 'en' else 'Отправляю команду перезагрузки на'} {server_name}...")
    result = await reboot_server(server_name)
    if result.get("success"):
        text = (
            f"✅ Reboot command sent: <b>{server_name}</b>\n"
            "You can check availability in 1-3 minutes."
            if lang == "en"
            else
            f"✅ Команда перезагрузки отправлена: <b>{server_name}</b>\n"
            "Проверить доступность можно через 1-3 минуты."
        )
    else:
        text = (
            f"❌ Failed to reboot {server_name}: {result.get('error', 'error')}"
            if lang == "en"
            else f"❌ Не удалось перезагрузить {server_name}: {result.get('error', 'ошибка')}"
        )
    await cb.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="◀️ К серверам / To servers", callback_data="back_to_servers")]]),
    )


# ─── Statistics ───────────────────────────────────────────────────────────────


def _is_paid_now(client, today: Optional[date] = None) -> bool:
    today = today or date.today()
    paid_until = _parse_date(client.paid_until)
    return bool(paid_until and paid_until >= today)


def _stats_kb(lang: str = "ru") -> InlineKeyboardMarkup:
    return _with_home([
        [
            InlineKeyboardButton(text=("✅ Paid" if lang == "en" else "✅ Оплатил"), callback_data="stats_paid"),
            InlineKeyboardButton(text=("⏳ Unpaid" if lang == "en" else "⏳ Не оплатил"), callback_data="stats_unpaid"),
        ],
        [InlineKeyboardButton(text=("🚫 Non-payers" if lang == "en" else "🚫 Не плательщики"), callback_data="stats_nonpayer")],
    ])


def _stats_back_kb(lang: str = "ru") -> InlineKeyboardMarkup:
    return _with_home([[InlineKeyboardButton(text=("◀️ To stats" if lang == "en" else "◀️ К статистике"), callback_data="stats_back")]])


def _stats_client_line(client, lang: str = "ru") -> str:
    username = f"@{html.escape(client.username)}" if client.username else "—"
    paid_until = _parse_date(client.paid_until)
    paid_until_text = paid_until.strftime("%d.%m.%Y") if paid_until else "—"
    return (
        f"• <b>{html.escape(client.name)}</b> | TG: <code>{client.telegram_id}</code> | {username}\n"
        +
        (
            f"  {_client_status(client, lang)} | until: {paid_until_text} | keys: {client.key_count} ({client.payable_key_count} billable)"
            if lang == "en"
            else f"  {_client_status(client, lang)} | до: {paid_until_text} | ключей: {client.key_count} ({client.payable_key_count} платн.)"
        )
    )


def _stats_clients_text(title: str, clients: List, lang: str = "ru") -> str:
    if not clients:
        return f"<b>{title}</b>\n\nList is empty." if lang == "en" else f"<b>{title}</b>\n\nСписок пуст."

    lines = [f"<b>{title}</b>", (f"Clients: <b>{len(clients)}</b>" if lang == "en" else f"Клиентов: <b>{len(clients)}</b>"), ""]
    lines.extend(_stats_client_line(c, lang) for c in clients)
    return "\n".join(lines)


async def _stats_summary_text(lang: str = "ru") -> str:
    clients = await get_all_clients()
    key_stats = await get_global_key_stats()

    total = len(clients)
    active = sum(1 for c in clients if c.active)
    billable_active = [c for c in clients if c.active and c.payable_key_count > 0]
    nonpayer_active = [c for c in clients if c.active and c.payable_key_count <= 0]

    paid = sum(1 for c in billable_active if _is_paid_now(c))
    waiting = sum(1 for c in billable_active if c.payment_status == "waiting_confirm")
    pending = max(0, len(billable_active) - paid - waiting)

    total_keys = key_stats["total_keys"]
    payable_keys = key_stats["payable_keys"]
    paused_keys = key_stats["paused_keys"]
    monthly = sum(c.monthly_fee for c in billable_active)

    return (
        (
            f"<b>📊 Statistics</b>\n\n"
            f"Total clients: <b>{total}</b>\n"
            f"Active clients: <b>{active}</b>\n\n"
            f"Paying clients: <b>{len(billable_active)}</b>\n"
            f"Non-payers: <b>{len(nonpayer_active)}</b>\n\n"
            f"🔑 Total keys: <b>{total_keys}</b>\n"
            f"💰 Billable keys: <b>{payable_keys}</b>\n"
            f"⏸ Paused keys: <b>{paused_keys}</b>\n"
            f"📦 Per-device tariff: <b>{_device_price_text()}</b>\n\n"
            f"✅ Paid: <b>{paid}</b>\n"
            f"🔄 Under review: <b>{waiting}</b>\n"
            f"⏳ Unpaid: <b>{pending}</b>\n\n"
            f"💰 Expected monthly revenue: <b>{monthly:.0f} ₽</b>"
            if lang == "en"
            else
            f"<b>📊 Статистика</b>\n\n"
            f"Всего клиентов: <b>{total}</b>\n"
            f"Активных клиентов: <b>{active}</b>\n\n"
            f"Платящих клиентов: <b>{len(billable_active)}</b>\n"
            f"Неплательщиков: <b>{len(nonpayer_active)}</b>\n\n"
            f"🔑 Ключей всего: <b>{total_keys}</b>\n"
            f"💰 Платных ключей: <b>{payable_keys}</b>\n"
            f"⏸ Остановленных ключей: <b>{paused_keys}</b>\n"
            f"📦 Тариф за устройство: <b>{_device_price_text()}</b>\n\n"
            f"✅ Оплачено: <b>{paid}</b>\n"
            f"🔄 На проверке: <b>{waiting}</b>\n"
            f"⏳ Не оплачено: <b>{pending}</b>\n\n"
            f"💰 Ожидаемый доход в месяц: <b>{monthly:.0f} ₽</b>"
        )
    )


@router.message(F.text.in_(ADMIN_STATS_TEXTS))
async def show_stats(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    lang = await _lang_for_user(msg.from_user)
    text = await _stats_summary_text(lang)
    await msg.answer(text, parse_mode="HTML", reply_markup=_stats_kb(lang))


@router.callback_query(F.data == "stats_back")
async def stats_back(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)
    text = await _stats_summary_text(lang)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=_stats_kb(lang))
    await cb.answer()


@router.callback_query(F.data == "stats_paid")
async def stats_paid(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    clients = await get_all_clients()
    paid_clients = [c for c in clients if c.active and c.payable_key_count > 0 and _is_paid_now(c)]
    paid_clients.sort(key=lambda c: c.name.lower())
    lang = await _lang_for_user(cb.from_user)
    text = _stats_clients_text("✅ Paid" if lang == "en" else "✅ Оплатил", paid_clients, lang)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=_stats_back_kb(lang))
    await cb.answer()


@router.callback_query(F.data == "stats_unpaid")
async def stats_unpaid(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    clients = await get_all_clients()
    unpaid_clients = [c for c in clients if c.active and c.payable_key_count > 0 and not _is_paid_now(c)]
    unpaid_clients.sort(key=lambda c: c.name.lower())
    lang = await _lang_for_user(cb.from_user)
    text = _stats_clients_text("⏳ Unpaid" if lang == "en" else "⏳ Не оплатил", unpaid_clients, lang)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=_stats_back_kb(lang))
    await cb.answer()


@router.callback_query(F.data == "stats_nonpayer")
async def stats_nonpayer(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    clients = await get_all_clients()
    nonpayer_clients = [c for c in clients if c.active and c.payable_key_count <= 0]
    nonpayer_clients.sort(key=lambda c: c.name.lower())
    lang = await _lang_for_user(cb.from_user)
    text = _stats_clients_text("🚫 Non-payers" if lang == "en" else "🚫 Не плательщики", nonpayer_clients, lang)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=_stats_back_kb(lang))
    await cb.answer()


# ─── Add client (FSM) ─────────────────────────────────────────────────────────


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
    lang = await _lang_for_user(cb.from_user)
    await state.clear()
    await cb.message.answer(
        "Введи <b>Telegram ID</b> или <b>@username</b> нового клиента:" if lang == "ru" else "Enter new client's <b>Telegram ID</b> or <b>@username</b>:",
        parse_mode="HTML",
        reply_markup=back_kb(lang),
    )
    await state.set_state(AddClientForm.telegram_id)
    await cb.answer()


@router.callback_query(F.data.startswith("create_from_key:"))
async def create_client_from_key(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)

    key_id = int(cb.data.split(":")[1])
    key = await get_key_by_id(key_id)
    if not key:
        await cb.answer("Ключ не найден" if lang == "ru" else "Key not found", show_alert=True)
        return
    if await is_key_linked_any(key_id):
        await cb.answer("Ключ уже привязан. Обнови список." if lang == "ru" else "Key is already linked. Refresh the list.", show_alert=True)
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
            f"{'Сервер' if lang == 'ru' else 'Server'}: <b>{key.server_name}</b>\n\n"
            + ("Введи <b>Telegram ID</b> или <b>@username</b> клиента:" if lang == "ru" else "Enter client's <b>Telegram ID</b> or <b>@username</b>:")
        ),
        parse_mode="HTML",
        reply_markup=back_kb(lang),
    )
    await cb.answer()


@router.message(F.text.in_(ADMIN_ADD_CLIENT_TEXTS))
@router.message(Command("add_client"))
async def cmd_add_client(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    lang = await _lang_for_user(msg.from_user)
    await msg.answer(
        "Введи <b>Telegram ID</b> или <b>@username</b> нового клиента:"
        if lang == "ru"
        else "Enter new client's <b>Telegram ID</b> or <b>@username</b>:",
        parse_mode="HTML",
        reply_markup=back_kb(lang),
    )
    await state.set_state(AddClientForm.telegram_id)


@router.message(F.text.in_(BACK_TEXTS))
async def go_back(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    lang = await _lang_for_user(msg.from_user)

    current = await state.get_state()
    if current is None:
        lang = await _lang_for_user(msg.from_user)
        await msg.answer("Ты уже в главном меню." if lang == "ru" else "You are already in main menu.", reply_markup=admin_main_kb(lang))
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
        str(AddClientForm.telegram_id): "Введи <b>Telegram ID</b> или <b>@username</b>:" if lang == "ru" else "Enter <b>Telegram ID</b> or <b>@username</b>:",
        str(AddClientForm.name): "Введи <b>имя</b> клиента:" if lang == "ru" else "Enter client <b>name</b>:",
        str(AddClientForm.username): "Введи <b>@username</b> (без @, или '-'):" if lang == "ru" else "Enter <b>@username</b> (without @, or '-'):",
        str(AddClientForm.server): "Выбери <b>сервер</b>:" if lang == "ru" else "Choose <b>server</b>:",
        str(PaymentMonthsForm.months): "Введи количество месяцев оплаты (целое число, от 1):" if lang == "ru" else "Enter payment months (integer, from 1):",
    }

    if current == str(PaymentMonthsForm.months):
        await state.clear()
        lang = await _lang_for_user(msg.from_user)
        await msg.answer("Отменено." if lang == "ru" else "Canceled.", reply_markup=admin_main_kb(lang))
        return

    data = await state.get_data()
    if current == str(AddClientForm.confirm) and data.get("bind_key_id"):
        await state.set_state(AddClientForm.username)
        await msg.answer(
            "Введи <b>@username</b> (без @, или '-'):",
            parse_mode="HTML",
            reply_markup=back_kb(lang),
        )
        return

    try:
        idx = [str(s) for s in states_order].index(current)
    except ValueError:
        idx = 0

    if idx <= 0:
        await state.clear()
        await msg.answer("Добавление отменено." if lang == "ru" else "Client creation canceled.", reply_markup=admin_main_kb(lang))
        return

    prev_state = states_order[idx - 1]
    await state.set_state(prev_state)

    prompt = prompts.get(str(prev_state), "Шаг назад" if lang == "ru" else "Step back")
    await msg.answer(prompt, parse_mode="HTML", reply_markup=back_kb(lang))


@router.message(AddClientForm.telegram_id)
async def add_tg_id(msg: Message, state: FSMContext):
    lang = await _lang_for_user(msg.from_user)
    if msg.text in ("◀️ Назад", "🏠 Главное меню"):
        return

    raw = (msg.text or "").strip()
    resolved_username = None
    tg_id = None

    try:
        tg_id = int(raw)
    except ValueError:
        username = raw.lstrip("@")
        if not username:
            await msg.answer("❌ Неверный формат. Введи Telegram ID или @username." if lang == "ru" else "❌ Invalid format. Enter Telegram ID or @username.")
            return

        try:
            chat = await msg.bot.get_chat(f"@{username}")
        except TelegramBadRequest:
            await msg.answer(
                "ℹ️ Не удалось получить Telegram ID через API.\n"
                "Продолжу по username: ID привяжется автоматически, когда клиент напишет /start."
            )
            resolved_username = username
        except Exception:
            logger.exception("failed to resolve username %s", username)
            await msg.answer(
                "ℹ️ Ошибка при проверке username через API.\n"
                "Продолжу по username: ID привяжется автоматически после /start."
            )
            resolved_username = username
        else:
            if getattr(chat, "type", None) != "private":
                await msg.answer("❌ Этот username не похож на личный аккаунт Telegram.")
                return

            tg_id = int(chat.id)
            resolved_username = (chat.username or username).lstrip("@")
            await msg.answer(
                f"✅ Нашёл пользователя: <code>{tg_id}</code> (@{resolved_username})",
                parse_mode="HTML",
            )

    if tg_id is not None:
        existing = await get_client_by_tg(tg_id)
        if existing:
            await msg.answer(
                f"❌ Клиент с таким Telegram ID уже есть: <b>{existing.name}</b> (id={existing.id})",
                parse_mode="HTML",
            )
            return

    if resolved_username:
        username_taken = await get_client_by_username(resolved_username)
        if username_taken:
            await msg.answer(
                f"❌ Клиент с username @{resolved_username} уже есть: <b>{username_taken.name}</b> (id={username_taken.id})",
                parse_mode="HTML",
            )
            return

    await state.update_data(telegram_id=tg_id, username=resolved_username)
    data = await state.get_data()
    suggested_name = data.get("suggested_name")
    prompt = "Введи <b>имя</b> клиента:"
    if suggested_name:
        prompt = f"Введи <b>имя</b> клиента (например: <code>{suggested_name}</code>):"
    await msg.answer(prompt, parse_mode="HTML", reply_markup=back_kb(lang))
    await state.set_state(AddClientForm.name)


@router.message(AddClientForm.name)
async def add_name(msg: Message, state: FSMContext):
    lang = await _lang_for_user(msg.from_user)
    if msg.text in ("◀️ Назад", "🏠 Главное меню"):
        return

    await state.update_data(name=msg.text.strip())
    data = await state.get_data()
    if data.get("username"):
        await msg.answer(
            f"✅ Username подтянул автоматически: @{data['username']}",
            parse_mode="HTML",
        )
        await _add_client_show_next_step(msg, state)
        return

    await msg.answer(
        "Введи <b>@username</b> (без @, или '-' если нет):",
        parse_mode="HTML",
        reply_markup=back_kb(lang),
    )
    await state.set_state(AddClientForm.username)


async def _add_client_show_next_step(msg: Message, state: FSMContext):
    lang = await _lang_for_user(msg.from_user)
    data = await state.get_data()
    tg_id_text = (
        f"<code>{data['telegram_id']}</code>"
        if data.get("telegram_id") is not None
        else "<i>автопривязка после /start</i>"
    )
    bind_key_id = data.get("bind_key_id")
    if bind_key_id:
        key = await get_key_by_id(int(bind_key_id))
        if not key:
            await state.clear()
            await msg.answer("❌ Ключ не найден. Начни снова." if lang == "ru" else "❌ Key not found. Start again.", reply_markup=admin_main_kb(lang))
            return
        if await is_key_linked_any(int(bind_key_id)):
            await state.clear()
            await msg.answer("❌ Ключ уже привязан к другому клиенту." if lang == "ru" else "❌ Key is already linked to another client.", reply_markup=admin_main_kb(lang))
            return

        await state.update_data(server=key.server_name)
        data = await state.get_data()
        summary = (
            f"<b>Проверь данные:</b>\n\n"
            f"Telegram ID: {tg_id_text}\n"
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
    await msg.answer("Выбери <b>сервер</b>:" if lang == "ru" else "Choose a <b>server</b>:", parse_mode="HTML", reply_markup=kb)
    await state.set_state(AddClientForm.server)


@router.message(AddClientForm.username)
async def add_username(msg: Message, state: FSMContext):
    lang = await _lang_for_user(msg.from_user)
    if msg.text in ("◀️ Назад", "🏠 Главное меню"):
        return

    username = msg.text.strip().lstrip("@")
    await state.update_data(username=None if username == "-" else username)
    await _add_client_show_next_step(msg, state)


@router.callback_query(AddClientForm.server, F.data.startswith("sel_srv:"))
async def add_server(cb: CallbackQuery, state: FSMContext):
    lang = await _lang_for_user(cb.from_user)
    server_name = cb.data.split(":", 1)[1]
    await state.update_data(server=server_name)

    data = await state.get_data()
    tg_id_text = (
        f"<code>{data['telegram_id']}</code>"
        if data.get("telegram_id") is not None
        else "<i>автопривязка после /start</i>"
    )
    summary = (
        (
            f"<b>Review details:</b>\n\n"
            f"Telegram ID: {tg_id_text}\n"
            f"Name: {data['name']}\n"
            f"Username: @{data.get('username') or '-'}\n"
            f"Server: {server_name}\n\n"
            f"Default tariff: {_device_price_text()} per payable device\n"
            f"(amount is calculated by linked keys)\n\n"
            f"Create WireGuard key now?"
        )
        if lang == "en"
        else
        (
            f"<b>Проверь данные:</b>\n\n"
            f"Telegram ID: {tg_id_text}\n"
            f"Имя: {data['name']}\n"
            f"Username: @{data.get('username') or '-'}\n"
            f"Сервер: {server_name}\n\n"
            f"Тариф по умолчанию: {_device_price_text()} за платное устройство\n"
            f"(сумма считается по связанным ключам)\n\n"
            f"Создать WireGuard ключ сразу?"
        )
    )

    kb = _with_home([
        [
            InlineKeyboardButton(text="✅ Create key" if lang == "en" else "✅ Создать ключ", callback_data="add_with_wg"),
            InlineKeyboardButton(text="📝 Without key" if lang == "en" else "📝 Без ключа", callback_data="add_no_wg"),
        ],
        [InlineKeyboardButton(text="❌ Cancel" if lang == "en" else "❌ Отмена", callback_data="add_cancel")],
    ])

    await cb.message.edit_text(summary, parse_mode="HTML", reply_markup=kb)
    await state.set_state(AddClientForm.confirm)


@router.callback_query(AddClientForm.confirm, F.data.in_({"add_with_wg", "add_no_wg", "add_bind_key", "add_cancel"}))
async def add_confirm(cb: CallbackQuery, state: FSMContext):
    lang = await _lang_for_user(cb.from_user)
    if cb.data == "add_cancel":
        await state.clear()
        await cb.message.edit_text("❌ Отменено." if lang == "ru" else "❌ Canceled.")
        await cb.message.answer(tr(lang, "main_menu_title"), reply_markup=admin_main_kb(lang))
        return

    data = await state.get_data()
    await state.clear()

    await cb.message.edit_text("⏳ Создаю клиента...")
    if lang == "en":
        await cb.message.edit_text("⏳ Creating client...")

    wg_data = None
    if cb.data == "add_with_wg":
        wg_data = await add_peer(data["server"], data["name"])
        if not wg_data:
            await cb.message.answer("⚠️ Failed to create key. Adding client without key." if lang == "en" else "⚠️ Не удалось создать ключ. Добавляю клиента без ключа.")

    try:
        if cb.data == "add_bind_key":
            bind_key_id = int(data.get("bind_key_id", 0))
            key = await get_key_by_id(bind_key_id)
            if not key:
                await cb.message.edit_text("❌ Key not found. Try again." if lang == "en" else "❌ Ключ не найден. Попробуй снова.")
                return
            if await is_key_linked_any(bind_key_id):
                await cb.message.edit_text("❌ Key is already linked to another client." if lang == "en" else "❌ Ключ уже привязан к другому клиенту.")
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
        await cb.message.edit_text(f"❌ Failed to add client: {e}" if lang == "en" else f"❌ Не удалось добавить клиента: {e}")
        return

    ok_text = f"✅ <b>{data['name']}</b> added (id={client_id})!" if lang == "en" else f"✅ <b>{data['name']}</b> добавлен (id={client_id})!"
    if cb.data == "add_bind_key":
        ok_text += "\nKey linked successfully." if lang == "en" else "\nКлюч успешно привязан."
    await cb.message.edit_text(ok_text, parse_mode="HTML")
    await cb.message.answer(tr(lang, "main_menu_title"), reply_markup=admin_main_kb(lang))

    if wg_data:
        config_file = BufferedInputFile(
            wg_data["config_text"].encode(),
            filename=f"vpn_{data['name'].replace(' ', '_')}.conf",
        )
        await cb.message.answer_document(
            config_file,
            caption=(
                f"🔑 Config for <b>{data['name']}</b>\nIP: {wg_data['client_ip']}"
                if lang == "en"
                else f"🔑 Конфиг для <b>{data['name']}</b>\nIP: {wg_data['client_ip']}"
            ),
            parse_mode="HTML",
        )
        if wg_data.get("vpn_uri"):
            await cb.message.answer(wg_data["vpn_uri"], disable_web_page_preview=True)


# ─── Payment confirm/reject ───────────────────────────────────────────────────


@router.callback_query(F.data.startswith("pay_choose:"))
async def pay_choose(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    client_id = int(cb.data.split(":")[1])
    await _show_months_selector(cb.message, client_id, edit=True, lang=await _lang_for_user(cb.from_user))


@router.callback_query(F.data.startswith("confirm_pay:"))
async def confirm_pay_select_month(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    client_id = int(cb.data.split(":")[1])
    await _show_months_selector(cb.message, client_id, edit=True, lang=await _lang_for_user(cb.from_user))


@router.callback_query(F.data.startswith("confirm_pay_custom:"))
async def confirm_pay_custom(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    lang = await _lang_for_user(cb.from_user)

    client_id = int(cb.data.split(":")[1])
    await state.set_state(PaymentMonthsForm.months)
    await state.update_data(pay_client_id=client_id)
    await cb.message.answer(
        ("Введи количество месяцев оплаты (целое число, от 1):" if lang == "ru" else "Enter payment months (integer, from 1):"),
        reply_markup=back_kb(lang),
    )
    await cb.answer()


@router.message(PaymentMonthsForm.months)
async def payment_months_input(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    lang = await _lang_for_user(msg.from_user)
    if msg.text in ("◀️ Назад", "🏠 Главное меню"):
        return

    try:
        months = int(msg.text.strip())
    except ValueError:
        await msg.answer("❌ Введи целое число месяцев (например 4)." if lang == "ru" else "❌ Enter an integer number of months (e.g. 4).")
        return

    if months < 1 or months > 60:
        await msg.answer("❌ Допустимо от 1 до 60 месяцев." if lang == "ru" else "❌ Allowed range is 1 to 60 months.")
        return

    data = await state.get_data()
    client_id = int(data.get("pay_client_id"))
    await state.clear()

    waiting = await msg.answer("⏳ Подтверждаю оплату...")
    await _apply_payment_confirmation(msg.bot, client_id, months, waiting)
    await msg.answer(tr(lang, "main_menu_title"), reply_markup=admin_main_kb(lang))


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
        await cb.answer("Клиент не найден / Client not found")
        return

    await update_payment_status(client_id, "pending")
    await log_payment(client_id, "rejected", note="Admin rejected")

    await cb.message.edit_text(
        f"❌ Оплата от <b>{client.name}</b> отклонена.",
        parse_mode="HTML",
        reply_markup=_with_home([[InlineKeyboardButton(text="👤 Открыть клиента / Open client", callback_data=f"client_card:{client.id}")]]),
    )

    try:
        client_lang = normalize_lang(await get_user_lang(client.telegram_id))
        kb = None
        if client.payable_key_count > 0:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(
                        text=("✅ I paid" if client_lang == "en" else "✅ Я оплатил"),
                        callback_data=f"paid:{client.id}",
                    )
                ]]
            )
        await cb.bot.send_message(
            client.telegram_id,
            (
                "❌ <b>Payment not confirmed.</b>\nPlease verify your transfer and press the button again."
                if client_lang == "en"
                else "❌ <b>Оплата не подтверждена.</b>\nПроверь перевод и нажми кнопку снова."
            ),
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception:
        logger.exception("failed to notify rejected payment for client %s", client.id)


# ─── Manual peer removal (optional) ───────────────────────────────────────────


@router.callback_query(F.data.startswith("remove_peer:"))
async def remove_peer_manual(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    _, server_name, pubkey = cb.data.split(":", 2)
    db_key = await get_key_by_server_pubkey(server_name, pubkey)
    linked_clients = await get_key_access_clients(db_key.id) if db_key else []
    key_name = db_key.key_name if db_key and db_key.key_name else pubkey[:8]

    ok = await remove_peer(server_name, pubkey)
    if ok:
        if db_key:
            await delete_key_record(db_key.id)
            for linked in linked_clients:
                await _notify_client_key_deleted(cb.bot, linked.client_id, key_name, server_name)
        await cb.answer("Peer удалён / Peer removed")
    else:
        await cb.answer("Не удалось удалить peer / Failed to remove peer")


@router.message(F.text)
async def support_send_from_admin(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    lang = await _lang_for_user(msg.from_user)

    # Do not interfere with FSM flows (add client, months input, etc.).
    if await state.get_state():
        return

    target = get_admin_target(msg.from_user.id)
    if not target:
        return

    text = (msg.text or "").strip()
    if not text or text in {
        "🏠 Главное меню",
        "🏠 Main Menu",
        "◀️ Назад",
        "◀️ Back",
        "💬 Поддержка",
        "💬 Support",
        "❌ Закрыть диалог",
        "❌ Close dialog",
    }:
        return

    sender_name = html.escape(msg.from_user.full_name or "Администратор")
    sender_username = f"@{html.escape(msg.from_user.username)}" if msg.from_user.username else "—"
    safe_text = html.escape(text)[:3500]

    if target == SUPPORT_BROADCAST_TARGET:
        clients = await get_all_clients()
        unique_clients = {}
        for c in clients:
            if c.telegram_id in ADMIN_IDS:
                continue
            unique_clients[c.telegram_id] = c

        delivered = 0
        failed = 0
        for c in unique_clients.values():
            payload = (
                "📢 <b>Сообщение от поддержки</b>\n\n"
                f"{safe_text}\n\n"
                f"<i>Отправил: {sender_name} ({sender_username})</i>"
            )
            try:
                await msg.bot.send_message(
                    c.telegram_id,
                    payload,
                    parse_mode="HTML",
                    reply_markup=_client_support_kb_for_admin_send(show_pay_button=c.payable_key_count > 0),
                )
                open_client_dialog(int(c.telegram_id))
                delivered += 1
            except Exception:
                failed += 1
                logger.exception("failed to broadcast support message to client %s", c.id)

        await msg.answer(
            (
                "✅ Рассылка завершена.\n"
                f"Доставлено: <b>{delivered}</b>\n"
                f"Ошибок: <b>{failed}</b>"
            ),
            parse_mode="HTML",
            reply_markup=_admin_support_dialog_kb(lang),
        )
        return

    client = await get_client_by_tg(int(target))
    if not client:
        clear_admin_target(msg.from_user.id)
        await msg.answer("❌ Клиент не найден. Выбери диалог заново." if lang == "ru" else "❌ Client not found. Pick dialog again.", reply_markup=admin_main_kb(lang))
        return

    open_client_dialog(int(client.telegram_id))
    payload = (
        "💬 <b>Ответ поддержки</b>\n\n"
        f"{safe_text}\n\n"
        f"<i>Администратор: {sender_name} ({sender_username})</i>"
    )
    try:
        await msg.bot.send_message(
            client.telegram_id,
            payload,
            parse_mode="HTML",
            reply_markup=_client_support_kb_for_admin_send(show_pay_button=client.payable_key_count > 0),
        )
    except Exception:
        logger.exception("failed to send support message to client %s", client.id)
        await msg.answer("❌ Не удалось отправить сообщение клиенту." if lang == "ru" else "❌ Failed to send message to client.", reply_markup=_admin_support_dialog_kb(lang))
        return

    await msg.answer(
        (f"✅ Sent to client <b>{html.escape(client.name)}</b>." if lang == "en" else f"✅ Отправлено клиенту <b>{html.escape(client.name)}</b>."),
        parse_mode="HTML",
        reply_markup=_admin_support_dialog_kb(lang),
    )
    lang = await _lang_for_user(cb.from_user)
    lang = await _lang_for_user(cb.from_user)
    lang = await _lang_for_user(cb.from_user)
