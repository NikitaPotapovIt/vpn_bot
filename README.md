# VPN Bot — Инструкция по развёртыванию

## Установка

```bash
cd /opt
git clone <repo> vpn_bot
cd vpn_bot

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Настройка

### 1. Переменные окружения (создай `.env` или задай через systemd)

```env
TELEGRAM_TOKEN=1234567890:AABBCCddEEff...
ADMIN_IDS=123456789,987654321          # твои Telegram ID через запятую
POTAPOV_HOST=1.2.3.4
GHISLAIN_HOST=5.6.7.8
ALEV_HOST=9.10.11.12
SSH_KEY_PATH=/root/.ssh/id_rsa         # SSH-ключ для доступа к серверам
```

### 2. SSH-ключ

Убедись, что с сервера, где запущен бот, можно подключиться к остальным:
```bash
ssh-copy-id -i /root/.ssh/id_rsa root@GHISLAIN_HOST
ssh-copy-id -i /root/.ssh/id_rsa root@ALEV_HOST
```

### 3. WireGuard на серверах

Бот предполагает, что WireGuard уже установлен и интерфейс `wg0` поднят.
Если используешь AmneziaWG — замени `wg` на `awg` в `ssh_manager.py`:
- `wg show` → `awg show`
- `wg genkey` → `awg genkey`  
- `wg set` → `awg set`
- `wg_interface` в config.py → `"awg0"`

## Запуск как systemd-сервис

```bash
# Создай файл /etc/systemd/system/vpn-bot.service
```

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

```bash
systemctl daemon-reload
systemctl enable vpn-bot
systemctl start vpn-bot
systemctl status vpn-bot
```

## Как добавить клиента

1. Узнай Telegram ID клиента (попроси написать боту /start или используй @userinfobot)
2. Напиши боту `/add_client`
3. Следуй диалогу: ID → имя → @username → сервер → устройства → сумма
4. Выбери "Создать конфиг" — бот сгенерирует WireGuard-ключи на сервере и пришлёт `.conf` файл

## Команды администратора

| Команда | Описание |
|---------|----------|
| `/add_client` | Добавить нового клиента |
| `/clients` | Список всех клиентов с статусами |
| `/client <id>` | Карточка клиента с управлением |
| `/servers` | Статус всех трёх серверов |
| `/server <имя>` | Peer'ы на конкретном сервере |

## Команды клиента

| Команда | Описание |
|---------|----------|
| `/start` | Информация о подписке |
| `/status` | Текущий статус VPN и оплаты |
| Кнопка "Я оплатил" | Уведомить администратора об оплате |

## Логика платёжного цикла

```
1-е число        → Напоминание всем + сброс статусов
2-е число        → Повтор неоплатившим
3-е число        → "Отключим через 5 дней"
4–7-е число      → Ежедневные напоминания с обратным отсчётом
8-е число        → Автоотключение (disable_peer на WireGuard)
```

Клиент нажимает "Я оплатил" → ты получаешь уведомление с кнопками ✅/❌ → 
если подтверждаешь — цикл сбрасывается, клиент получает уведомление.
Если отклоняешь — статус возвращается в pending.

## AmneziaWG — отличия

В `ssh_manager.py` замени все команды `wg` на `awg`:
```python
# Было:
f"wg show {server.wg_interface}"
# Стало:
f"awg show {server.wg_interface}"
```

## Бэкап VPN-конфигов (ежедневно в 04:00 МСК)

Добавлены скрипты:
- `scripts/backup_vpn_configs.py` — делает бэкап `wg0.conf` и `clientsTable` с каждого сервера в `backup_config/<server>/<timestamp>/`, хранит только последние 30 бэкапов на сервер, затем делает `git pull --rebase`, `git commit` и `git push`.
- `scripts/install_backup_cron.sh` — ставит cron-задачу на ежедневный запуск в `04:00` по `Europe/Moscow`.

Установка cron:

```bash
cd /opt/vpn_bot
./scripts/install_backup_cron.sh
```

Проверка вручную:

```bash
cd /opt/vpn_bot
./scripts/backup_vpn_configs.py
```
