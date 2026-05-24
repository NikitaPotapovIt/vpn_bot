"""VPN client handlers."""

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (Message, CallbackQuery,
                            ReplyKeyboardMarkup, KeyboardButton,
                            InlineKeyboardMarkup, InlineKeyboardButton)
from database import (
    get_client_by_tg,
    get_unbound_client_by_username,
    update_client_fields,
    update_payment_status,
    get_client_server_names,
    get_user_lang,
    set_user_lang,
)
from scheduler import notify_payment_claimed
from config import ADMIN_IDS
import logging
import html
from datetime import datetime, date
from support_dialog import (
    open_client_dialog,
    close_client_dialog,
    is_client_dialog_open,
)
from i18n import tr, normalize_lang

router = Router()
logger = logging.getLogger(__name__)

# Important: do not intercept admin updates in the client router.
# Otherwise admin menu text buttons can be swallowed by this router
# (it is attached before admin_router).
router.message.filter(~F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(~F.from_user.id.in_(ADMIN_IDS))

CLIENT_STATUS_TEXTS = {"📊 Мой статус", "📊 My status"}
CLIENT_PAID_TEXTS = {"✅ Я оплатил", "✅ I paid"}
SUPPORT_OPEN_TEXTS = {"💬 Написать в поддержку", "💬 Contact support"}
SUPPORT_CLOSE_TEXTS = {"❌ Закрыть диалог", "❌ Close dialog"}
LANG_BUTTON_TEXTS = {"🌐 Язык", "🌐 Language"}


async def _lang_for_user(user) -> str:
    saved = await get_user_lang(user.id)
    if saved:
        return normalize_lang(saved)
    detected = normalize_lang(getattr(user, "language_code", None))
    await set_user_lang(user.id, detected)
    return detected


def _parse_date(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


def _status_human(client) -> str:
    lang = normalize_lang(getattr(client, "lang", None))
    if not _has_payable_keys(client):
        return "🚫 Payment is not required" if lang == "en" else "🚫 Оплата не требуется"
    paid_until = _parse_date(client.paid_until)
    if paid_until and paid_until >= date.today():
        return (
            f"✅ Paid until {paid_until.strftime('%d.%m.%Y')}"
            if lang == "en"
            else f"✅ Оплачено до {paid_until.strftime('%d.%m.%Y')}"
        )
    if client.payment_status == "paid":
        return "⏳ Renewal pending" if lang == "en" else "⏳ Ожидает продления"
    return {
        "paid": "✅ Paid" if lang == "en" else "✅ Оплачено",
        "pending": "⏳ Awaiting payment" if lang == "en" else "⏳ Ожидает оплаты",
        "waiting_confirm": "🔄 Under review" if lang == "en" else "🔄 Проверяется",
        "overdue": "🔴 Overdue" if lang == "en" else "🔴 Просрочено",
    }.get(client.payment_status, client.payment_status)


def _has_payable_keys(client) -> bool:
    return int(getattr(client, "payable_key_count", 0) or 0) > 0


def client_kb(lang: str, show_pay_button: bool = True, support_mode: bool = False) -> ReplyKeyboardMarkup:
    row = [KeyboardButton(text=tr(lang, "client_status_btn"))]
    if show_pay_button:
        row.append(KeyboardButton(text=tr(lang, "client_paid_btn")))
    keyboard = [row, [KeyboardButton(text=tr(lang, "support_open"))], [KeyboardButton(text=tr(lang, "lang_button"))]]
    if support_mode:
        keyboard.append([KeyboardButton(text=tr(lang, "support_close"))])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
    )


def _admin_support_kb(client_id: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=tr(lang, "support_reply_btn"), callback_data=f"support_reply:{client_id}"),
        InlineKeyboardButton(text=tr(lang, "support_close_btn"), callback_data=f"support_close:{client_id}"),
    ]])


