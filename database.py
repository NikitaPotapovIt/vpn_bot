import aiosqlite
from datetime import datetime, date
from typing import Optional, List, Dict
from dataclasses import dataclass

from config import DEVICE_MONTHLY_PRICE

DB_PATH = "vpn_bot.db"


@dataclass
class Client:
    id: int
    telegram_id: int
    name: str
    username: Optional[str]
    server_name: str
    devices: int
    monthly_fee: float
    active: bool
    payment_status: str
    payment_date: Optional[str]
    reminder_day: int
    disconnect_date: Optional[str]
    wg_pubkey: Optional[str]
    wg_peer_id: Optional[str]
    paid_until: Optional[str]
    key_count: int = 0
    payable_key_count: int = 0
    nonpayable_key_count: int = 0


@dataclass
class ClientKey:
    id: int
    server_name: str
    wg_pubkey: str
    key_name: Optional[str]
    allowed_ips: Optional[str]
    created_at: Optional[str]
    connected: bool
    last_handshake: int
    rx_bytes: int
    tx_bytes: int
    endpoint: Optional[str]
    active: bool
    payer: bool
    paused: bool
    client_id: Optional[int]  # legacy column (kept for compatibility)
    billing_client_id: Optional[int]
    billing_client_name: Optional[str] = None
    linked_clients: int = 0


@dataclass
class KeyAccessClient:
    client_id: int
    name: str
    username: Optional[str]


def _calc_monthly_fee(key_count: int, payable_devices: int, fallback_fee: float) -> float:
    # Если у клиента уже есть связанные ключи, считаем строго по платным ключам.
    # Fallback нужен только для legacy-записей без ключей.
    if key_count > 0:
        return float(payable_devices * DEVICE_MONTHLY_PRICE)
    return float(fallback_fee)


def _calc_devices(key_count: int, fallback_devices: int) -> int:
    if key_count > 0:
        return int(key_count)
    return int(fallback_devices)


async def _get_columns(db: aiosqlite.Connection, table_name: str) -> List[str]:
    async with db.execute(f"PRAGMA table_info({table_name})") as cur:
        rows = await cur.fetchall()
    return [r[1] for r in rows]


