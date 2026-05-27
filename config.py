import os
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import List, Optional
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "CHANGE_ME")


def _parse_admin_ids(raw: str) -> List[int]:
    result: List[int] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        result.append(int(item))
    return result


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


ADMIN_IDS = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
DEVICE_MONTHLY_PRICE = float(os.getenv("DEVICE_MONTHLY_PRICE", "100"))

@dataclass
class ServerConfig:
    name: str
    host: str
    port: int
    ssh_user: str
    ssh_key_path: str
    wg_interface: str = "wg0"
    wg_config_path: str = "/opt/amnezia/awg/wg0.conf"
    vpn_container: str = "amnezia-awg"
    protocol_label: str = "AWG1"
    is_local: bool = False

    @property
    def wg_base_dir(self) -> str:
        return str(PurePosixPath(self.wg_config_path).parent)

def _build_server_from_index(index: int, default_ssh_key: str) -> Optional[ServerConfig]:
    prefix = f"SERVER_{index}_"
    name = (os.getenv(f"{prefix}NAME", "") or "").strip()
    host = (os.getenv(f"{prefix}HOST", "") or "").strip()
    if not name or not host:
        return None
    return ServerConfig(
        name=name,
        host=host,
        port=int(os.getenv(f"{prefix}PORT", "22")),
        ssh_user=os.getenv(f"{prefix}SSH_USER", "root"),
        ssh_key_path=os.getenv(f"{prefix}SSH_KEY_PATH", default_ssh_key),
        wg_interface=os.getenv(f"{prefix}WG_INTERFACE", "wg0"),
        wg_config_path=os.getenv(f"{prefix}WG_CONFIG_PATH", "/opt/amnezia/awg/wg0.conf"),
        vpn_container=os.getenv(f"{prefix}VPN_CONTAINER", "amnezia-awg"),
        protocol_label=(os.getenv(f"{prefix}PROTOCOL_LABEL", "AWG1") or "AWG1").strip().upper(),
        is_local=_as_bool(os.getenv(f"{prefix}IS_LOCAL"), False),
    )


def _build_server_legacy(name: str, host_key: str, default_ssh_key: str, is_local: bool = False) -> Optional[ServerConfig]:
    host = (os.getenv(host_key, "") or "").strip()
    if not host:
        return None
    return ServerConfig(
        name=name,
        host=host,
        port=22,
        ssh_user="root",
        ssh_key_path=default_ssh_key,
        vpn_container="amnezia-awg",
        protocol_label="AWG1",
        is_local=is_local,
    )


def _load_servers() -> List[ServerConfig]:
    default_ssh_key = os.getenv("SSH_KEY_PATH", "/root/.ssh/id_rsa")
    servers: List[ServerConfig] = []
    for index in range(1, 11):
        server = _build_server_from_index(index, default_ssh_key)
        if server:
            servers.append(server)
    if servers:
        return servers

    # Legacy compatibility (no defaults in code to avoid leaking infra values)
    legacy_servers = [
        _build_server_legacy("Ghislain", "GHISLAIN_HOST", default_ssh_key, is_local=True),
        _build_server_legacy("Potapov", "POTAPOV_HOST", default_ssh_key),
        _build_server_legacy("Alev", "ALEV_HOST", default_ssh_key),
        _build_server_legacy("RCP", "RCP_HOST", default_ssh_key),
    ]
    return [srv for srv in legacy_servers if srv is not None]


SERVERS: List[ServerConfig] = _load_servers()

PAYMENT_DAY = 1
REMINDER_SCHEDULE_DAYS = [0, 1, 2]
DISCONNECT_WARNING_DAY = 2
DISCONNECT_AFTER_DAYS = 5
