import asyncio
import base64
import json
import re
import time
import zlib
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

def _docker(server_name: str, cmd: str, stdin: bool = False) -> str:
    server = _get_server(server_name)
    container = server.vpn_container if server else "amnezia-awg"
    stdin_flag = "-i " if stdin else ""
    return f"docker exec {stdin_flag}{container} {cmd}"


def _sh_single_quote(command: str) -> str:
    return "'" + command.replace("'", "'\"'\"'") + "'"


def _build_vpn_uri_from_config(config_text: str) -> str:
    """
    URI format compatible with AmneziaVPN import:
    vpn:// + Base64Url(qCompress(config_text))
    where qCompress = 4-byte length (BE) + zlib(payload).
    """
    raw = config_text.encode("utf-8")
    compressed = zlib.compress(raw, level=8)
    payload = len(raw).to_bytes(4, byteorder="big") + compressed
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"vpn://{encoded}"


def _extract_awg_params(wg_show_out: str, conf_out: str) -> Dict[str, int]:
    conf_names = {
        "jc": "Jc",
        "jmin": "Jmin",
        "jmax": "Jmax",
        "s1": "S1",
        "s2": "S2",
        "h1": "H1",
        "h2": "H2",
        "h3": "H3",
        "h4": "H4",
    }
    params: Dict[str, int] = {}
    for key in ("jc", "jmin", "jmax", "s1", "s2", "h1", "h2", "h3", "h4"):
        m = re.search(rf"^\s*{key}\s*:\s*(\d+)\s*$", wg_show_out, flags=re.MULTILINE)
        if m:
            params[key] = int(m.group(1))
            continue
        conf_key = conf_names[key]
        m = re.search(rf"^\s*{conf_key}\s*=\s*(\d+)\s*$", conf_out, flags=re.MULTILINE)
        if m:
            params[key] = int(m.group(1))
    return params


def _awg_params_text(params: Dict[str, int]) -> str:
    if not params.get("jc"):
        return ""
    return (
        f"Jc = {params.get('jc', 0)}\n"
        f"Jmin = {params.get('jmin', 50)}\n"
        f"Jmax = {params.get('jmax', 1000)}\n"
        f"S1 = {params.get('s1', 0)}\n"
        f"S2 = {params.get('s2', 0)}\n"
        f"H1 = {params.get('h1', 1)}\n"
        f"H2 = {params.get('h2', 2)}\n"
        f"H3 = {params.get('h3', 3)}\n"
        f"H4 = {params.get('h4', 4)}\n"
    )


def _extract_awg_interface_extras(conf_text: str) -> str:
    """
    Keep AWG obfuscation parameters from [Interface] for generated client configs.
    AWG2 relies on values like S3/S4 and I1-I5; copying extras keeps parity with server settings.
    """
    extras: List[str] = []
    in_interface = False
    skip_keys = {
        "privatekey",
        "address",
        "dns",
        "mtu",
        "listenport",
        "fwmark",
        "table",
        "saveconfig",
        "postup",
        "postdown",
        "preup",
        "predown",
    }
    for raw_line in conf_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            in_interface = line.lower() == "[interface]"
            continue
        if not in_interface or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip().lower()
        if key in skip_keys:
            continue
        extras.append(raw_line.strip())
    return "\n".join(extras)


def _is_awg2_like_config(conf_text: str, wg_show_out: str = "") -> bool:
    text = f"{conf_text}\n{wg_show_out}".lower()
    return any(k in text for k in ("s3", "s4", "i1", "i2", "i3", "i4", "i5"))


def _is_awg2_server(server: Optional[ServerConfig]) -> bool:
    if not server:
        return False
    label = (server.protocol_label or "").upper()
    container = (server.vpn_container or "").lower()
    return "AWG2" in label or container.endswith("awg2")


def _build_awg_restore_cmd(params: Dict[str, int], wg_interface: str = "wg0") -> str:
    if not params.get("jc"):
        return ""
    return (
        f"wg set {wg_interface} "
        f"jc {params.get('jc', 0)} "
        f"jmin {params.get('jmin', 50)} "
        f"jmax {params.get('jmax', 1000)} "
        f"s1 {params.get('s1', 0)} "
        f"s2 {params.get('s2', 0)} "
        f"h1 {params.get('h1', 1)} "
        f"h2 {params.get('h2', 2)} "
        f"h3 {params.get('h3', 3)} "
        f"h4 {params.get('h4', 4)}"
    )


