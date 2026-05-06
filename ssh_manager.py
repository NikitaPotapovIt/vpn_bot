import asyncio
import json
import re
import time
from typing import Optional, Dict, List, Tuple
import paramiko
from config import ServerConfig, SERVERS

def _get_server(server_name: str) -> Optional[ServerConfig]:
    return next((s for s in SERVERS if s.name == server_name), None)

def _ssh_exec(server: ServerConfig, command: str) -> Tuple[str, str, int]:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            server.host, port=server.port, username=server.ssh_user,
            key_filename=server.ssh_key_path, timeout=15,
        )
        stdin, stdout, stderr = client.exec_command(command)
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        code = stdout.channel.recv_exit_status()
        return out, err, code
    finally:
        client.close()

async def ssh_exec(server_name: str, command: str) -> Tuple[str, str, int]:
    server = _get_server(server_name)
    if not server:
        return "", f"Server '{server_name}' not found", 1
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _ssh_exec, server, command)

def _docker(cmd: str) -> str:
    return f"docker exec amnezia-awg {cmd}"

def _local_exec(command: str) -> Tuple[str, str, int]:
    import subprocess
    result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
    return result.stdout.strip(), result.stderr.strip(), result.returncode

async def local_exec(command: str) -> Tuple[str, str, int]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _local_exec, command)

async def _exec(server_name: str, command: str) -> Tuple[str, str, int]:
    server = _get_server(server_name)
    if server and server.is_local:
        return await local_exec(command)
    return await ssh_exec(server_name, command)

# ─── Мониторинг ───────────────────────────────────────────────────────────────

async def get_server_status(server_name: str) -> Dict:
    server = _get_server(server_name)
    if not server:
        return {"error": "not found", "name": server_name}
    try:
        uptime_out, _, _ = await _exec(server_name, "uptime -p")
        load_out, _, _ = await _exec(server_name, "cat /proc/loadavg")
        wg_out, _, code = await _exec(server_name, _docker("wg show"))
        peers_count = wg_out.count("peer:")
        mem_out, _, _ = await _exec(server_name, "free -m | grep Mem")
        mem_parts = mem_out.split()
        return {
            "name": server_name, "host": server.host,
            "uptime": uptime_out or "unknown",
            "load": load_out.split()[:3] if load_out else ["?","?","?"],
            "wg_running": code == 0, "peers_count": peers_count,
            "mem_total": int(mem_parts[1]) if len(mem_parts) > 1 else 0,
            "mem_used": int(mem_parts[2]) if len(mem_parts) > 2 else 0,
            "online": True,
        }
    except Exception as e:
        return {"name": server_name, "host": server.host, "online": False, "error": str(e)}

async def get_clients_table(server_name: str) -> List[Dict]:
    out, _, code = await _exec(server_name, _docker("cat /opt/amnezia/awg/clientsTable"))
    if code != 0 or not out:
        return []
    try:
        return json.loads(out)
    except Exception:
        return []

async def get_wg_dump(server_name: str) -> Dict[str, Dict]:
    out, _, _ = await _exec(server_name, _docker("wg show wg0 dump"))
    result = {}
    for line in out.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 7:
            pubkey = parts[0]
            last_hs = int(parts[4]) if parts[4] != "0" else 0
            result[pubkey] = {
                "endpoint": parts[2],
                "last_handshake": last_hs,
                "rx_bytes": int(parts[5]),
                "tx_bytes": int(parts[6]),
                "connected": last_hs > 0 and (time.time() - last_hs) < 180,
            }
    return result

async def get_peer_status(server_name: str, pubkey: str) -> Dict:
    dump = await get_wg_dump(server_name)
    p = dump.get(pubkey, {})
    return {
        "pubkey": pubkey,
        "connected": p.get("connected", False),
        "last_handshake": p.get("last_handshake", 0),
        "rx_mb": round(p.get("rx_bytes", 0) / 1_048_576, 2),
        "tx_mb": round(p.get("tx_bytes", 0) / 1_048_576, 2),
    }