async def _notify_admins_support_message(bot, client, text: str):
    username = f"@{html.escape(client.username)}" if client.username else "—"
    safe_text = html.escape(text.strip())[:3500]
    for admin_id in ADMIN_IDS:
        try:
            admin_lang = normalize_lang(await get_user_lang(admin_id))
            payload = (
                f"{tr(admin_lang, 'support_message_title')}\n\n"
                f"{tr(admin_lang, 'support_from')}: <b>{html.escape(client.name)}</b>\n"
                f"TG: <code>{client.telegram_id}</code> | {username}\n\n"
                f"{safe_text}"
            )
            kb = _admin_support_kb(client.id, admin_lang)
            await bot.send_message(admin_id, payload, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.exception("failed to forward support message to admin %s", admin_id)


async def _servers_line(client) -> str:
    lang = normalize_lang(await get_user_lang(client.telegram_id))
    servers = await get_client_server_names(client.id, active_only=True)
    if not servers:
        servers = [client.server_name]
    if len(servers) == 1:
        return f"🖥 Server: {servers[0]}" if lang == "en" else f"🖥 Сервер: {servers[0]}"
    return f"🖥 Servers: {', '.join(servers)}" if lang == "en" else f"🖥 Серверы: {', '.join(servers)}"


async def _resolve_client_for_user(user) -> object:
    client = await get_client_by_tg(user.id)
    if client:
        return client

    username = (user.username or "").strip()
    if not username:
        return None

    unbound = await get_unbound_client_by_username(username)
    if not unbound:
        return None

    await update_client_fields(unbound.id, telegram_id=int(user.id), username=username.lstrip("@"))
    return await get_client_by_tg(user.id)

@router.message(Command("start"))
async def cmd_start(msg: Message):
    # Admins are handled in admin.py
    if msg.from_user.id in ADMIN_IDS:
        return

    lang = await _lang_for_user(msg.from_user)
    client = await _resolve_client_for_user(msg.from_user)
    if client:
        status_text = _status_human(client)
        has_payable_keys = _has_payable_keys(client)
        servers_line = await _servers_line(client)

        await msg.answer(
            f"👋 <b>{'Привет' if lang == 'ru' else 'Hello'}, {client.name}!</b>\n\n"
            f"{servers_line}\n"
            f"{'📱 Устройств' if lang == 'ru' else '📱 Devices'}: {client.devices}\n"
            f"{'💰 Оплата' if lang == 'ru' else '💰 Payment'}: {client.monthly_fee:.0f} ₽/{'мес' if lang == 'ru' else 'mo'}\n"
            f"{'Статус' if lang == 'ru' else 'Status'}: {status_text}",
            parse_mode="HTML",
            reply_markup=client_kb(
                lang=lang,
                show_pay_button=has_payable_keys,
                support_mode=is_client_dialog_open(msg.from_user.id),
            ),
        )
    else:
        await msg.answer(tr(lang, "client_not_registered_long"))

@router.message(F.text.in_(CLIENT_STATUS_TEXTS))
@router.message(Command("status"))
async def cmd_status(msg: Message):
    if msg.from_user.id in ADMIN_IDS:
        return
    lang = await _lang_for_user(msg.from_user)
    client = await _resolve_client_for_user(msg.from_user)
    if not client:
        await msg.answer(tr(lang, "client_not_registered"))
        return

    active = "🟢 active" if (lang == "en" and client.active) else ("🔴 disabled" if lang == "en" else ("🟢 активен" if client.active else "🔴 отключён"))
    disc = (
        f"\n⚠️ Planned disconnect: {client.disconnect_date}"
        if (lang == "en" and client.disconnect_date)
        else (f"\n⚠️ Плановое отключение: {client.disconnect_date}" if client.disconnect_date else "")
    )
    paid_until = _parse_date(client.paid_until)
    paid_line = (
        f"\n📅 Paid until: {paid_until.strftime('%d.%m.%Y')}"
        if (lang == "en" and paid_until)
        else (f"\n📅 Оплачено до: {paid_until.strftime('%d.%m.%Y')}" if paid_until else "")
    )
    has_payable_keys = _has_payable_keys(client)
    servers_line = await _servers_line(client)

    kb = None
    needs_payment = has_payable_keys and (
        client.payment_status in ("pending", "overdue")
        or (client.payment_status == "paid" and (not paid_until or paid_until < date.today()))
    )
    if needs_payment:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=tr(lang, "client_paid_btn"), callback_data=f"paid:{client.id}")
        ]])

    await msg.answer(
        f"📊 <b>{'Статус подписки' if lang == 'ru' else 'Subscription status'}</b>\n\n"
        f"{servers_line}\n"
        f"VPN: {active}\n"
        f"{'Оплата' if lang == 'ru' else 'Payment'}: {_status_human(client)}"
        f"{paid_line}"
        f"{disc}",
        parse_mode="HTML",
        reply_markup=kb or client_kb(
            lang=lang,
            show_pay_button=has_payable_keys,
            support_mode=is_client_dialog_open(msg.from_user.id),
        ),
    )

