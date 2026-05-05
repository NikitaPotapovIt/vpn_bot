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
        name="POTAPOV_HOST",
        host=os.getenv("POTAPOV_HOST", "REDACTED_HOST"),
        port=22,
        ssh_user="root",
        ssh_key_path=os.getenv("SSH_KEY_PATH", "/root/.ssh/id_rsa"),
        wg_interface="wg0",
        wg_config_path="/etc/wireguard/wg0.conf",
    ),
    ServerConfig(
        name="GHISLAIN_HOST",
        host=os.getenv("GHISLAIN_HOST", "REDACTED_HOST"),
        port=22,
        ssh_user="root",
        ssh_key_path=os.getenv("SSH_KEY_PATH", "/root/.ssh/id_rsa"),
        wg_interface="wg0",
        wg_config_path="/etc/wireguard/wg0.conf",
    ),
    ServerConfig(
        name="ALEV_HOST",
        host=os.getenv("ALEV_HOST", "REDACTED_HOST"),
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
