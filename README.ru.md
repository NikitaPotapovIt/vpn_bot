# VPN Bot (RU)

Telegram-бот для управления VPN-клиентами (WireGuard/AmneziaWG): регистрация клиентов, генерация конфигов, контроль оплат, уведомления и админ-инструменты.

## Возможности

- Добавление клиента по `Telegram ID` или `@username`
- Автопривязка `telegram_id` после `/start`, если клиент добавлен по `@username`
- Выдача конфигурации и `vpn://` ссылки
- Управление статусом оплаты и сценариями подтверждения
- Кнопка `🎁 Тестовый период` (до конца месяца) в админском сценарии оплаты
- Поддержка диалогов клиент ↔ админ
- Проверка статуса серверов и speed test
- Планировщик напоминаний и авто-отключений
- Ежедневные бэкапы VPN-конфигов с git sync

## Структура проекта

- `bot.py` — точка входа
- `config.py` — загрузка конфигурации из `.env`
- `database.py` — SQLite-слой и модели
- `scheduler.py` — периодические задачи оплаты/уведомлений
- `ssh_manager.py` — SSH/локальные команды и WireGuard-операции
- `handlers/admin.py` — сценарии администратора
- `handlers/client.py` — сценарии клиента
- `support_dialog.py` — состояние диалогов поддержки
- `scripts/backup_vpn_configs.py` — бэкап и push в backup-репозиторий
- `scripts/install_backup_cron.sh` — установка cron-задачи
- `.env.example` — шаблон переменных окружения

## Требования

- Python 3.10+
- Linux-сервер (для systemd/cron)
- Docker на VPN-серверах (контейнер `amnezia-awg`)
- Доступ по SSH к VPN-серверам

## Быстрый старт

1. Клонирование и зависимости:

```bash
git clone <your_repo_url> vpn_bot
cd vpn_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

2. Настройка окружения:

```bash
cp .env.example .env
```

3. Заполнить `.env`:

- `TELEGRAM_TOKEN`, `ADMIN_IDS`
- серверы в формате `SERVER_<N>_*`
- параметры backup-репозитория (`BACKUP_GIT_*`) при использовании бэкапов

4. Запуск:

```bash
python bot.py
```

## Формат серверов в `.env`

Предпочтительный формат:

- `SERVER_1_NAME`, `SERVER_1_HOST`, `SERVER_1_PORT`, `SERVER_1_SSH_USER`, `SERVER_1_SSH_KEY_PATH`, `SERVER_1_WG_INTERFACE`, `SERVER_1_WG_CONFIG_PATH`, `SERVER_1_IS_LOCAL`
- `SERVER_2_*`, `SERVER_3_*` и т.д.

Поддерживается и legacy-формат (`FRANCE_HOST`, `GERMANY_HOST`, `ICELAND_HOST`, `JAPAN_HOST`) для обратной совместимости.

## Запуск через systemd

Файл `/etc/systemd/system/vpn-bot.service`:

```ini
[Unit]
Description=VPN Telegram Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/vpn_bot
EnvironmentFile=/opt/vpn_bot/.env
ExecStart=/opt/vpn_bot/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Команды:

```bash
sudo systemctl daemon-reload
sudo systemctl enable vpn-bot
sudo systemctl start vpn-bot
sudo systemctl status vpn-bot
```

## Бэкапы

Установка cron:

```bash
./scripts/install_backup_cron.sh
```

Ручной запуск:

```bash
./scripts/backup_vpn_configs.py
```

По умолчанию бэкап пушится в репозиторий из `BACKUP_GIT_REPO_URL`.

## Безопасность для open source

- Не коммитьте `.env`, приватные SSH-ключи и дампы
- Проверьте, что все реальные IP/домены/токены находятся только в `.env`
- Рекомендуется ограничить права SSH-ключа и доступ по IP
