"""Persistent state for support dialogs between clients and admins."""

import sqlite3
from datetime import datetime
from typing import Optional, Union

from database import DB_PATH

SUPPORT_CLIENT_OPEN_TEXT = "💬 Написать в поддержку"
SUPPORT_ADMIN_MENU_TEXT = "💬 Поддержка"
SUPPORT_CLOSE_TEXT = "❌ Закрыть диалог"
SUPPORT_BROADCAST_TARGET = "__broadcast__"

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _ensure_tables(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS support_dialogs (
            client_tg_id INTEGER PRIMARY KEY,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS support_admin_targets (
            admin_tg_id INTEGER PRIMARY KEY,
            target_type TEXT NOT NULL,
            target_tg_id INTEGER,
            updated_at TEXT NOT NULL
        )
        """
    )


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def open_client_dialog(client_tg_id: int):
    client_tg_id = int(client_tg_id)
    with _connect() as conn:
        _ensure_tables(conn)
        conn.execute(
            """
            INSERT INTO support_dialogs (client_tg_id, updated_at)
            VALUES (?, ?)
            ON CONFLICT(client_tg_id) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (client_tg_id, _now_iso()),
        )


def is_client_dialog_open(client_tg_id: int) -> bool:
    client_tg_id = int(client_tg_id)
    with _connect() as conn:
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT 1 FROM support_dialogs WHERE client_tg_id = ?",
            (client_tg_id,),
        ).fetchone()
    return bool(row)


def close_client_dialog(client_tg_id: int):
    client_tg_id = int(client_tg_id)
    with _connect() as conn:
        _ensure_tables(conn)
        conn.execute("DELETE FROM support_dialogs WHERE client_tg_id = ?", (client_tg_id,))
        conn.execute(
            "DELETE FROM support_admin_targets WHERE target_type = 'client' AND target_tg_id = ?",
            (client_tg_id,),
        )


def set_admin_target(admin_tg_id: int, target: Union[int, str]):
    admin_tg_id = int(admin_tg_id)
    if target == SUPPORT_BROADCAST_TARGET:
        target_type = "broadcast"
        target_tg_id = None
    else:
        target_type = "client"
        target_tg_id = int(target)

    with _connect() as conn:
        _ensure_tables(conn)
        conn.execute(
            """
            INSERT INTO support_admin_targets (admin_tg_id, target_type, target_tg_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(admin_tg_id) DO UPDATE
            SET target_type = excluded.target_type,
                target_tg_id = excluded.target_tg_id,
                updated_at = excluded.updated_at
            """,
            (admin_tg_id, target_type, target_tg_id, _now_iso()),
        )


def get_admin_target(admin_tg_id: int) -> Optional[Union[int, str]]:
    admin_tg_id = int(admin_tg_id)
    with _connect() as conn:
        _ensure_tables(conn)
        row = conn.execute(
            """
            SELECT target_type, target_tg_id
            FROM support_admin_targets
            WHERE admin_tg_id = ?
            """,
            (admin_tg_id,),
        ).fetchone()
    if not row:
        return None
    if row["target_type"] == "broadcast":
        return SUPPORT_BROADCAST_TARGET
    if row["target_tg_id"] is None:
        return None
    return int(row["target_tg_id"])


def clear_admin_target(admin_tg_id: int):
    admin_tg_id = int(admin_tg_id)
    with _connect() as conn:
        _ensure_tables(conn)
        conn.execute("DELETE FROM support_admin_targets WHERE admin_tg_id = ?", (admin_tg_id,))
