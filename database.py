import aiosqlite
import asyncio
from datetime import datetime, date
from typing import Optional, List
from dataclasses import dataclass

DB_PATH = "vpn_bot.db"

@dataclass
class Client:
    id: int
    telegram_id: int
    name: str
    username: Optional[str]
    server_name: str
    devices: int
    monthly_fee: float  # в рублях/валюте
    active: bool
    payment_status: str  # "pending" | "waiting_confirm" | "paid" | "overdue"
    payment_date: Optional[str]  # дата последней оплаты
    reminder_day: int   # сколько дней прошло с 1-го числа (счётчик напоминаний)
    disconnect_date: Optional[str]  # запланированная дата отключения
    wg_pubkey: Optional[str]  # публичный ключ WireGuard клиента
    wg_peer_id: Optional[str] # идентификатор peer на сервере

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
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
                wg_peer_id TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payment_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER,
                action TEXT,
                amount REAL,
                timestamp TEXT,
                note TEXT
            )
        """)
        await db.commit()

async def add_client(telegram_id: int, name: str, username: str,
                     server_name: str, devices: int, monthly_fee: float,
                     wg_pubkey: str = None, wg_peer_id: str = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO clients (telegram_id, name, username, server_name, devices, monthly_fee,
                                 payment_status, wg_pubkey, wg_peer_id)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (telegram_id, name, username, server_name, devices, monthly_fee, wg_pubkey, wg_peer_id))
        await db.commit()
        return cur.lastrowid

async def get_client_by_tg(telegram_id: int) -> Optional[Client]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM clients WHERE telegram_id = ?", (telegram_id,)) as cur:
            row = await cur.fetchone()
            return _row_to_client(row) if row else None

async def get_client_by_id(client_id: int) -> Optional[Client]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM clients WHERE id = ?", (client_id,)) as cur:
            row = await cur.fetchone()
            return _row_to_client(row) if row else None

async def get_all_clients() -> List[Client]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM clients ORDER BY name") as cur:
            rows = await cur.fetchall()
            return [_row_to_client(r) for r in rows]

async def get_active_clients() -> List[Client]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM clients WHERE active = 1") as cur:
            rows = await cur.fetchall()
            return [_row_to_client(r) for r in rows]

async def update_payment_status(client_id: int, status: str, payment_date: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if payment_date:
            await db.execute(
                "UPDATE clients SET payment_status = ?, payment_date = ?, reminder_day = 0, disconnect_date = NULL WHERE id = ?",
                (status, payment_date, client_id)
            )
        else:
            await db.execute(
                "UPDATE clients SET payment_status = ? WHERE id = ?",
                (status, client_id)
            )
        await db.commit()

async def increment_reminder_day(client_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE clients SET reminder_day = reminder_day + 1 WHERE id = ?",
            (client_id,)
        )
        await db.commit()

async def set_disconnect_date(client_id: int, disconnect_date: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE clients SET disconnect_date = ? WHERE id = ?",
            (disconnect_date, client_id)
        )
        await db.commit()

async def set_client_active(client_id: int, active: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE clients SET active = ? WHERE id = ?",
            (1 if active else 0, client_id)
        )
        await db.commit()

async def reset_monthly_payments():
    """Вызывается 1-го числа — сбрасывает статусы для нового цикла"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE clients 
            SET payment_status = 'pending', reminder_day = 0, disconnect_date = NULL
            WHERE active = 1
        """)
        await db.commit()

async def log_payment(client_id: int, action: str, amount: float = 0, note: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO payment_log (client_id, action, amount, timestamp, note)
            VALUES (?, ?, ?, ?, ?)
        """, (client_id, action, amount, datetime.now().isoformat(), note))
        await db.commit()

async def update_client_fields(client_id: int, **kwargs):
    """Универсальное обновление полей клиента"""
    if not kwargs:
        return
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [client_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE clients SET {fields} WHERE id = ?", values)
        await db.commit()

def _row_to_client(row) -> Client:
    return Client(
        id=row["id"],
        telegram_id=row["telegram_id"],
        name=row["name"],
        username=row["username"],
        server_name=row["server_name"],
        devices=row["devices"],
        monthly_fee=row["monthly_fee"],
        active=bool(row["active"]),
        payment_status=row["payment_status"],
        payment_date=row["payment_date"],
        reminder_day=row["reminder_day"],
        disconnect_date=row["disconnect_date"],
        wg_pubkey=row["wg_pubkey"],
        wg_peer_id=row["wg_peer_id"],
    )