def _append_peer_to_conf_text(
    conf_text: str,
    client_name: str,
    pubkey: str,
    psk: str,
    client_ip: str,
) -> str:
    safe_name = client_name.replace("\r", " ").replace("\n", " ")
    block = (
        "[Peer]\n"
        f"# {safe_name}\n"
        f"PublicKey = {pubkey}\n"
        f"PresharedKey = {psk}\n"
        f"AllowedIPs = {client_ip}\n"
    )
    base = conf_text.rstrip()
    if base:
        return base + "\n\n" + block + "\n"
    return block + "\n"


def _remove_peer_from_conf_text(conf_text: str, pubkey: str) -> Tuple[str, bool]:
    lines = conf_text.splitlines()
    out_lines: List[str] = []
    i = 0
    removed = False

    while i < len(lines):
        if lines[i].strip() != "[Peer]":
            out_lines.append(lines[i])
            i += 1
            continue

        block: List[str] = [lines[i]]
        i += 1
        while i < len(lines) and lines[i].strip() != "[Peer]":
            block.append(lines[i])
            i += 1

        is_target = False
        for ln in block:
            normalized = re.sub(r"\s+", "", ln)
            if normalized == f"PublicKey={pubkey}":
                is_target = True
                break

        if is_target:
            removed = True
            continue
        out_lines.extend(block)

    result = "\n".join(out_lines).rstrip() + "\n"
    return result, removed


def _replace_peer_allowed_ips_in_conf_text(conf_text: str, pubkey: str, new_allowed_ips: str) -> Tuple[str, bool]:
    lines = conf_text.splitlines()
    out_lines: List[str] = []
    i = 0
    changed = False

    while i < len(lines):
        if lines[i].strip() != "[Peer]":
            out_lines.append(lines[i])
            i += 1
            continue

        block: List[str] = [lines[i]]
        i += 1
        while i < len(lines) and lines[i].strip() != "[Peer]":
            block.append(lines[i])
            i += 1

        is_target = False
        for ln in block:
            normalized = re.sub(r"\s+", "", ln)
            if normalized == f"PublicKey={pubkey}":
                is_target = True
                break

        if not is_target:
            out_lines.extend(block)
            continue

        replaced = False
        new_block: List[str] = []
        for ln in block:
            if re.match(r"^\s*AllowedIPs\s*=", ln):
                current_allowed = ln.split("=", 1)[1].strip() if "=" in ln else ""
                if current_allowed != new_allowed_ips:
                    new_block.append(f"AllowedIPs = {new_allowed_ips}")
                    changed = True
                else:
                    new_block.append(ln)
                replaced = True
            else:
                new_block.append(ln)
        if not replaced:
            new_block.append(f"AllowedIPs = {new_allowed_ips}")
            changed = True
        out_lines.extend(new_block)

    result = "\n".join(out_lines).rstrip() + "\n"
    return result, changed


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


async def _read_awg_file(server_name: str, path: str) -> Optional[str]:
    out, _, code = await _exec(server_name, _docker(server_name, f"cat {path}"))
    if code != 0:
        return None
    return out


async def _write_awg_file(server_name: str, path: str, content: str, mode: str = "600") -> bool:
    write_cmd = (
        f"cat > {path} <<'CFGEOF'\n"
        f"{content}"
        "CFGEOF\n"
        f"chmod {mode} {path}"
    )
    _, _, code = await _exec(server_name, _docker(server_name, f"sh -lc {_sh_single_quote(write_cmd)}"))
    return code == 0


async def _validate_awg_conf_text(server_name: str, conf_text: str) -> bool:
    """
    Validate syntax via wg-quick strip.
    Important: use *.conf, otherwise wg-quick fails on filename check.
    """
    cmd = (
        "tmp_base=$(mktemp /tmp/wg0XXXXXX) && "
        "tmp_conf=\"${tmp_base}.conf\" && "
        "mv \"$tmp_base\" \"$tmp_conf\" && "
        "cat > \"$tmp_conf\" <<'CFGEOF'\n"
        f"{conf_text}"
        "CFGEOF\n"
        "wg-quick strip \"$tmp_conf\" >/dev/null 2>&1; "
        "rc=$?; rm -f \"$tmp_conf\"; exit $rc"
    )
    _, _, code = await _exec(server_name, _docker(server_name, f"sh -lc {_sh_single_quote(cmd)}"))
    return code == 0


