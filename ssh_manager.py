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


def _sh_single_quote(command: str) -> str:
    return "'" + command.replace("'", "'\"'\"'") + "'"

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
                "allowed_ips": parts[3] if len(parts) > 3 else "",
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
    result_map: Dict[str, Dict] = {}

    for c in clients:
        pubkey = c.get("clientId")
        if not pubkey:
            continue
        data = c.get("userData", {})
        wg = dump.get(pubkey, {})
        rx_bytes = int(wg.get("rx_bytes", 0))
        tx_bytes = int(wg.get("tx_bytes", 0))
        result_map[pubkey] = {
            "pubkey": pubkey,
            "name": data.get("clientName", "Unknown"),
            "ip": data.get("allowedIps") or wg.get("allowed_ips") or "—",
            "created": data.get("creationDate", "—"),
            "connected": wg.get("connected", False),
            "last_handshake": int(wg.get("last_handshake", 0)),
            "rx_bytes": rx_bytes,
            "tx_bytes": tx_bytes,
            "rx_mb": round(rx_bytes / 1_048_576, 2),
            "tx_mb": round(tx_bytes / 1_048_576, 2),
            "endpoint": wg.get("endpoint", "—"),
        }

    # Некоторые ключи могут существовать в dump, но отсутствовать в clientsTable
    for pubkey, wg in dump.items():
        if pubkey in result_map:
            continue
        rx_bytes = int(wg.get("rx_bytes", 0))
        tx_bytes = int(wg.get("tx_bytes", 0))
        result_map[pubkey] = {
            "pubkey": pubkey,
            "name": f"Imported-{pubkey[:6]}",
            "ip": wg.get("allowed_ips") or "—",
            "created": "—",
            "connected": wg.get("connected", False),
            "last_handshake": int(wg.get("last_handshake", 0)),
            "rx_bytes": rx_bytes,
            "tx_bytes": tx_bytes,
            "rx_mb": round(rx_bytes / 1_048_576, 2),
            "tx_mb": round(tx_bytes / 1_048_576, 2),
            "endpoint": wg.get("endpoint", "—"),
        }

    # Стабильная сортировка по имени, затем по ключу
    return sorted(result_map.values(), key=lambda p: ((p.get("name") or "").lower(), p["pubkey"]))

# ─── Пинг и скорость ──────────────────────────────────────────────────────────

async def ping_server(server_name: str) -> Dict:
    server = _get_server(server_name)
    if not server:
        return {"success": False, "ms": None, "name": server_name}
    out, _, code = await local_exec(f"ping -c 3 -W 3 {server.host}")
    match = re.search(r"min/avg/max.*?=\s+[\d.]+/([\d.]+)/", out)
    avg_ms = float(match.group(1)) if match else None
    return {"success": code == 0, "ms": avg_ms, "host": server.host, "name": server_name}

async def _exec_in_context(server_name: str, cmd: str, context: str) -> Tuple[str, str, int]:
    if context == "vpn":
        wrapped = _docker(f"sh -lc {_sh_single_quote(cmd)}")
        return await _exec(server_name, wrapped)
    return await _exec(server_name, cmd)


def _parse_speedtest_simple(output: str) -> Dict:
    data = {}
    for line in output.splitlines():
        if "Ping:" in line:
            m = re.search(r"([\d.]+)\s*ms", line)
            if m:
                data["ping_ms"] = float(m.group(1))
        elif "Download:" in line:
            m = re.search(r"([\d.]+)\s*Mbit", line)
            if m:
                data["download_mbps"] = float(m.group(1))
        elif "Upload:" in line:
            m = re.search(r"([\d.]+)\s*Mbit", line)
            if m:
                data["upload_mbps"] = float(m.group(1))
    return data


def _is_nonzero_speed_result(data: Dict) -> bool:
    try:
        dl = float(data.get("download_mbps") or 0)
    except Exception:
        dl = 0.0
    try:
        ul = float(data.get("upload_mbps") or 0)
    except Exception:
        ul = 0.0
    return dl > 0.1 or ul > 0.1