async def get_all_peers_merged(server_name: str) -> List[Dict]:
    clients = await get_clients_table(server_name)
    dump = await get_wg_dump(server_name)
    result = []
    for c in clients:
        pubkey = c["clientId"]
        data = c["userData"]
        wg = dump.get(pubkey, {})
        result.append({
            "pubkey": pubkey,
            "name": data.get("clientName", "Unknown"),
            "ip": data.get("allowedIps", "—"),
            "created": data.get("creationDate", "—"),
            "connected": wg.get("connected", False),
            "last_handshake": wg.get("last_handshake", 0),
            "rx_mb": round(wg.get("rx_bytes", 0) / 1_048_576, 2),
            "tx_mb": round(wg.get("tx_bytes", 0) / 1_048_576, 2),
            "endpoint": wg.get("endpoint", "—"),
        })
    return result

# ─── Пинг и скорость ──────────────────────────────────────────────────────────

async def ping_server(server_name: str) -> Dict:
    server = _get_server(server_name)
    if not server:
        return {"success": False, "ms": None, "name": server_name}
    out, _, code = await local_exec(f"ping -c 3 -W 3 {server.host}")
    match = re.search(r"min/avg/max.*?=\s+[\d.]+/([\d.]+)/", out)
    avg_ms = float(match.group(1)) if match else None
    return {"success": code == 0, "ms": avg_ms, "host": server.host, "name": server_name}

async def speed_test(server_name: str) -> Dict:
    # Попытка 1: speedtest-cli
    out, _, code = await _exec(server_name, "which speedtest-cli 2>/dev/null || which speedtest 2>/dev/null")
    if code == 0 and out.strip():
        binary = out.strip().splitlines()[0]
        result_out, _, code2 = await _exec(server_name, f"{binary} --simple 2>&1")
        if code2 == 0:
            data = {}
            for line in result_out.splitlines():
                if "Ping:" in line:
                    m = re.search(r"([\d.]+)\s*ms", line)
                    if m: data["ping_ms"] = float(m.group(1))
                elif "Download:" in line:
                    m = re.search(r"([\d.]+)\s*Mbit", line)
                    if m: data["download_mbps"] = float(m.group(1))
                elif "Upload:" in line:
                    m = re.search(r"([\d.]+)\s*Mbit", line)
                    if m: data["upload_mbps"] = float(m.group(1))
            if data:
                return {"success": True, "method": "speedtest-cli", **data}

    # Попытка 2: wget
    cmd = "wget -O /dev/null --report-speed=bits https://speed.hetzner.de/10MB.bin 2>&1 | tail -5"
    out, _, code = await _exec(server_name, cmd)
    match = re.search(r"([\d.]+)\s*([KMG])b(?:it)?/s", out)
    if match:
        val = float(match.group(1))
        unit = match.group(2)
        mbps = val / 1000 if unit == "K" else (val * 1000 if unit == "G" else val)
        return {"success": True, "method": "wget", "download_mbps": round(mbps, 1)}

    return {"success": False, "error": "speedtest-cli не установлен. Установи: apt install speedtest-cli"}

# ─── Управление peer'ами ──────────────────────────────────────────────────────

