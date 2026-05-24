# VPN Bot (EN)

A Telegram bot for managing VPN clients (WireGuard/AmneziaWG): client onboarding, config generation, payment flow, notifications, and admin tooling.

## Features

- Add client by `Telegram ID` or `@username`
- Auto-bind `telegram_id` after `/start` for username-based registration
- Generate client config and `vpn://` link
- Payment status handling with admin confirmation flow
- `🎁 Trial period` action (until end of month) in admin payment flow
- Client ↔ admin support dialogs
- Server health checks and speed tests
- Scheduled reminders and auto-disconnect logic
- Daily VPN config backups with git sync

## Project Structure

- `bot.py` — application entry point
- `config.py` — environment-based configuration loader
- `database.py` — SQLite data layer and models
- `scheduler.py` — periodic billing/notification jobs
- `ssh_manager.py` — SSH/local execution and WireGuard operations
- `handlers/admin.py` — admin scenarios
- `handlers/client.py` — client scenarios
- `support_dialog.py` — support dialog state
- `scripts/backup_vpn_configs.py` — backup + push to backup repository
- `scripts/install_backup_cron.sh` — cron installer
- `.env.example` — environment template

## Requirements

- Python 3.10+
- Linux server (for systemd/cron)
- Docker on VPN hosts (container `amnezia-awg`)
- SSH access to VPN hosts

## Quick Start

1. Clone and install dependencies:

```bash
git clone <your_repo_url> vpn_bot
cd vpn_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

2. Configure environment:

```bash
cp .env.example .env
```

3. Fill `.env`:

- `TELEGRAM_TOKEN`, `ADMIN_IDS`
- servers in `SERVER_<N>_*` format
- backup repository settings (`BACKUP_GIT_*`) if backup automation is used

4. Run:

```bash
python bot.py
```

## Server Variables Format

Preferred format:

- `SERVER_1_NAME`, `SERVER_1_HOST`, `SERVER_1_PORT`, `SERVER_1_SSH_USER`, `SERVER_1_SSH_KEY_PATH`, `SERVER_1_WG_INTERFACE`, `SERVER_1_WG_CONFIG_PATH`, `SERVER_1_IS_LOCAL`
- then `SERVER_2_*`, `SERVER_3_*`, etc.

Legacy variables (`FRANCE_HOST`, `GERMANY_HOST`, `ICELAND_HOST`, `JAPAN_HOST`) are still supported for backward compatibility.

## systemd Setup

Create `/etc/systemd/system/vpn-bot.service`:

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

Commands:

```bash
sudo systemctl daemon-reload
sudo systemctl enable vpn-bot
sudo systemctl start vpn-bot
sudo systemctl status vpn-bot
```

## Backups

Install cron job:

```bash
./scripts/install_backup_cron.sh
```

Run manually:

```bash
./scripts/backup_vpn_configs.py
```

By default, backup sync targets `BACKUP_GIT_REPO_URL`.

## If you want to support me:

- BTC bc1qqzenfgfct0uyqszpwlws4gse6690u28xrm6r9w
- ETH 0xb79D3dfeDaA4A66b395a9a226764a980C18d7f71
- USDT 0xb79D3dfeDaA4A66b395a9a226764a980C18d7f71