def _parse_wget_download(output: str) -> Optional[float]:
    match = re.search(r"([\d.]+)\s*([KMG])b(?:it)?/s", output)
    if not match:
        return None
    val = float(match.group(1))
    unit = match.group(2)
    mbps = val / 1000 if unit == "K" else (val * 1000 if unit == "G" else val)
    mbps = round(mbps, 1)
    return mbps if mbps > 0.1 else None


def _parse_curl_metrics(output: str) -> Optional[Dict]:
    speed_match = re.search(r"speed_bps=([\d.]+)", output)
    size_match = re.search(r"size_bytes=([\d.]+)", output)
    code_match = re.search(r"http_code=(\d+)", output)
    err_match = re.search(r"err=(.*)", output)
    if not speed_match:
        return None

    bytes_per_sec = float(speed_match.group(1))
    size_bytes = float(size_match.group(1)) if size_match else 0.0
    http_code = code_match.group(1) if code_match else "000"
    err = err_match.group(1).strip() if err_match else ""
    mbps = round((bytes_per_sec * 8) / 1_000_000, 1)
    return {
        "download_mbps": mbps,
        "size_bytes": size_bytes,
        "http_code": http_code,
        "err": err,
    }


async def _speed_test_ctx(server_name: str, context: str) -> Dict:
    location = "vpn-container" if context == "vpn" else "host"
    test_urls = [
        "http://speed.hetzner.de/10MB.bin",
        "http://speedtest.tele2.net/10MB.zip",
        "http://ipv4.download.thinkbroadband.com/10MB.zip",
    ]
    binary_cmd = "command -v speedtest-cli 2>/dev/null || command -v speedtest 2>/dev/null"
    out, _, code = await _exec_in_context(server_name, binary_cmd, context)
    if code == 0 and out.strip():
        binary = out.strip().splitlines()[0]
        result_out, _, code2 = await _exec_in_context(server_name, f"{binary} --simple 2>&1", context)
        if code2 == 0:
            data = _parse_speedtest_simple(result_out)
            # Некоторые версии speedtest-cli в контейнере возвращают 0.00/0.00.
            # В таком случае считаем результат невалидным и пробуем альтернативы.
            if data and _is_nonzero_speed_result(data):
                return {"success": True, "context": location, "method": "speedtest-cli", **data}

    # Попытка 2: wget (download only) по нескольким URL
    for url in test_urls:
        wget_cmd = f"wget -O /dev/null --report-speed=bits --timeout=15 {url} 2>&1 | tail -5"
        out, _, _ = await _exec_in_context(server_name, wget_cmd, context)
        mbps = _parse_wget_download(out)
        if mbps is not None:
            return {"success": True, "context": location, "method": f"wget ({url})", "download_mbps": mbps}

    # Попытка 3: curl (download only) по нескольким URL с валидацией size/http_code
    last_diag = ""
    for url in test_urls:
        curl_cmd = (
            "curl -L -o /dev/null -sS --connect-timeout 8 -m 45 "
            f"-w 'speed_bps=%{{speed_download}} size_bytes=%{{size_download}} http_code=%{{http_code}} err=%{{errormsg}}\\n' {url}"
        )
        out, err, _ = await _exec_in_context(server_name, curl_cmd, context)
        metrics = _parse_curl_metrics(out)
        if metrics:
            if (
                metrics["download_mbps"] > 0.1
                and metrics["size_bytes"] >= 100_000
                and metrics["http_code"] in {"200", "206"}
            ):
                return {
                    "success": True,
                    "context": location,
                    "method": f"curl ({url})",
                    "download_mbps": metrics["download_mbps"],
                }
            last_diag = (
                f"url={url} speed={metrics['download_mbps']} "
                f"size={int(metrics['size_bytes'])} code={metrics['http_code']} err={metrics['err']}"
            )
        elif err:
            last_diag = f"url={url} err={err}"

    return {
        "success": False,
        "context": location,
        "error": "Не удалось измерить скорость (speedtest/wget/curl).",
        "diagnostic": last_diag,
    }


