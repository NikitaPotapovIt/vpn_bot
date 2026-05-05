import asyncio
import re
from typing import Optional, Dict, List, Tuple
import paramiko
from config import ServerConfig, SERVERS

def _get_server(server_name: str) -> Optional[ServerConfig]:
    return next((s for s in SERVERS if s.name == server_name), None)

def _ssh_exec(server: ServerConfig, command: str) -> Tuple[str, str, int]:
    """Синхронное выполнение SSH-команды. Возвращает (stdout, stderr, exit_code)."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            server.host,
            port=server.port,
            username=server.ssh_user,
            key_filename=server.ssh_key_path,
            timeout=10,
        )
        stdin, stdout, stderr = client.exec_command(command)
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        code = stdout.channel.recv_exit_status()
        return out, err, code
    finally:
        client.close()

async def ssh_exec(server_name: str, command: str) -> Tuple[str, str, int]:
    """Асинхронная обёртка над SSH"""
    server = _get_server(server_name)
    if not server:
        return "", f"Server '{server_name}' not found", 1
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _ssh_exec, server, command)

# ─── Мониторинг ───────────────────────────────────────────────────────────────

async def get_server_status(server_name: str) -> Dict:
    """Возвращает общую информацию о сервере: uptime, load, wg статус"""
    server = _get_server(server_name)
    if not server:
        return {"error": "not found"}

    uptime_out, _, _ = await ssh_exec(server_name, "uptime -p")
    load_out, _, _ = await ssh_exec(server_name, "cat /proc/loadavg")
    wg_out, _, code = await ssh_exec(server_name, f"wg show {server.wg_interface}")

    peers_count = wg_out.count("peer:")
    return {
        "name": server_name,
        "host": server.host,
        "uptime": uptime_out or "unknown",
        "load": load_out.split()[:3] if load_out else ["?", "?", "?"],
        "wg_running": code == 0,
        "peers_count": peers_count,
        "wg_raw": wg_out,
    }

async def get_peer_status(server_name: str, pubkey: str) -> Dict:
    """Статус конкретного peer'а: последнее рукопожатие, трафик"""
    out, _, _ = await ssh_exec(server_name, f"wg show {_get_server(server_name).wg_interface} dump")
    for line in out.splitlines():
        parts = line.split("\t")
        # dump format: pubkey, preshared, endpoint, allowed_ips, last_handshake, rx, tx, keepalive
        if len(parts) >= 7 and parts[0] == pubkey:
            last_hs = int(parts[4]) if parts[4] != "0" else 0
            rx_bytes = int(parts[5])
            tx_bytes = int(parts[6])
            connected = (last_hs > 0) and ((asyncio.get_event_loop().time() - last_hs) < 180)
            return {
                "pubkey": pubkey,
                "connected": connected,
                "last_handshake": last_hs,
                "rx_mb": round(rx_bytes / 1_048_576, 2),
                "tx_mb": round(tx_bytes / 1_048_576, 2),
            }
    return {"pubkey": pubkey, "connected": False, "last_handshake": 0, "rx_mb": 0, "tx_mb": 0}

async def get_all_peers_status(server_name: str) -> List[Dict]:
    """Все peer'ы на сервере"""
    server = _get_server(server_name)
    if not server:
        return []
    out, _, _ = await ssh_exec(server_name, f"wg show {server.wg_interface} dump")
    results = []
    for line in out.splitlines()[1:]:  # первая строка — сам интерфейс
        parts = line.split("\t")
        if len(parts) >= 7:
            last_hs = int(parts[4]) if parts[4] != "0" else 0
            import time
            connected = last_hs > 0 and (time.time() - last_hs) < 180
            results.append({
                "pubkey": parts[0][:16] + "...",
                "endpoint": parts[2],
                "connected": connected,
                "rx_mb": round(int(parts[5]) / 1_048_576, 2),
                "tx_mb": round(int(parts[6]) / 1_048_576, 2),
                "last_handshake": last_hs,
            })
    return results

# ─── Управление peer'ами ──────────────────────────────────────────────────────