async def _sync_wg_runtime_from_conf(server_name: str, conf_path: Optional[str] = None) -> bool:
    server = _get_server(server_name)
    if not server:
        return False
    conf_path = conf_path or server.wg_config_path
    wg_interface = server.wg_interface
    cmd = (
        "tmp_base=$(mktemp /tmp/wg1XXXXXX) && "
        "tmp_conf=\"${tmp_base}.conf\" && "
        "mv \"$tmp_base\" \"$tmp_conf\" && "
        f"cp {conf_path} \"$tmp_conf\" && "
        "wg-quick strip \"$tmp_conf\" > \"$tmp_conf.stripped\" && "
        f"wg syncconf {wg_interface} \"$tmp_conf.stripped\"; "
        "rc=$?; rm -f \"$tmp_conf\" \"$tmp_conf.stripped\"; exit $rc"
    )
    _, _, code = await _exec(server_name, _docker(server_name, f"sh -lc {_sh_single_quote(cmd)}"))
    return code == 0


async def _backup_awg_conf(server_name: str) -> bool:
    server = _get_server(server_name)
    if not server:
        return False
    conf_path = server.wg_config_path
    cmd = f"cp {conf_path} {conf_path}.autobak_$(date +%Y%m%d_%H%M%S)"
    _, _, code = await _exec(server_name, _docker(server_name, f"sh -lc {_sh_single_quote(cmd)}"))
    return code == 0


async def _apply_awg_conf_with_rollback(server_name: str, conf_new: str, conf_old: str) -> bool:
    server = _get_server(server_name)
    if not server:
        return False
    conf_path = server.wg_config_path
    if not await _validate_awg_conf_text(server_name, conf_new):
        return False
    if not await _write_awg_file(server_name, conf_path, conf_new, mode="600"):
        return False

    restore_new_cmd = ""
    if not _is_awg2_like_config(conf_new):
        restore_new_cmd = _build_awg_restore_cmd(_extract_awg_params("", conf_new), server.wg_interface)
    if await _sync_wg_runtime_from_conf(server_name):
        if restore_new_cmd:
            _, _, restore_code = await _exec(server_name, _docker(server_name, f"sh -lc {_sh_single_quote(restore_new_cmd)}"))
            if restore_code == 0:
                return True
        else:
            return True

    # Roll back if runtime apply failed
    await _write_awg_file(server_name, conf_path, conf_old, mode="600")
    if await _sync_wg_runtime_from_conf(server_name):
        restore_old_cmd = ""
        if not _is_awg2_like_config(conf_old):
            restore_old_cmd = _build_awg_restore_cmd(_extract_awg_params("", conf_old), server.wg_interface)
        if restore_old_cmd:
            await _exec(server_name, _docker(server_name, f"sh -lc {_sh_single_quote(restore_old_cmd)}"))
    return False


async def _load_clients_table_strict(server_name: str) -> Optional[List[Dict]]:
    server = _get_server(server_name)
    if not server:
        return None
    raw = await _read_awg_file(server_name, f"{server.wg_base_dir}/clientsTable")
    if raw is None:
        return []
    if not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, list) else None


async def _write_clients_table(server_name: str, table: List[Dict]) -> bool:
    server = _get_server(server_name)
    if not server:
        return False
    payload = json.dumps(table, indent=4, ensure_ascii=False) + "\n"
    return await _write_awg_file(server_name, f"{server.wg_base_dir}/clientsTable", payload, mode="644")

# ─── Monitoring ───────────────────────────────────────────────────────────────

async def get_server_status(server_name: str) -> Dict:
    server = _get_server(server_name)
    if not server:
        return {"error": "not found", "name": server_name}
    try:
        uptime_out, _, _ = await _exec(server_name, "uptime -p")
        load_out, _, _ = await _exec(server_name, "cat /proc/loadavg")
        wg_out, _, code = await _exec(server_name, _docker(server_name, "wg show"))
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
    table = await _load_clients_table_strict(server_name)
    return table if table is not None else []

async def get_wg_dump(server_name: str) -> Dict[str, Dict]:
    server = _get_server(server_name)
    if not server:
        return {}
    out, _, _ = await _exec(server_name, _docker(server_name, f"wg show {server.wg_interface} dump"))
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

    # Some keys may exist in dump but be absent in clientsTable
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

    # Stable sort by name, then by key
    return sorted(result_map.values(), key=lambda p: ((p.get("name") or "").lower(), p["pubkey"]))