async def speed_test_host(server_name: str) -> Dict:
    return await _speed_test_ctx(server_name, "host")


async def speed_test_vpn(server_name: str) -> Dict:
    return await _speed_test_ctx(server_name, "vpn")


async def speed_test(server_name: str) -> Dict:
    """Совместимость со старым API: тест хоста."""
    return await speed_test_host(server_name)


async def speed_test_both(server_name: str) -> Dict:
    host = await speed_test_host(server_name)
    vpn = await speed_test_vpn(server_name)
    return {"host": host, "vpn": vpn}


async def reboot_server(server_name: str) -> Dict:
    server = _get_server(server_name)
    if not server:
        return {"success": False, "error": f"Server '{server_name}' not found"}

    # Возвращает управление сразу, перезагрузка происходит спустя несколько секунд.
    cmd = "nohup sh -c 'sleep 2 && reboot' >/dev/null 2>&1 &"
    _, err, code = await _exec(server_name, cmd)
    if code == 0:
        return {"success": True}
    return {"success": False, "error": err or "не удалось запланировать reboot"}

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

    # Добавляем peer:
    # 1) Надёжно дописываем блок в wg0.conf (без literal "\n" из echo в разных sh).
    # 2) Пытаемся применить к живому wg0; если интерфейс сейчас не поднят, конфиг всё равно сохранится.
    safe_client_name = client_name.replace("\n", " ").replace("\r", " ")
    peer_lines = [
        "[Peer]",
        f"# {safe_client_name}",
        f"PublicKey = {pubkey}",
        f"PresharedKey = {psk}",
        f"AllowedIPs = {client_ip}",
        "",
    ]
    append_peer_cmd = (
        "printf '%s\\n' "
        + " ".join(_sh_single_quote(line) for line in peer_lines)
        + " >> /opt/amnezia/awg/wg0.conf"
    )
    _, _, append_code = await _exec(server_name, _docker(f"sh -lc {_sh_single_quote(append_peer_cmd)}"))
    if append_code != 0:
        return None

    awg_restore_cmd = ""
    if jc:
        awg_restore_cmd = (
            f"wg set wg0 "
            f"jc {jc.group(1)} "
            f"jmin {jmin.group(1) if jmin else 50} "
            f"jmax {jmax.group(1) if jmax else 1000} "
            f"s1 {s1.group(1) if s1 else 0} "
            f"s2 {s2.group(1) if s2 else 0} "
            f"h1 1 h2 2 h3 3 h4 4"
        )

    runtime_apply_cmd = (
        f"tmp_psk=$(mktemp) && "
        f"printf '%s' {_sh_single_quote(psk)} > \"$tmp_psk\" && "
        f"wg set wg0 peer {pubkey} preshared-key \"$tmp_psk\" allowed-ips {client_ip}; "
        f"rc=$?; "
        + (f"if [ $rc -eq 0 ]; then {awg_restore_cmd}; fi; " if awg_restore_cmd else "")
        + f"rm -f \"$tmp_psk\"; exit $rc"
    )
    await _exec(server_name, _docker(f"sh -lc {_sh_single_quote(runtime_apply_cmd)}"))

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
    table_json = json.dumps(table, indent=4, ensure_ascii=False)
    write_table_cmd = (
        "cat > /opt/amnezia/awg/clientsTable <<'JSONEOF'\n"
        f"{table_json}\n"
        "JSONEOF\n"
        "chmod 644 /opt/amnezia/awg/clientsTable"
    )
    await _exec(server_name, _docker(f"sh -lc {_sh_single_quote(write_table_cmd)}"))

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
