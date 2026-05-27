#!/usr/bin/env python3
"""Daily backup of VPN configs with git sync and rotation."""

import asyncio
import os
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
BACKUP_REMOTE_URL = os.getenv("BACKUP_GIT_REPO_URL", "git@github.com:your-org/your-backup-repo.git")
BACKUP_REPO_DIR = Path(os.getenv("BACKUP_GIT_LOCAL_DIR", "/root/vpn_backup_repo"))
BACKUP_BRANCH = os.getenv("BACKUP_GIT_BRANCH", "main")

# Allows direct script run: ./scripts/backup_vpn_configs.py
# and proper project-module imports from repository root.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import SERVERS
from ssh_manager import local_exec, ssh_exec


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
    )


def _must_git(args: list[str], cwd: Path) -> str:
    proc = _run_git(args, cwd=cwd)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def _single_quote(command: str) -> str:
    return "'" + command.replace("'", "'\"'\"'") + "'"


async def _exec_on_server(server_name: str, command: str, is_local: bool) -> Tuple[str, str, int]:
    if is_local:
        return await local_exec(command)
    return await ssh_exec(server_name, command)


async def _read_container_file(server_name: str, is_local: bool, container_name: str, path: str) -> str:
    cmd = f"docker exec {container_name} sh -lc {_single_quote(f'cat {path}')}"
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

    base_dir = getattr(server, "wg_base_dir", "/opt/amnezia/awg")
    wg_conf = await _read_container_file(server.name, server.is_local, server.vpn_container, server.wg_config_path)
    clients_table = await _read_container_file(server.name, server.is_local, server.vpn_container, f"{base_dir}/clientsTable")

    (backup_dir / "wg0.conf").write_text((wg_conf or "") + "\n", encoding="utf-8")
    (backup_dir / "clientsTable.json").write_text((clients_table or "") + "\n", encoding="utf-8")

    _rotate_backups(server_dir)
    return backup_dir


def _ensure_backup_repo() -> Path:
    if (BACKUP_REPO_DIR / ".git").exists():
        _must_git(["remote", "set-url", "origin", BACKUP_REMOTE_URL], cwd=BACKUP_REPO_DIR)
    else:
        BACKUP_REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        if BACKUP_REPO_DIR.exists():
            shutil.rmtree(BACKUP_REPO_DIR, ignore_errors=True)
        _must_git(["clone", BACKUP_REMOTE_URL, str(BACKUP_REPO_DIR)], cwd=REPO_ROOT)
    return BACKUP_REPO_DIR


def _copy_tree(src: Path, dst: Path):
    dst.mkdir(parents=True, exist_ok=True)
    src_children = {p.name for p in src.iterdir()}
    for p in dst.iterdir():
        if p.name == ".git":
            continue
        if p.name not in src_children:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
    for child in src.iterdir():
        target = dst / child.name
        if child.is_dir():
            _copy_tree(child, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)


def _resolve_backup_branch(backup_repo: Path) -> str:
    branch = BACKUP_BRANCH
    local_has_branch = _run_git(
        ["show-ref", "--verify", f"refs/heads/{branch}"],
        cwd=backup_repo,
    ).returncode == 0
    if local_has_branch:
        return branch

    remote_head_proc = _run_git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=backup_repo)
    if remote_head_proc.returncode == 0:
        return remote_head_proc.stdout.strip().split("/", 1)[1]

    remote_branches = _must_git(["branch", "-r"], cwd=backup_repo).splitlines()
    remote_branches = [b.strip() for b in remote_branches if b.strip() and "->" not in b]
    candidates = [b.split("/", 1)[1] for b in remote_branches if b.startswith("origin/")]
    if "master" in candidates:
        return "master"
    if "main" in candidates:
        return "main"
    if candidates:
        return candidates[0]
    raise RuntimeError("No remote branches found in backup repository")


def _git_sync_backups() -> bool:
    backup_repo = _ensure_backup_repo()
    _must_git(["fetch", "origin"], cwd=backup_repo)
    branch = _resolve_backup_branch(backup_repo)
    local_has_branch = _run_git(
        ["show-ref", "--verify", f"refs/heads/{branch}"],
        cwd=backup_repo,
    ).returncode == 0
    if local_has_branch:
        _must_git(["checkout", branch], cwd=backup_repo)
    else:
        _must_git(["checkout", "-b", branch, f"origin/{branch}"], cwd=backup_repo)
    _must_git(["pull", "--rebase", "origin", branch], cwd=backup_repo)

    target_backup_dir = backup_repo / "backup_config"
    _copy_tree(BACKUP_ROOT, target_backup_dir)
    _must_git(["add", "backup_config"], cwd=backup_repo)

    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=backup_repo,
        text=True,
        capture_output=True,
    )
    if staged.returncode == 0:
        return False

    commit_time = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M:%S MSK")
    _must_git(["commit", "-m", f"backup(vpn): {commit_time}"], cwd=backup_repo)
    _must_git(["push", "origin", branch], cwd=backup_repo)
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