async def add_peer(server_name: str, client_name: str) -> Optional[Dict]:
    """
    Генерирует новую WireGuard пару ключей на сервере,
    добавляет peer в конфиг и возвращает данные для клиента.
    Возвращает dict с pubkey, privkey, config_text или None при ошибке.
    """
    server = _get_server(server_name)
    if not server:
        return None

    # Генерация ключей прямо на сервере
    priv_out, _, code = await ssh_exec(server_name, "wg genkey")
    if code != 0 or not priv_out:
        return None
    privkey = priv_out.strip()

    pub_out, _, code = await ssh_exec(server_name, f"echo '{privkey}' | wg pubkey")
    if code != 0 or not pub_out:
        return None
    pubkey = pub_out.strip()

    # Получаем следующий свободный IP в подсети (простая логика)
    conf_out, _, _ = await ssh_exec(server_name, f"cat {server.wg_config_path}")
    used_ips = re.findall(r"AllowedIPs\s*=\s*10\.8\.0\.(\d+)", conf_out)
    used_nums = set(int(x) for x in used_ips)
    next_num = next(i for i in range(2, 255) if i not in used_nums)
    client_ip = f"10.8.0.{next_num}/32"

    # Получаем публичный ключ сервера и endpoint
    server_pub_out, _, _ = await ssh_exec(server_name, f"wg show {server.wg_interface} public-key")
    server_pubkey = server_pub_out.strip()

    # Добавляем peer в конфиг сервера
    peer_block = f"""
[Peer]
# {client_name}
PublicKey = {pubkey}
AllowedIPs = {client_ip}
"""
    # Дописываем в конфиг и перезагружаем
    cmd = f"echo '{peer_block}' >> {server.wg_config_path} && wg addconf {server.wg_interface} <(echo '{peer_block}')"
    _, err, code = await ssh_exec(server_name, f"bash -c \"{cmd.replace(chr(34), chr(39))}\"")

    if code != 0:
        # fallback: wg syncconf
        await ssh_exec(server_name, f"wg syncconf {server.wg_interface} <(wg-quick strip {server.wg_interface})")

    # Формируем конфиг для клиента
    dns_out, _, _ = await ssh_exec(server_name, "cat /etc/resolv.conf | grep nameserver | head -1 | awk '{print $2}'")
    dns = dns_out.strip() or "1.1.1.1"

    client_config = f"""[Interface]
PrivateKey = {privkey}
Address = {client_ip}
DNS = {dns}

[Peer]
PublicKey = {server_pubkey}
Endpoint = {server.host}:51820
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
"""
    return {
        "pubkey": pubkey,
        "privkey": privkey,
        "client_ip": client_ip,
        "config_text": client_config,
        "server_name": server_name,
    }

async def remove_peer(server_name: str, pubkey: str) -> bool:
    """Удаляет peer с сервера"""
    server = _get_server(server_name)
    if not server:
        return False
    _, _, code = await ssh_exec(
        server_name,
        f"wg set {server.wg_interface} peer {pubkey} remove && "
        f"sed -i '/PublicKey = {pubkey}/,/^$/d' {server.wg_config_path}"
    )
    return code == 0

async def disable_peer(server_name: str, pubkey: str) -> bool:
    """Отключает peer (убирает AllowedIPs — трафик не пройдёт)"""
    server = _get_server(server_name)
    if not server:
        return False
    # Ставим пустой AllowedIPs = 0.0.0.0/32 (недостижимый)
    _, _, code = await ssh_exec(
        server_name,
        f"wg set {server.wg_interface} peer {pubkey} allowed-ips 192.0.2.0/32"
    )
    return code == 0

async def enable_peer(server_name: str, pubkey: str, client_ip: str) -> bool:
    """Восстанавливает доступ peer'а"""
    server = _get_server(server_name)
    if not server:
        return False
    _, _, code = await ssh_exec(
        server_name,
        f"wg set {server.wg_interface} peer {pubkey} allowed-ips {client_ip}"
    )
    return code == 0

async def ping_server(server_name: str) -> bool:
    """Проверяет доступность сервера"""
    _, _, code = await ssh_exec(server_name, "echo ok")
    return code == 0