@router.message(F.text.in_(CLIENT_PAID_TEXTS))
async def btn_paid(msg: Message):
    if msg.from_user.id in ADMIN_IDS:
        return
    lang = await _lang_for_user(msg.from_user)
    client = await _resolve_client_for_user(msg.from_user)
    if not client:
        await msg.answer(tr(lang, "client_not_registered"))
        return
    await _process_paid(msg.bot, client, lang=lang, reply=msg)

@router.callback_query(F.data.startswith("paid:"))
async def client_paid(cb: CallbackQuery):
    lang = await _lang_for_user(cb.from_user)
    client_id = int(cb.data.split(":")[1])
    client = await _resolve_client_for_user(cb.from_user)
    if not client or client.id != client_id:
        await cb.answer(tr(lang, "auth_error"), show_alert=True)
        return
    await _process_paid(cb.bot, client, lang=lang, callback=cb)

async def _process_paid(bot, client, lang: str, reply=None, callback=None):
    if not _has_payable_keys(client):
        text = "Payment is not required for this account." if lang == "en" else "Для этого аккаунта оплата не требуется."
        if callback:
            await callback.answer(text, show_alert=True)
        else:
            await reply.answer(
                text,
                reply_markup=client_kb(lang=lang, show_pay_button=False, support_mode=is_client_dialog_open(client.telegram_id)),
            )
        return

    if client.payment_status == "waiting_confirm":
        text = "Payment is already sent for review! Please wait for confirmation. 🔄" if lang == "en" else "Оплата уже отправлена на проверку! Ожидай подтверждения. 🔄"
        if callback:
            await callback.answer(text, show_alert=True)
        else:
            await reply.answer(text)
        return

    if client.payment_status == "paid":
        text = "Your payment is already confirmed ✅" if lang == "en" else "Твоя оплата уже подтверждена ✅"
        if callback:
            await callback.answer(text, show_alert=True)
        else:
            await reply.answer(text)
        return

    await update_payment_status(client.id, "waiting_confirm")
    await notify_payment_claimed(bot, client)

    text = (
        "🔄 <b>Request sent!</b>\n\n"
        "An administrator will review and confirm your payment."
        if lang == "en"
        else "🔄 <b>Заявка отправлена!</b>\n\nАдминистратор проверит и подтвердит оплату."
    )
    if callback:
        await callback.message.edit_text(text, parse_mode="HTML")
        await callback.answer(tr(lang, "paid_sent"))
    else:
        await reply.answer(
            text,
            parse_mode="HTML",
            reply_markup=client_kb(
                lang=lang,
                show_pay_button=_has_payable_keys(client),
                support_mode=is_client_dialog_open(client.telegram_id),
            ),
        )