# ─── Ping and speed ───────────────────────────────────────────────────────────

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
        wrapped = _docker(server_name, f"sh -lc {_sh_single_quote(cmd)}")
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
            # Some speedtest-cli versions in container return 0.00/0.00.
            # Treat as invalid and try alternatives.
            if data and _is_nonzero_speed_result(data):
                return {"success": True, "context": location, "method": "speedtest-cli", **data}

    # Attempt 2: wget (download only) across several URLs
    for url in test_urls:
        wget_cmd = f"wget -O /dev/null --report-speed=bits --timeout=15 {url} 2>&1 | tail -5"
        out, _, _ = await _exec_in_context(server_name, wget_cmd, context)
        mbps = _parse_wget_download(out)
        if mbps is not None:
            return {"success": True, "context": location, "method": f"wget ({url})", "download_mbps": mbps}

    # Attempt 3: curl (download only) across several URLs with size/http_code validation
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
    """Backward compatibility: host speed test."""
    return await speed_test_host(server_name)


async def speed_test_both(server_name: str) -> Dict:
    host = await speed_test_host(server_name)
    vpn = await speed_test_vpn(server_name)
    return {"host": host, "vpn": vpn}


async def reboot_server(server_name: str) -> Dict:
    server = _get_server(server_name)
    if not server:
        return {"success": False, "error": f"Server '{server_name}' not found"}

    # Returns immediately; reboot happens a few seconds later.
    cmd = "nohup sh -c 'sleep 2 && reboot' >/dev/null 2>&1 &"
    _, err, code = await _exec(server_name, cmd)
    if code == 0:
        return {"success": True}
    return {"success": False, "error": err or "не удалось запланировать reboot"}

# ─── Peer management ──────────────────────────────────────────────────────────

async def add_peer(server_name: str, client_name: str) -> Optional[Dict]:
    server = _get_server(server_name)
    if not server:
        return None
    conf_path = server.wg_config_path
    base_dir = server.wg_base_dir
    wg_iface = server.wg_interface

    priv_out, _, code = await _exec(server_name, _docker(server_name, "wg genkey"))
    if code != 0 or not priv_out:
        return None
    privkey = priv_out.strip()

    pub_out, _, code = await _exec(server_name, f"echo '{privkey}' | {_docker(server_name, 'wg pubkey', stdin=True)}")
    if code != 0 or not pub_out:
        return None
    pubkey = pub_out.strip()

    psk_out, _, _ = await _exec(server_name, _docker(server_name, "wg genpsk"))
    psk = psk_out.strip()

    conf_out = await _read_awg_file(server_name, conf_path)
    if conf_out is None:
        return None
    used_ips = set(int(x) for x in re.findall(r"AllowedIPs\s*=\s*10\.8\.1\.(\d+)", conf_out))
    next_num = next(i for i in range(2, 255) if i not in used_ips)
    client_ip = f"10.8.1.{next_num}/32"

    server_pub_out, _, _ = await _exec(server_name, _docker(server_name, f"cat {base_dir}/wireguard_server_public_key.key"))
    server_pubkey = server_pub_out.strip()

    wg_show_out, _, _ = await _exec(server_name, _docker(server_name, f"wg show {wg_iface}"))
    awg_params = _extract_awg_params(wg_show_out, conf_out)
    port_match = re.search(r"listening port:\s*(\d+)", wg_show_out)
    port = port_match.group(1) if port_match else "46742"

    conf_new = _append_peer_to_conf_text(conf_out, client_name, pubkey, psk, client_ip)
    if not await _validate_awg_conf_text(server_name, conf_new):
        return None
    await _backup_awg_conf(server_name)
    if not await _write_awg_file(server_name, conf_path, conf_new, mode="600"):
        return None

    # Important: AWG2 has extra obfuscation params (S3/S4, I1-I5 and ranged H1-H4).
    # Re-applying AWG1-only params here can break an AWG2 interface.
    awg_restore_cmd = ""
    if not _is_awg2_like_config(conf_out, wg_show_out):
        awg_restore_cmd = _build_awg_restore_cmd(awg_params, wg_iface)

    runtime_apply_cmd = (
        f"tmp_psk=$(mktemp) && "
        f"printf '%s' {_sh_single_quote(psk)} > \"$tmp_psk\" && "
        f"wg set {wg_iface} peer {pubkey} preshared-key \"$tmp_psk\" allowed-ips {client_ip}; "
        f"rc=$?; "
        + (
            f"if [ $rc -eq 0 ] && ! ({awg_restore_cmd}); then rc=1; fi; "
            if awg_restore_cmd else ""
        )
        + f"rm -f \"$tmp_psk\"; exit $rc"
    )
    _, _, apply_code = await _exec(server_name, _docker(server_name, f"sh -lc {_sh_single_quote(runtime_apply_cmd)}"))
    if apply_code != 0:
        # If runtime apply failed, roll back config file.
        await _exec(server_name, _docker(server_name, f"wg set {wg_iface} peer {pubkey} remove"))
        await _write_awg_file(server_name, conf_path, conf_out, mode="600")
        await _sync_wg_runtime_from_conf(server_name)
        return None

    # Update clientsTable (if file is corrupted, do not overwrite it).
    import datetime
    table = await _load_clients_table_strict(server_name)
    if table is not None:
        table = [c for c in table if c.get("clientId") != pubkey]
        table.append({
            "clientId": pubkey,
            "userData": {
                "allowedIps": client_ip,
                "clientName": client_name,
                "creationDate": datetime.datetime.now().strftime("%a %b %-d %H:%M:%S %Y"),
            }
        })
        await _write_clients_table(server_name, table)

    amnezia_params = _extract_awg_interface_extras(conf_out)

    client_config = (
        f"[Interface]\nPrivateKey = {privkey}\nAddress = {client_ip}\nDNS = 1.1.1.1\n"
        + (f"{amnezia_params}\n" if amnezia_params else "")
        + f"[Peer]\nPublicKey = {server_pubkey}\nPresharedKey = {psk}\n"
        f"Endpoint = {server.host}:{port}\nAllowedIPs = 0.0.0.0/0\nPersistentKeepalive = 25\n"
    )
    return {"pubkey": pubkey, "privkey": privkey, "client_ip": client_ip,
            "config_text": client_config, "server_name": server_name,
            "vpn_uri": _build_vpn_uri_from_config(client_config)}

