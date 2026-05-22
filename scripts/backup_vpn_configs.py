#!/usr/bin/env python3
"""Ежедневный бэкап VPN-конфигов с git sync и ротацией."""

import asyncio
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Tuple
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKUP_ROOT = REPO_ROOT / "backup_config"
MAX_BACKUPS_PER_SERVER = 30
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# Позволяет запускать скрипт напрямую: ./scripts/backup_vpn_configs.py
# и корректно импортировать модули проекта из корня репозитория.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import SERVERS
from ssh_manager import local_exec, ssh_exec


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )


def _must_git(args: list[str]) -> str:
    proc = _run_git(args)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def _single_quote(command: str) -> str:
    return "'" + command.replace("'", "'\"'\"'") + "'"


async def _exec_on_server(server_name: str, command: str, is_local: bool) -> Tuple[str, str, int]:
    if is_local:
        return await local_exec(command)
    return await ssh_exec(server_name, command)


async def _read_container_file(server_name: str, is_local: bool, path: str) -> str:
    cmd = f"docker exec amnezia-awg sh -lc {_single_quote(f'cat {path}')}"
    out, err, code = await _exec_on_server(server_name, cmd, is_local)
    if code != 0:
        raise RuntimeError(err or f"exit code {code}")
    return out


def _rotate_backups(server_dir: Path, keep: int = MAX_BACKUPS_PER_SERVER):
    snapshots = sorted([p for p in server_dir.iterdir() if p.is_dir()])
    if len(snapshots) <= keep:
        return
    for old in snapshots[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


async def _backup_server(server) -> Path:
    ts = datetime.now(MOSCOW_TZ).strftime("%Y%m%d_%H%M%S")
    server_dir = BACKUP_ROOT / server.name
    backup_dir = server_dir / ts
    backup_dir.mkdir(parents=True, exist_ok=True)

    wg_conf = await _read_container_file(server.name, server.is_local, "/opt/amnezia/awg/wg0.conf")
    clients_table = await _read_container_file(server.name, server.is_local, "/opt/amnezia/awg/clientsTable")

    (backup_dir / "wg0.conf").write_text((wg_conf or "") + "\n", encoding="utf-8")
    (backup_dir / "clientsTable.json").write_text((clients_table or "") + "\n", encoding="utf-8")

    _rotate_backups(server_dir)
    return backup_dir


def _git_sync_backups() -> bool:
    branch = _must_git(["rev-parse", "--abbrev-ref", "HEAD"])
    _must_git(["pull", "--rebase", "origin", branch])
    _must_git(["add", "backup_config"])

    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    if staged.returncode == 0:
        return False

    commit_time = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M:%S MSK")
    _must_git(["commit", "-m", f"backup(vpn): {commit_time}"])
    _must_git(["push", "origin", branch])
    return True


async def main() -> int:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []

    for server in SERVERS:
        try:
            path = await _backup_server(server)
            print(f"[OK] {server.name}: {path}")
        except Exception as exc:
            errors.append(f"{server.name}: {exc}")
            print(f"[ERR] {server.name}: {exc}", file=sys.stderr)

    try:
        pushed = _git_sync_backups()
        print("[OK] git push done" if pushed else "[OK] no changes to push")
    except Exception as exc:
        print(f"[ERR] git sync failed: {exc}", file=sys.stderr)
        return 2

    if errors:
        print("\nBackup finished with errors:", file=sys.stderr)
        for err in errors:
            print(f"- {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
