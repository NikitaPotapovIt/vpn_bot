"""Simple RU/EN localization for bot UI."""

from typing import Optional

LANG_RU = "ru"
LANG_EN = "en"
SUPPORTED_LANGS = {LANG_RU, LANG_EN}


TEXTS = {
    "lang_button": {"ru": "🌐 Язык", "en": "🌐 Language"},
    "choose_language": {"ru": "Выбери язык интерфейса:", "en": "Choose interface language:"},
    "lang_saved_ru": {"ru": "✅ Язык переключен: Русский", "en": "✅ Language switched: Russian"},
    "lang_saved_en": {"ru": "✅ Язык переключен: English", "en": "✅ Language switched: English"},

    "home_menu": {"ru": "🏠 Главное меню", "en": "🏠 Main Menu"},
    "back": {"ru": "◀️ Назад", "en": "◀️ Back"},

    "support_menu": {"ru": "💬 Поддержка", "en": "💬 Support"},
    "support_open": {"ru": "💬 Написать в поддержку", "en": "💬 Contact support"},
    "support_close": {"ru": "❌ Закрыть диалог", "en": "❌ Close dialog"},

    "client_status_btn": {"ru": "📊 Мой статус", "en": "📊 My status"},
    "client_paid_btn": {"ru": "✅ Я оплатил", "en": "✅ I paid"},
    "client_not_registered": {
        "ru": "❌ Ты не зарегистрирован.",
        "en": "❌ You are not registered.",
    },
    "client_not_registered_long": {
        "ru": "Привет! Ты не зарегистрирован в системе.\nОбратись к администратору для подключения.",
        "en": "Hi! You are not registered in the system.\nContact an administrator to get access.",
    },
    "auth_error": {"ru": "❌ Ошибка авторизации", "en": "❌ Authorization error"},
    "paid_sent": {"ru": "Заявка отправлена!", "en": "Request sent!"},
    "support_reply_btn": {"ru": "✍️ Ответить", "en": "✍️ Reply"},
    "support_close_btn": {"ru": "❌ Закрыть диалог", "en": "❌ Close dialog"},
    "support_message_title": {"ru": "💬 <b>Сообщение в поддержку</b>", "en": "💬 <b>Support message</b>"},
    "support_from": {"ru": "От", "en": "From"},
    "support_paid_btn_mixed": {"ru": "✅ Я оплатил / I paid", "en": "✅ I paid / Я оплатил"},
    "admin_confirm_btn": {"ru": "✅ Подтвердить", "en": "✅ Confirm"},
    "admin_trial_btn": {"ru": "🎁 Тестовый период", "en": "🎁 Trial period"},
    "admin_reject_btn": {"ru": "❌ Отклонить", "en": "❌ Reject"},
    "billing_cycle_started_title": {"ru": "📅 <b>Начало расчётного периода</b>", "en": "📅 <b>Billing cycle started</b>"},
    "billing_cycle_sent_count": {"ru": "Разослано напоминаний: {count} клиентам", "en": "Reminders sent: {count} clients"},
    "payment_claim_title": {"ru": "💳 <b>Заявка на оплату</b>", "en": "💳 <b>Payment claim</b>"},
    "payment_claim_client": {"ru": "Клиент: <b>{name}</b> ({username})", "en": "Client: <b>{name}</b> ({username})"},
    "payment_claim_billable_keys": {"ru": "Платных ключей: {count}", "en": "Billable keys: {count}"},
    "payment_claim_tariff": {"ru": "Tariff: {price:.0f} ₽/device", "en": "Tariff: {price:.0f} ₽/device"},
    "payment_claim_monthly_total": {"ru": "Сумма за месяц: {amount:.0f} ₽", "en": "Monthly total: {amount:.0f} ₽"},
    "auto_disabled_admin": {
        "ru": "🔴 Клиент <b>{name}</b> ({username}) автоматически отключён (неоплата).",
        "en": "🔴 Client <b>{name}</b> ({username}) was auto-disabled (non-payment).",
    },

    "admin_clients": {"ru": "👥 Клиенты", "en": "👥 Clients"},
    "admin_servers": {"ru": "🖥 Серверы", "en": "🖥 Servers"},
    "admin_add_client": {"ru": "➕ Добавить клиента", "en": "➕ Add Client"},
    "admin_stats": {"ru": "📊 Статистика", "en": "📊 Statistics"},
    "admin_pick_section": {"ru": "Выбери раздел:", "en": "Choose a section:"},
    "admin_menu_back": {"ru": "🏠 Возврат в главное меню.", "en": "🏠 Back to main menu."},
    "main_menu_title": {"ru": "Главное меню:", "en": "Main menu:"},
    "client_not_found": {"ru": "Клиент не найден", "en": "Client not found"},
    "dialog_closed": {"ru": "Диалог закрыт", "en": "Dialog closed"},
    "support_closed_by_admin": {
        "ru": "ℹ️ <b>Диалог с поддержкой закрыт администратором.</b>",
        "en": "ℹ️ <b>Support dialog was closed by administrator.</b>",
    },
    "no_active_dialog": {"ru": "Активный диалог не выбран.", "en": "No active dialog selected."},
    "broadcast_mode_closed": {"ru": "✅ Режим рассылки закрыт.", "en": "✅ Broadcast mode closed."},

    "bot_started": {
        "ru": "🤖 <b>VPN Bot запущен!</b>\n\nКоманды:\n/clients — список клиентов\n/add_client — добавить клиента\n/servers — статус всех серверов\n/server <имя> — детально по серверу\n/client <id> — карточка клиента",
        "en": "🤖 <b>VPN Bot started!</b>\n\nCommands:\n/clients — clients list\n/add_client — add client\n/servers — all servers status\n/server <name> — server details\n/client <id> — client card",
    },
}


def normalize_lang(lang: Optional[str]) -> str:
    if not lang:
        return LANG_EN
    lang = lang.lower().strip()
    if lang.startswith("ru"):
        return LANG_RU
    if lang.startswith("en"):
        return LANG_EN
    return LANG_EN


def tr(lang: str, key: str, **kwargs) -> str:
    lang = normalize_lang(lang)
    item = TEXTS.get(key)
    if not item:
        return key
    text = item.get(lang) or item.get(LANG_EN) or key
    if kwargs:
        return text.format(**kwargs)
    return text