async def add_peer(server_name: str, client_name: str) -> Optional[Dict]:
    priv_out, _, code = await _exec(server_name, _docker("wg genkey"))
    if code != 0 or not priv_out:
        return None
    privkey = priv_out.strip()

    pub_out, _, code = await _exec(server_name, f"echo '{privkey}' | docker exec -i amnezia-awg wg pubkey")
    if code != 0 or not pub_out:
        return None
    pubkey = pub_out.strip()

    psk_out, _, _ = await _exec(server_name, _docker("wg genpsk"))
    psk = psk_out.strip()

    conf_out, _, _ = await _exec(server_name, _docker("cat /opt/amnezia/awg/wg0.conf"))
    used_ips = set(int(x) for x in re.findall(r"AllowedIPs\s*=\s*10\.8\.1\.(\d+)", conf_out))
    next_num = next(i for i in range(2, 255) if i not in used_ips)
    client_ip = f"10.8.1.{next_num}/32"

    server_pub_out, _, _ = await _exec(server_name, _docker("cat /opt/amnezia/awg/wireguard_server_public_key.key"))
    server_pubkey = server_pub_out.strip()

    wg_show_out, _, _ = await _exec(server_name, _docker("wg show wg0"))
    jc = re.search(r"jc:\s*(\d+)", wg_show_out)
    jmin = re.search(r"jmin:\s*(\d+)", wg_show_out)
    jmax = re.search(r"jmax:\s*(\d+)", wg_show_out)
    s1 = re.search(r"s1:\s*(\d+)", wg_show_out)
    s2 = re.search(r"s2:\s*(\d+)", wg_show_out)
    port_match = re.search(r"listening port:\s*(\d+)", wg_show_out)
    port = port_match.group(1) if port_match else "46742"

    # Добавляем peer
    add_cmd = (
        f"docker exec amnezia-awg sh -c "
        f"'echo \"[Peer]\\n# {client_name}\\nPublicKey = {pubkey}\\n"
        f"PresharedKey = {psk}\\nAllowedIPs = {client_ip}\\n\" >> /opt/amnezia/awg/wg0.conf && "
        f"wg set wg0 peer {pubkey} preshared-key <(echo {psk}) allowed-ips {client_ip}'"
    )
    await _exec(server_name, add_cmd)

    # Обновляем clientsTable
    import datetime
    table = await get_clients_table(server_name)
    table.append({
        "clientId": pubkey,
        "userData": {
            "allowedIps": client_ip,
            "clientName": client_name,
            "creationDate": datetime.datetime.now().strftime("%a %b %-d %H:%M:%S %Y"),
        }
    })
    table_json = json.dumps(table, indent=4, ensure_ascii=False).replace("'", "'\\''")
    await _exec(server_name, f"docker exec amnezia-awg sh -c 'cat > /opt/amnezia/awg/clientsTable' << 'JSONEOF'\n{table_json}\nJSONEOF")

    server = _get_server(server_name)
    amnezia_params = ""
    if jc:
        amnezia_params = (
            f"Jc = {jc.group(1)}\n"
            f"Jmin = {jmin.group(1) if jmin else 50}\n"
            f"Jmax = {jmax.group(1) if jmax else 1000}\n"
            f"S1 = {s1.group(1) if s1 else 0}\n"
            f"S2 = {s2.group(1) if s2 else 0}\n"
            f"H1 = 1\nH2 = 2\nH3 = 3\nH4 = 4\n"
        )

    client_config = (
        f"[Interface]\nPrivateKey = {privkey}\nAddress = {client_ip}\nDNS = 1.1.1.1\n"
        f"{amnezia_params}\n"
        f"[Peer]\nPublicKey = {server_pubkey}\nPresharedKey = {psk}\n"
        f"Endpoint = {server.host}:{port}\nAllowedIPs = 0.0.0.0/0\nPersistentKeepalive = 25\n"
    )
    return {"pubkey": pubkey, "privkey": privkey, "client_ip": client_ip,
            "config_text": client_config, "server_name": server_name}

async def remove_peer(server_name: str, pubkey: str) -> bool:
    await _exec(server_name, f"docker exec amnezia-awg wg set wg0 peer {pubkey} remove")
    escaped = re.escape(pubkey).replace("/", "\\/")
    await _exec(server_name,
        f"docker exec amnezia-awg sh -c 'sed -i \"/{escaped}/,/^$/d\" /opt/amnezia/awg/wg0.conf'"
    )
    table = await get_clients_table(server_name)
    table = [c for c in table if c["clientId"] != pubkey]
    table_json = json.dumps(table, indent=4, ensure_ascii=False)
    await _exec(server_name,
        f"docker exec amnezia-awg sh -c 'cat > /opt/amnezia/awg/clientsTable' << 'JSONEOF'\n{table_json}\nJSONEOF"
    )
    return True

async def disable_peer(server_name: str, pubkey: str) -> bool:
    _, _, code = await _exec(server_name,
        f"docker exec amnezia-awg wg set wg0 peer {pubkey} allowed-ips 192.0.2.0/32")
    return code == 0

async def enable_peer(server_name: str, pubkey: str, client_ip: str) -> bool:
    _, _, code = await _exec(server_name,
        f"docker exec amnezia-awg wg set wg0 peer {pubkey} allowed-ips {client_ip}")
    return code == 0