async def remove_peer(server_name: str, pubkey: str) -> bool:
    server = _get_server(server_name)
    if not server:
        return False
    conf_out = await _read_awg_file(server_name, server.wg_config_path)
    if conf_out is None:
        return False
    conf_new, removed = _remove_peer_from_conf_text(conf_out, pubkey)
    if removed:
        await _backup_awg_conf(server_name)
        if _is_awg2_server(server):
            _, _, code = await _exec(server_name, _docker(server_name, f"wg set {server.wg_interface} peer {pubkey} remove"))
            if code != 0:
                return False
            if not await _write_awg_file(server_name, server.wg_config_path, conf_new, mode="600"):
                return False
        else:
            if not await _apply_awg_conf_with_rollback(server_name, conf_new, conf_out):
                return False

    table = await _load_clients_table_strict(server_name)
    if table is not None:
        table = [c for c in table if c.get("clientId") != pubkey]
        await _write_clients_table(server_name, table)
    return True

async def disable_peer(server_name: str, pubkey: str) -> bool:
    server = _get_server(server_name)
    if not server:
        return False
    conf_out = await _read_awg_file(server_name, server.wg_config_path)
    if conf_out is None:
        return False

    conf_new, changed = _replace_peer_allowed_ips_in_conf_text(conf_out, pubkey, "192.0.2.0/32")
    if changed:
        await _backup_awg_conf(server_name)
        if _is_awg2_server(server):
            _, _, code = await _exec(server_name, _docker(server_name, f"wg set {server.wg_interface} peer {pubkey} allowed-ips 192.0.2.0/32"))
            if code != 0:
                return False
            if not await _write_awg_file(server_name, server.wg_config_path, conf_new, mode="600"):
                return False
        else:
            if not await _apply_awg_conf_with_rollback(server_name, conf_new, conf_out):
                return False
    return True

async def enable_peer(server_name: str, pubkey: str, client_ip: str) -> bool:
    if not client_ip:
        return False

    server = _get_server(server_name)
    if not server:
        return False
    conf_out = await _read_awg_file(server_name, server.wg_config_path)
    if conf_out is None:
        return False

    conf_new, changed = _replace_peer_allowed_ips_in_conf_text(conf_out, pubkey, client_ip)
    if changed:
        await _backup_awg_conf(server_name)
        if _is_awg2_server(server):
            _, _, code = await _exec(server_name, _docker(server_name, f"wg set {server.wg_interface} peer {pubkey} allowed-ips {client_ip}"))
            if code != 0:
                return False
            if not await _write_awg_file(server_name, server.wg_config_path, conf_new, mode="600"):
                return False
        else:
            if not await _apply_awg_conf_with_rollback(server_name, conf_new, conf_out):
                return False
    return True
