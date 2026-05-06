import os
from dataclasses import dataclass
from typing import List
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "17070698").split(",")))

@dataclass
class ServerConfig:
    name: str
    host: str
    port: int
    ssh_user: str
    ssh_key_path: str
    wg_interface: str = "wg0"
    wg_config_path: str = "/opt/amnezia/awg/wg0.conf"
    is_local: bool = False

SERVERS: List[ServerConfig] = [
    ServerConfig(
        name="Ghislain",
        host=os.getenv("GHISLAIN_HOST", "REDACTED_HOST"),
        port=22, ssh_user="root",
        ssh_key_path=os.getenv("SSH_KEY_PATH", "/root/.ssh/id_rsa"),
        is_local=True,
    ),
    ServerConfig(
        name="Potapov",
        host=os.getenv("POTAPOV_HOST", "REDACTED_HOST"),
        port=22, ssh_user="root",
        ssh_key_path=os.getenv("SSH_KEY_PATH", "/root/.ssh/id_rsa"),
    ),
    ServerConfig(
        name="Alev",
        host=os.getenv("ALEV_HOST", "REDACTED_HOST"),
        port=22, ssh_user="root",
        ssh_key_path=os.getenv("SSH_KEY_PATH", "/root/.ssh/id_rsa"),
    ),
]

PAYMENT_DAY = 1
REMINDER_SCHEDULE_DAYS = [0, 1, 2]
DISCONNECT_WARNING_DAY = 2
DISCONNECT_AFTER_DAYS = 5