async def _get_key_id(db: aiosqlite.Connection, server_name: str, wg_pubkey: str) -> Optional[int]:
    async with db.execute(
        "SELECT id FROM client_keys WHERE server_name = ? AND wg_pubkey = ?",
        (server_name, wg_pubkey),
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else None


async def _ensure_key_access(db: aiosqlite.Connection, key_id: int, client_id: int):
    await db.execute(
        "INSERT OR IGNORE INTO key_access (key_id, client_id) VALUES (?, ?)",
        (key_id, client_id),
    )


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE,
                name TEXT NOT NULL,
                username TEXT,
                server_name TEXT NOT NULL,
                devices INTEGER DEFAULT 1,
                monthly_fee REAL NOT NULL,
                active INTEGER DEFAULT 1,
                payment_status TEXT DEFAULT 'pending',
                payment_date TEXT,
                reminder_day INTEGER DEFAULT 0,
                disconnect_date TEXT,
                wg_pubkey TEXT,
                wg_peer_id TEXT,
                paid_until TEXT
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS client_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_name TEXT NOT NULL,
                wg_pubkey TEXT NOT NULL,
                key_name TEXT,
                allowed_ips TEXT,
                created_at TEXT,
                connected INTEGER DEFAULT 0,
                last_handshake INTEGER DEFAULT 0,
                rx_bytes INTEGER DEFAULT 0,
                tx_bytes INTEGER DEFAULT 0,
                endpoint TEXT,
                active INTEGER DEFAULT 1,
                payer INTEGER DEFAULT 1,
                paused INTEGER DEFAULT 0,
                client_id INTEGER,
                billing_client_id INTEGER,
                UNIQUE(server_name, wg_pubkey),
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE SET NULL,
                FOREIGN KEY (billing_client_id) REFERENCES clients(id) ON DELETE SET NULL
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS key_access (
                key_id INTEGER NOT NULL,
                client_id INTEGER NOT NULL,
                PRIMARY KEY (key_id, client_id),
                FOREIGN KEY (key_id) REFERENCES client_keys(id) ON DELETE CASCADE,
                FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER,
                action TEXT,
                amount REAL,
                timestamp TEXT,
                note TEXT
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS support_dialogs (
                client_tg_id INTEGER PRIMARY KEY,
                updated_at TEXT NOT NULL
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS support_admin_targets (
                admin_tg_id INTEGER PRIMARY KEY,
                target_type TEXT NOT NULL,
                target_tg_id INTEGER,
                updated_at TEXT NOT NULL
            )
            """
        )

        # Миграции старых баз
        client_columns = await _get_columns(db, "clients")
        if "paid_until" not in client_columns:
            await db.execute("ALTER TABLE clients ADD COLUMN paid_until TEXT")

        key_columns = await _get_columns(db, "client_keys")
        if "billing_client_id" not in key_columns:
            await db.execute("ALTER TABLE client_keys ADD COLUMN billing_client_id INTEGER")
        if "paused" not in key_columns:
            await db.execute("ALTER TABLE client_keys ADD COLUMN paused INTEGER DEFAULT 0")

        await db.execute("CREATE INDEX IF NOT EXISTS idx_client_keys_client_id ON client_keys(client_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_client_keys_billing_client_id ON client_keys(billing_client_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_client_keys_server ON client_keys(server_name)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_key_access_client_id ON key_access(client_id)")

        # Перенос legacy-полей wg_pubkey/wg_peer_id в таблицу ключей
        async with db.execute(
            "SELECT id, server_name, name, active, wg_pubkey, wg_peer_id FROM clients WHERE wg_pubkey IS NOT NULL AND wg_pubkey != ''"
        ) as cur:
            legacy_rows = await cur.fetchall()

        for row in legacy_rows:
            client_id, server_name, client_name, active, wg_pubkey, wg_peer_id = row
            await db.execute(
                """
                INSERT OR IGNORE INTO client_keys (
                    server_name, wg_pubkey, key_name, allowed_ips, active, payer, client_id, billing_client_id
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (server_name, wg_pubkey, client_name, wg_peer_id, 1 if active else 0, client_id, client_id),
            )

        # Миграция client_keys.client_id -> key_access
        await db.execute(
            """
            INSERT OR IGNORE INTO key_access (key_id, client_id)
            SELECT id, client_id
            FROM client_keys
            WHERE client_id IS NOT NULL
            """
        )

        # Заполнить billing_client_id где возможно
        await db.execute(
            """
            UPDATE client_keys
            SET billing_client_id = client_id
            WHERE billing_client_id IS NULL AND client_id IS NOT NULL
            """
        )

        await db.execute(
            """
            UPDATE client_keys
            SET billing_client_id = (
                SELECT ka.client_id FROM key_access ka WHERE ka.key_id = client_keys.id LIMIT 1
            )
            WHERE billing_client_id IS NULL
              AND EXISTS (SELECT 1 FROM key_access ka2 WHERE ka2.key_id = client_keys.id)
            """
        )

        await db.commit()


async def add_client(
    telegram_id: Optional[int],
    name: str,
    username: Optional[str],
    server_name: str,
    devices: int,
    monthly_fee: float,
    wg_pubkey: str = None,
    wg_peer_id: str = None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        cur = await db.execute(
            """
            INSERT INTO clients (
                telegram_id, name, username, server_name, devices, monthly_fee, payment_status, wg_pubkey, wg_peer_id
            )
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (telegram_id, name, username, server_name, devices, monthly_fee, wg_pubkey, wg_peer_id),
        )
        client_id = cur.lastrowid

        if wg_pubkey:
            await db.execute(
                """
                INSERT OR IGNORE INTO client_keys (
                    server_name, wg_pubkey, key_name, allowed_ips, active, payer, client_id, billing_client_id
                ) VALUES (?, ?, ?, ?, 1, 1, ?, ?)
                """,
                (server_name, wg_pubkey, name, wg_peer_id, client_id, client_id),
            )
            await db.execute(
                """
                UPDATE client_keys
                SET key_name = COALESCE(key_name, ?),
                    allowed_ips = COALESCE(allowed_ips, ?),
                    billing_client_id = COALESCE(billing_client_id, ?),
                    client_id = COALESCE(client_id, ?)
                WHERE server_name = ? AND wg_pubkey = ?
                """,
                (name, wg_peer_id, client_id, client_id, server_name, wg_pubkey),
            )

            key_id = await _get_key_id(db, server_name, wg_pubkey)
            if key_id:
                await _ensure_key_access(db, key_id, client_id)

        await db.commit()
        return client_id


async def _fetch_clients(where_sql: str = "", params: tuple = ()) -> List[Client]:
    query = f"""
        SELECT
            c.*,
            COALESCE(COUNT(DISTINCT k.id), 0) AS key_count,
            COALESCE(COUNT(DISTINCT CASE WHEN k.payer = 1 AND k.billing_client_id = c.id THEN k.id END), 0) AS payable_key_count
        FROM clients c
        LEFT JOIN key_access ka ON ka.client_id = c.id
        LEFT JOIN client_keys k ON k.id = ka.key_id AND k.active = 1
        {where_sql}
        GROUP BY c.id
        ORDER BY c.name
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [_row_to_client(r) for r in rows]


async def get_client_by_tg(telegram_id: int) -> Optional[Client]:
    clients = await _fetch_clients("WHERE c.telegram_id = ?", (telegram_id,))
    return clients[0] if clients else None


async def get_client_by_username(username: str) -> Optional[Client]:
    normalized = (username or "").strip().lstrip("@").lower()
    if not normalized:
        return None
    clients = await _fetch_clients("WHERE LOWER(c.username) = ?", (normalized,))
    return clients[0] if clients else None


async def get_unbound_client_by_username(username: str) -> Optional[Client]:
    normalized = (username or "").strip().lstrip("@").lower()
    if not normalized:
        return None
    clients = await _fetch_clients(
        "WHERE LOWER(c.username) = ? AND c.telegram_id IS NULL",
        (normalized,),
    )
    return clients[0] if clients else None


async def get_client_by_id(client_id: int) -> Optional[Client]:
    clients = await _fetch_clients("WHERE c.id = ?", (client_id,))
    return clients[0] if clients else None


async def get_all_clients() -> List[Client]:
    return await _fetch_clients()


async def get_active_clients() -> List[Client]:
    return await _fetch_clients("WHERE c.active = 1")


async def update_payment_status(client_id: int, status: str, payment_date: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if payment_date:
            await db.execute(
                """
                UPDATE clients
                SET payment_status = ?, payment_date = ?, reminder_day = 0, disconnect_date = NULL
                WHERE id = ?
                """,
                (status, payment_date, client_id),
            )
        else:
            await db.execute("UPDATE clients SET payment_status = ? WHERE id = ?", (status, client_id))
        await db.commit()


async def set_paid_until(client_id: int, paid_until: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE clients SET paid_until = ? WHERE id = ?", (paid_until, client_id))
        await db.commit()


async def increment_reminder_day(client_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE clients SET reminder_day = reminder_day + 1 WHERE id = ?", (client_id,))
        await db.commit()


async def set_disconnect_date(client_id: int, disconnect_date: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE clients SET disconnect_date = ? WHERE id = ?", (disconnect_date, client_id))
        await db.commit()


async def set_client_active(client_id: int, active: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE clients SET active = ? WHERE id = ?", (1 if active else 0, client_id))
        await db.commit()


async def reset_monthly_payments():
    """1-го числа: pending только у реально неоплаченных клиентов"""
    today = date.today().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE clients
            SET payment_status = 'pending', reminder_day = 0, disconnect_date = NULL
            WHERE active = 1
              AND payment_status != 'waiting_confirm'
              AND (paid_until IS NULL OR paid_until < ?)
            """,
            (today,),
        )
        await db.execute(
            """
            UPDATE clients
            SET payment_status = 'paid', reminder_day = 0, disconnect_date = NULL
            WHERE active = 1
              AND paid_until IS NOT NULL
              AND paid_until >= ?
            """,
            (today,),
        )
        await db.commit()


async def log_payment(client_id: int, action: str, amount: float = 0, note: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO payment_log (client_id, action, amount, timestamp, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (client_id, action, amount, datetime.now().isoformat(), note),
        )
        await db.commit()


async def get_last_payment_log(client_id: int, actions: Optional[List[str]] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if actions:
            placeholders = ",".join("?" for _ in actions)
            query = f"""
                SELECT id, client_id, action, amount, timestamp, note
                FROM payment_log
                WHERE client_id = ? AND action IN ({placeholders})
                ORDER BY id DESC
                LIMIT 1
            """
            params = (client_id, *actions)
        else:
            query = """
                SELECT id, client_id, action, amount, timestamp, note
                FROM payment_log
                WHERE client_id = ?
                ORDER BY id DESC
                LIMIT 1
            """
            params = (client_id,)
        async with db.execute(query, params) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_payment_logs_for_day(client_id: int, day: str, actions: Optional[List[str]] = None):
    """Возвращает платежные логи клиента за конкретную дату (YYYY-MM-DD), по возрастанию id."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if actions:
            placeholders = ",".join("?" for _ in actions)
            query = f"""
                SELECT id, client_id, action, amount, timestamp, note
                FROM payment_log
                WHERE client_id = ?
                  AND substr(timestamp, 1, 10) = ?
                  AND action IN ({placeholders})
                ORDER BY id ASC
            """
            params = (client_id, day, *actions)
        else:
            query = """
                SELECT id, client_id, action, amount, timestamp, note
                FROM payment_log
                WHERE client_id = ?
                  AND substr(timestamp, 1, 10) = ?
                ORDER BY id ASC
            """
            params = (client_id, day)
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def update_client_fields(client_id: int, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [client_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE clients SET {fields} WHERE id = ?", values)
        await db.commit()


async def upsert_client_key(
    server_name: str,
    wg_pubkey: str,
    key_name: Optional[str] = None,
    allowed_ips: Optional[str] = None,
    created_at: Optional[str] = None,
    connected: bool = False,
    last_handshake: int = 0,
    rx_bytes: int = 0,
    tx_bytes: int = 0,
    endpoint: Optional[str] = None,
    active: bool = True,
    client_id: Optional[int] = None,
    payer: Optional[bool] = None,
    billing_client_id: Optional[int] = None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO client_keys (
                server_name, wg_pubkey, key_name, allowed_ips, created_at,
                connected, last_handshake, rx_bytes, tx_bytes, endpoint,
                active, payer, client_id, billing_client_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                server_name,
                wg_pubkey,
                key_name,
                allowed_ips,
                created_at,
                1 if connected else 0,
                int(last_handshake or 0),
                int(rx_bytes or 0),
                int(tx_bytes or 0),
                endpoint,
                1 if active else 0,
                1 if payer is not None and payer else 1,
                client_id,
                billing_client_id if billing_client_id is not None else client_id,
            ),
        )

        await db.execute(
            """
            UPDATE client_keys
            SET key_name = COALESCE(?, key_name),
                allowed_ips = COALESCE(?, allowed_ips),
                created_at = COALESCE(created_at, ?),
                connected = ?,
                last_handshake = ?,
                rx_bytes = ?,
                tx_bytes = ?,
                endpoint = COALESCE(?, endpoint),
                active = ?
            WHERE server_name = ? AND wg_pubkey = ?
            """,
            (
                key_name,
                allowed_ips,
                created_at,
                1 if connected else 0,
                int(last_handshake or 0),
                int(rx_bytes or 0),
                int(tx_bytes or 0),
                endpoint,
                1 if active else 0,
                server_name,
                wg_pubkey,
            ),
        )

        key_id = await _get_key_id(db, server_name, wg_pubkey)
        if key_id is None:
            await db.commit()
            raise RuntimeError("Failed to read key id after upsert")

        if client_id is not None:
            await _ensure_key_access(db, key_id, client_id)
            await db.execute(
                "UPDATE client_keys SET client_id = COALESCE(client_id, ?) WHERE id = ?",
                (client_id, key_id),
            )

        if payer is not None:
            await db.execute("UPDATE client_keys SET payer = ? WHERE id = ?", (1 if payer else 0, key_id))

        if billing_client_id is not None:
            await _ensure_key_access(db, key_id, billing_client_id)
            await db.execute("UPDATE client_keys SET billing_client_id = ?, client_id = ? WHERE id = ?", (billing_client_id, billing_client_id, key_id))
        elif client_id is not None:
            await db.execute(
                "UPDATE client_keys SET billing_client_id = COALESCE(billing_client_id, ?) WHERE id = ?",
                (client_id, key_id),
            )

        await db.commit()
        return key_id


async def sync_server_keys(server_name: str, peers: List[Dict]) -> Dict[str, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT wg_pubkey FROM client_keys WHERE server_name = ?",
            (server_name,),
        ) as cur:
            existing = {r["wg_pubkey"] for r in await cur.fetchall()}

        incoming = set()
        added = 0

        for p in peers:
            pubkey = p.get("pubkey")
            if not pubkey:
                continue
            incoming.add(pubkey)
            if pubkey not in existing:
                added += 1

            await db.execute(
                """
                INSERT OR IGNORE INTO client_keys (
                    server_name, wg_pubkey, key_name, allowed_ips, created_at,
                    connected, last_handshake, rx_bytes, tx_bytes, endpoint,
                    active, payer
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1)
                """,
                (
                    server_name,
                    pubkey,
                    p.get("name"),
                    p.get("ip"),
                    p.get("created"),
                    1 if p.get("connected") else 0,
                    int(p.get("last_handshake") or 0),
                    int(p.get("rx_bytes") or 0),
                    int(p.get("tx_bytes") or 0),
                    p.get("endpoint"),
                ),
            )

            await db.execute(
                """
                UPDATE client_keys
                SET key_name = COALESCE(?, key_name),
                    allowed_ips = COALESCE(?, allowed_ips),
                    created_at = COALESCE(created_at, ?),
                    connected = ?,
                    last_handshake = ?,
                    rx_bytes = ?,
                    tx_bytes = ?,
                    endpoint = COALESCE(?, endpoint),
                    active = 1
                WHERE server_name = ? AND wg_pubkey = ?
                """,
                (
                    p.get("name"),
                    p.get("ip"),
                    p.get("created"),
                    1 if p.get("connected") else 0,
                    int(p.get("last_handshake") or 0),
                    int(p.get("rx_bytes") or 0),
                    int(p.get("tx_bytes") or 0),
                    p.get("endpoint"),
                    server_name,
                    pubkey,
                ),
            )

        if incoming:
            placeholders = ",".join("?" for _ in incoming)
            params = [server_name, *incoming]
            await db.execute(
                f"UPDATE client_keys SET active = 0, connected = 0 WHERE server_name = ? AND wg_pubkey NOT IN ({placeholders})",
                params,
            )
            async with db.execute(
                f"SELECT COUNT(*) FROM client_keys WHERE server_name = ? AND wg_pubkey NOT IN ({placeholders})",
                params,
            ) as cur:
                inactive = int((await cur.fetchone())[0])
        else:
            await db.execute(
                "UPDATE client_keys SET active = 0, connected = 0 WHERE server_name = ?",
                (server_name,),
            )
            async with db.execute(
                "SELECT COUNT(*) FROM client_keys WHERE server_name = ?",
                (server_name,),
            ) as cur:
                inactive = int((await cur.fetchone())[0])

        await db.commit()

    return {"total": len(incoming), "added": added, "inactive": inactive}


def _key_select_sql(where_clause: str) -> str:
    return f"""
        SELECT
            k.*,
            b.name AS billing_client_name,
            (SELECT COUNT(*) FROM key_access ka2 WHERE ka2.key_id = k.id) AS linked_clients
        FROM client_keys k
        LEFT JOIN clients b ON b.id = k.billing_client_id
        {where_clause}
        ORDER BY k.active DESC, k.key_name COLLATE NOCASE, k.id
    """


async def get_client_keys(client_id: int) -> List[ClientKey]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = _key_select_sql("JOIN key_access ka ON ka.key_id = k.id WHERE ka.client_id = ?")
        async with db.execute(query, (client_id,)) as cur:
            rows = await cur.fetchall()
    return [_row_to_client_key(r) for r in rows]


async def get_client_server_names(client_id: int, active_only: bool = True) -> List[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        active_clause = "AND k.active = 1" if active_only else ""
        query = f"""
            SELECT DISTINCT k.server_name
            FROM key_access ka
            JOIN client_keys k ON k.id = ka.key_id
            WHERE ka.client_id = ? {active_clause}
            ORDER BY k.server_name COLLATE NOCASE
        """
        async with db.execute(query, (client_id,)) as cur:
            rows = await cur.fetchall()
            servers = [r["server_name"] for r in rows if r["server_name"]]

        if servers:
            return servers

        async with db.execute("SELECT server_name FROM clients WHERE id = ?", (client_id,)) as cur:
            row = await cur.fetchone()
            if row and row["server_name"]:
                return [row["server_name"]]
    return []


async def get_unlinked_keys() -> List[ClientKey]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = _key_select_sql("WHERE NOT EXISTS (SELECT 1 FROM key_access ka WHERE ka.key_id = k.id)")
        async with db.execute(query) as cur:
            rows = await cur.fetchall()
    return [_row_to_client_key(r) for r in rows]


async def get_linkable_keys(client_id: int) -> List[ClientKey]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = _key_select_sql(
            "WHERE NOT EXISTS (SELECT 1 FROM key_access ka WHERE ka.key_id = k.id AND ka.client_id = ?)"
        )
        async with db.execute(query, (client_id,)) as cur:
            rows = await cur.fetchall()
    return [_row_to_client_key(r) for r in rows]


async def get_key_by_id(key_id: int) -> Optional[ClientKey]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = _key_select_sql("WHERE k.id = ?")
        async with db.execute(query, (key_id,)) as cur:
            row = await cur.fetchone()
    return _row_to_client_key(row) if row else None


async def get_key_by_server_pubkey(server_name: str, wg_pubkey: str) -> Optional[ClientKey]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = _key_select_sql("WHERE k.server_name = ? AND k.wg_pubkey = ?")
        async with db.execute(query, (server_name, wg_pubkey)) as cur:
            row = await cur.fetchone()
    return _row_to_client_key(row) if row else None


async def get_key_access_clients(key_id: int) -> List[KeyAccessClient]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT c.id AS client_id, c.name, c.username
            FROM key_access ka
            JOIN clients c ON c.id = ka.client_id
            WHERE ka.key_id = ?
            ORDER BY c.name
            """,
            (key_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [KeyAccessClient(client_id=r["client_id"], name=r["name"], username=r["username"]) for r in rows]


async def is_key_linked_to_client(key_id: int, client_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM key_access WHERE key_id = ? AND client_id = ? LIMIT 1",
            (key_id, client_id),
        ) as cur:
            row = await cur.fetchone()
    return row is not None


async def is_key_linked_any(key_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM key_access WHERE key_id = ? LIMIT 1", (key_id,)) as cur:
            row = await cur.fetchone()
    return row is not None


async def assign_key_to_client(key_id: int, client_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_key_access(db, key_id, client_id)
        await db.execute(
            "UPDATE client_keys SET billing_client_id = COALESCE(billing_client_id, ?), client_id = COALESCE(client_id, ?) WHERE id = ?",
            (client_id, client_id, key_id),
        )
        await db.commit()


async def unassign_key(key_id: int, client_id: Optional[int] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if client_id is None:
            await db.execute("DELETE FROM key_access WHERE key_id = ?", (key_id,))
            await db.execute("UPDATE client_keys SET billing_client_id = NULL, client_id = NULL WHERE id = ?", (key_id,))
            await db.commit()
            return

        await db.execute("DELETE FROM key_access WHERE key_id = ? AND client_id = ?", (key_id, client_id))
        await db.execute(
            """
            UPDATE client_keys
            SET billing_client_id = (
                SELECT ka.client_id FROM key_access ka WHERE ka.key_id = client_keys.id LIMIT 1
            )
            WHERE id = ? AND billing_client_id = ?
            """,
            (key_id, client_id),
        )
        await db.execute("UPDATE client_keys SET client_id = billing_client_id WHERE id = ?", (key_id,))
        await db.commit()


async def delete_key_record(key_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("DELETE FROM key_access WHERE key_id = ?", (key_id,))
        await db.execute("DELETE FROM client_keys WHERE id = ?", (key_id,))
        await db.commit()


async def set_key_payer(key_id: int, payer: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE client_keys SET payer = ? WHERE id = ?", (1 if payer else 0, key_id))
        await db.commit()


async def set_key_paused(key_id: int, paused: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE client_keys SET paused = ? WHERE id = ?", (1 if paused else 0, key_id))
        await db.commit()


async def set_key_billing_client(key_id: int, client_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_key_access(db, key_id, client_id)
        await db.execute(
            "UPDATE client_keys SET billing_client_id = ?, client_id = ? WHERE id = ?",
            (client_id, client_id, key_id),
        )
        await db.commit()


async def delete_client_record(client_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE client_keys
            SET billing_client_id = (
                SELECT ka.client_id
                FROM key_access ka
                WHERE ka.key_id = client_keys.id AND ka.client_id != ?
                LIMIT 1
            )
            WHERE billing_client_id = ?
            """,
            (client_id, client_id),
        )
        await db.execute("DELETE FROM key_access WHERE client_id = ?", (client_id,))
        await db.execute("UPDATE client_keys SET client_id = billing_client_id")
        await db.execute("DELETE FROM clients WHERE id = ?", (client_id,))
        await db.commit()


async def get_payment_waiting_clients() -> List[Client]:
    return await _fetch_clients("WHERE c.payment_status = 'waiting_confirm'")


async def get_global_key_stats() -> Dict[str, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                COUNT(*) AS total_keys,
                COALESCE(SUM(CASE WHEN payer = 1 THEN 1 ELSE 0 END), 0) AS payable_keys,
                COALESCE(SUM(CASE WHEN paused = 1 THEN 1 ELSE 0 END), 0) AS paused_keys
            FROM client_keys
            WHERE active = 1
            """
        ) as cur:
            row = await cur.fetchone()
    return {
        "total_keys": int(row["total_keys"] or 0),
        "payable_keys": int(row["payable_keys"] or 0),
        "paused_keys": int(row["paused_keys"] or 0),
    }


def _row_to_client(row) -> Client:
    key_count = int(row["key_count"] or 0) if "key_count" in row.keys() else 0
    payable_key_count = int(row["payable_key_count"] or 0) if "payable_key_count" in row.keys() else 0
    nonpayable = max(0, key_count - payable_key_count)

    devices = _calc_devices(key_count, int(row["devices"] or 0))
    monthly_fee = _calc_monthly_fee(key_count, payable_key_count, float(row["monthly_fee"] or 0))

    return Client(
        id=row["id"],
        telegram_id=row["telegram_id"],
        name=row["name"],
        username=row["username"],
        server_name=row["server_name"],
        devices=devices,
        monthly_fee=monthly_fee,
        active=bool(row["active"]),
        payment_status=row["payment_status"],
        payment_date=row["payment_date"],
        reminder_day=row["reminder_day"],
        disconnect_date=row["disconnect_date"],
        wg_pubkey=row["wg_pubkey"],
        wg_peer_id=row["wg_peer_id"],
        paid_until=row["paid_until"] if "paid_until" in row.keys() else None,
        key_count=key_count,
        payable_key_count=payable_key_count,
        nonpayable_key_count=nonpayable,
    )


def _row_to_client_key(row) -> ClientKey:
    return ClientKey(
        id=row["id"],
        server_name=row["server_name"],
        wg_pubkey=row["wg_pubkey"],
        key_name=row["key_name"],
        allowed_ips=row["allowed_ips"],
        created_at=row["created_at"],
        connected=bool(row["connected"]),
        last_handshake=int(row["last_handshake"] or 0),
        rx_bytes=int(row["rx_bytes"] or 0),
        tx_bytes=int(row["tx_bytes"] or 0),
        endpoint=row["endpoint"],
        active=bool(row["active"]),
        payer=bool(row["payer"]),
        paused=bool(row["paused"]) if "paused" in row.keys() else False,
        client_id=row["client_id"] if "client_id" in row.keys() else None,
        billing_client_id=row["billing_client_id"] if "billing_client_id" in row.keys() else None,
        billing_client_name=row["billing_client_name"] if "billing_client_name" in row.keys() else None,
        linked_clients=int(row["linked_clients"] or 0) if "linked_clients" in row.keys() else 0,
    )
