import os
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "17070698").split(",")))  # твои Telegram ID

# --- Серверы ---
@dataclass
class ServerConfig:
    name: str           # "Server 1 (DE)"
    host: str
    port: int
    ssh_user: str
    ssh_key_path: str   # путь к приватному SSH-ключу
    wg_interface: str   # например "wg0" или "awg0"
    wg_config_path: str # /etc/wireguard/wg0.conf

SERVERS: List[ServerConfig] = [
    ServerConfig(
        name="Server 1 (DE)",
        host=os.getenv("POTAPOV_HOST", "1.2.3.4"),
        port=22,
        ssh_user="root",
        ssh_key_path=os.getenv("SSH_KEY_PATH", "/root/.ssh/id_rsa"),
        wg_interface="wg0",
        wg_config_path="/etc/wireguard/wg0.conf",
    ),
    ServerConfig(
        name="Server 2 (NL)",
        host=os.getenv("GHISLAIN_HOST", "5.6.7.8"),
        port=22,
        ssh_user="root",
        ssh_key_path=os.getenv("SSH_KEY_PATH", "/root/.ssh/id_rsa"),
        wg_interface="wg0",
        wg_config_path="/etc/wireguard/wg0.conf",
    ),
    ServerConfig(
        name="Server 3 (FI)",
        host=os.getenv("ALEV_HOST", "9.10.11.12"),
        port=22,
        ssh_user="root",
        ssh_key_path=os.getenv("SSH_KEY_PATH", "/root/.ssh/id_rsa"),
        wg_interface="wg0",
        wg_config_path="/etc/wireguard/wg0.conf",
    ),
]

# --- Платёжная логика ---
PAYMENT_DAY = 1  # день месяца для напоминания
REMINDER_SCHEDULE_DAYS = [0, 1, 2]  # день 0 = 1-е число, день 1 = повтор, день 2 = предупреждение об отключении
DISCONNECT_WARNING_DAY = 2          # на какой день шлём "отключим через 5 дней"
DISCONNECT_AFTER_DAYS = 5           # через сколько дней после предупреждения отключать