@router.message(F.text.in_(SUPPORT_OPEN_TEXTS))
async def open_support_dialog(msg: Message):
    if msg.from_user.id in ADMIN_IDS:
        return
    lang = await _lang_for_user(msg.from_user)
    client = await _resolve_client_for_user(msg.from_user)
    if not client:
        await msg.answer(tr(lang, "client_not_registered"))
        return

    already_open = is_client_dialog_open(client.telegram_id)
    open_client_dialog(client.telegram_id)

    if not already_open:
        open_text = (
            "💬 <b>Client opened support dialog</b>\n\n"
            f"Client: <b>{html.escape(client.name)}</b>\n"
            f"TG: <code>{client.telegram_id}</code>\n"
            f"Username: @{html.escape(client.username) if client.username else '-'}"
        )
        for admin_id in ADMIN_IDS:
            try:
                admin_lang = normalize_lang(await get_user_lang(admin_id))
                kb = _admin_support_kb(client.id, admin_lang)
                await msg.bot.send_message(admin_id, open_text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                logger.exception("failed to notify admin %s about support open", admin_id)

    await msg.answer(
        "✅ Support dialog is open.\nWrite your message and an administrator will reply here."
        if lang == "en"
        else "✅ Диалог с поддержкой открыт.\nНапиши сообщение, и администратор ответит здесь.",
        reply_markup=client_kb(lang=lang, show_pay_button=_has_payable_keys(client), support_mode=True),
    )


@router.message(F.text.in_(SUPPORT_CLOSE_TEXTS))
async def close_support_dialog_client(msg: Message):
    if msg.from_user.id in ADMIN_IDS:
        return
    lang = await _lang_for_user(msg.from_user)
    client = await _resolve_client_for_user(msg.from_user)
    if not client:
        await msg.answer(tr(lang, "client_not_registered"))
        return

    was_open = is_client_dialog_open(client.telegram_id)
    close_client_dialog(client.telegram_id)
    await msg.answer(
        "✅ Support dialog is closed." if lang == "en" else "✅ Диалог с поддержкой закрыт.",
        reply_markup=client_kb(lang=lang, show_pay_button=_has_payable_keys(client), support_mode=False),
    )
    if was_open:
        notice = (
            "ℹ️ <b>Client closed support dialog</b>\n\n"
            f"Client: <b>{html.escape(client.name)}</b>\n"
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
    lang = await _lang_for_user(msg.from_user)
    client = await _resolve_client_for_user(msg.from_user)
    if not client:
        return
    if not is_client_dialog_open(client.telegram_id):
        return

    text = (msg.text or "").strip()
    if not text or text in CLIENT_STATUS_TEXTS | CLIENT_PAID_TEXTS | SUPPORT_OPEN_TEXTS | SUPPORT_CLOSE_TEXTS | LANG_BUTTON_TEXTS:
        return

    await _notify_admins_support_message(msg.bot, client, text)
    await msg.answer(
        "📨 Message sent to support." if lang == "en" else "📨 Сообщение отправлено в поддержку.",
        reply_markup=client_kb(lang=lang, show_pay_button=_has_payable_keys(client), support_mode=True),
    )


@router.message(F.text.in_(LANG_BUTTON_TEXTS))
@router.message(Command("language"))
async def language_menu(msg: Message):
    lang = await _lang_for_user(msg.from_user)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Русский", callback_data="lang_set:ru"),
                InlineKeyboardButton(text="English", callback_data="lang_set:en"),
            ]
        ]
    )
    await msg.answer(tr(lang, "choose_language"), reply_markup=kb)


@router.callback_query(F.data.startswith("lang_set:"))
async def language_set(cb: CallbackQuery):
    lang = normalize_lang(cb.data.split(":", 1)[1])
    await set_user_lang(cb.from_user.id, lang)
    client = await _resolve_client_for_user(cb.from_user)
    has_pay = _has_payable_keys(client) if client else True
    support_mode = is_client_dialog_open(cb.from_user.id)
    await cb.message.answer(
        tr(lang, "lang_saved_ru" if lang == "ru" else "lang_saved_en"),
        reply_markup=client_kb(lang=lang, show_pay_button=has_pay, support_mode=support_mode),
    )
    await cb.answer()
