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
