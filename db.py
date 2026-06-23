import asyncio
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from config import DATABASE_NAME, DEFAULT_ORDER_TIMEOUT_MINUTES, DEFAULT_OTP_PRICE


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, db_name: str = DATABASE_NAME) -> None:
        self.db_name = db_name
        self.conn = sqlite3.connect(self.db_name, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    balance REAL NOT NULL DEFAULT 0,
                    banned INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    number TEXT NOT NULL,
                    service TEXT NOT NULL,
                    region TEXT NOT NULL,
                    status TEXT NOT NULL,
                    otp_message TEXT,
                    otp_code TEXT,
                    price REAL NOT NULL DEFAULT 0,
                    provider_otp_id TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    free_mode INTEGER NOT NULL DEFAULT 1,
                    otp_price REAL NOT NULL DEFAULT 0.25,
                    order_timeout_minutes INTEGER NOT NULL DEFAULT 10
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO settings (id, free_mode, otp_price, order_timeout_minutes)
                VALUES (1, 1, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (DEFAULT_OTP_PRICE, DEFAULT_ORDER_TIMEOUT_MINUTES),
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_user_status ON orders(user_id, status)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_number_status ON orders(number, status)"
            )
            self.conn.commit()

    def _fetchone(self, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(query, params).fetchone()
        return dict(row) if row else None

    def _fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def _execute(self, query: str, params: tuple[Any, ...] = ()) -> int:
        with self.lock:
            cursor = self.conn.execute(query, params)
            self.conn.commit()
        return cursor.rowcount

    async def close(self) -> None:
        await asyncio.to_thread(self.conn.close)

    async def ensure_user(self, user_id: int, username: str | None) -> None:
        await asyncio.to_thread(self._ensure_user_sync, user_id, username)

    def _ensure_user_sync(self, user_id: int, username: str | None) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO users (user_id, username, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = COALESCE(excluded.username, users.username)
                """,
                (user_id, username, utc_now_iso()),
            )
            self.conn.commit()

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        return await asyncio.to_thread(
            self._fetchone, "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )

    async def is_banned(self, user_id: int) -> bool:
        user = await self.get_user(user_id)
        return bool(user and user["banned"])

    async def set_banned(self, user_id: int, banned: bool) -> bool:
        updated = await asyncio.to_thread(
            self._execute,
            "UPDATE users SET banned = ? WHERE user_id = ?",
            (1 if banned else 0, user_id),
        )
        return updated > 0

    async def get_settings(self) -> dict[str, Any]:
        row = await asyncio.to_thread(self._fetchone, "SELECT * FROM settings WHERE id = 1")
        if not row:
            return {
                "free_mode": True,
                "otp_price": DEFAULT_OTP_PRICE,
                "order_timeout_minutes": DEFAULT_ORDER_TIMEOUT_MINUTES,
            }
        return {
            "free_mode": bool(row["free_mode"]),
            "otp_price": float(row["otp_price"]),
            "order_timeout_minutes": int(row["order_timeout_minutes"]),
        }

    async def set_free_mode(self, enabled: bool) -> None:
        await asyncio.to_thread(
            self._execute,
            "UPDATE settings SET free_mode = ? WHERE id = 1",
            (1 if enabled else 0,),
        )

    async def set_otp_price(self, price: float) -> None:
        await asyncio.to_thread(
            self._execute, "UPDATE settings SET otp_price = ? WHERE id = 1", (price,)
        )

    async def set_order_timeout(self, minutes: int) -> None:
        await asyncio.to_thread(
            self._execute,
            "UPDATE settings SET order_timeout_minutes = ? WHERE id = 1",
            (minutes,),
        )

    async def get_balance(self, user_id: int) -> float:
        user = await self.get_user(user_id)
        return float(user["balance"]) if user else 0.0

    async def add_balance(self, user_id: int, amount: float) -> float:
        await asyncio.to_thread(self._add_balance_sync, user_id, amount)
        return await self.get_balance(user_id)

    def _add_balance_sync(self, user_id: int, amount: float) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id)
            )
            self.conn.commit()

    async def deduct_balance(self, user_id: int, amount: float) -> bool:
        return await asyncio.to_thread(self._deduct_balance_sync, user_id, amount)

    def _deduct_balance_sync(self, user_id: int, amount: float) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if not row or float(row["balance"]) < amount:
                return False
            self.conn.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id)
            )
            self.conn.commit()
            return True

    async def count_active_orders(self, user_id: int) -> int:
        row = await asyncio.to_thread(
            self._fetchone,
            "SELECT COUNT(*) AS total FROM orders WHERE user_id = ? AND status IN ('waiting_otp', 'otp_locked')",
            (user_id,),
        )
        return int(row["total"]) if row else 0

    async def create_order(
        self,
        user_id: int,
        number: str,
        service: str,
        region: str,
        price: float,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._create_order_sync, user_id, number, service, region, price
        )

    def _create_order_sync(
        self, user_id: int, number: str, service: str, region: str, price: float
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self.lock:
            cursor = self.conn.execute(
                """
                INSERT INTO orders (
                    user_id, number, service, region, status, otp_message, otp_code,
                    price, provider_otp_id, created_at, completed_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'waiting_otp', NULL, NULL, ?, NULL, ?, NULL, ?)
                """,
                (user_id, number, service, region, price, now, now),
            )
            order_id = cursor.lastrowid
            self.conn.commit()
            row = self.conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
        return dict(row)

    async def get_order(self, order_id: int) -> dict[str, Any] | None:
        return await asyncio.to_thread(
            self._fetchone, "SELECT * FROM orders WHERE order_id = ?", (order_id,)
        )

    async def get_order_for_user(
        self, order_id: int, user_id: int, is_admin: bool = False
    ) -> dict[str, Any] | None:
        if is_admin:
            return await self.get_order(order_id)
        return await asyncio.to_thread(
            self._fetchone,
            "SELECT * FROM orders WHERE order_id = ? AND user_id = ?",
            (order_id, user_id),
        )

    async def list_user_orders(self, user_id: int, kind: str, limit: int = 10) -> list[dict[str, Any]]:
        if kind == "completed":
            query = """
                SELECT * FROM orders
                WHERE user_id = ? AND status IN ('completed', 'expired', 'cancelled')
                ORDER BY created_at DESC LIMIT ?
            """
        else:
            query = """
                SELECT * FROM orders
                WHERE user_id = ? AND status IN ('waiting_otp', 'otp_locked')
                ORDER BY created_at DESC LIMIT ?
            """
        return await asyncio.to_thread(self._fetchall, query, (user_id, limit))

    async def list_active_orders(self, limit: int = 20) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._fetchall,
            """
            SELECT * FROM orders
            WHERE status IN ('waiting_otp', 'otp_locked')
            ORDER BY created_at DESC LIMIT ?
            """,
            (limit,),
        )

    async def cancel_order(self, order_id: int) -> bool:
        updated = await asyncio.to_thread(self._cancel_order_sync, order_id)
        return updated

    def _cancel_order_sync(self, order_id: int) -> bool:
        now = utc_now_iso()
        with self.lock:
            cursor = self.conn.execute(
                """
                UPDATE orders
                SET status = 'cancelled', completed_at = ?, updated_at = ?
                WHERE order_id = ? AND status IN ('waiting_otp', 'otp_locked')
                """,
                (now, now, order_id),
            )
            self.conn.commit()
        return cursor.rowcount > 0

    async def expire_waiting_orders(self, timeout_minutes: int) -> int:
        return await asyncio.to_thread(self._expire_waiting_orders_sync, timeout_minutes)

    def _expire_waiting_orders_sync(self, timeout_minutes: int) -> int:
        threshold = (
            datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=timeout_minutes)
        ).isoformat()
        now = utc_now_iso()
        with self.lock:
            cursor = self.conn.execute(
                """
                UPDATE orders
                SET status = 'expired', completed_at = ?, updated_at = ?
                WHERE status = 'waiting_otp' AND created_at <= ?
                """,
                (now, now, threshold),
            )
            self.conn.commit()
        return cursor.rowcount

    async def attach_otp(
        self,
        number: str,
        provider_otp_id: str,
        message: str,
        code: str | None,
        received_at: str,
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(
            self._attach_otp_sync, number, provider_otp_id, message, code, received_at
        )

    def _attach_otp_sync(
        self, number: str, provider_otp_id: str, message: str, code: str | None, received_at: str
    ) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                """
                SELECT * FROM orders
                WHERE number = ? AND status = 'waiting_otp'
                ORDER BY created_at ASC LIMIT 1
                """,
                (number,),
            ).fetchone()
            if not row:
                return None
            status = "otp_locked" if float(row["price"]) > 0 else "completed"
            completed_at = None if status == "otp_locked" else received_at
            self.conn.execute(
                """
                UPDATE orders
                SET otp_message = ?, otp_code = ?, provider_otp_id = ?, status = ?,
                    completed_at = ?, updated_at = ?
                WHERE order_id = ?
                """,
                (
                    message,
                    code,
                    provider_otp_id,
                    status,
                    completed_at,
                    received_at,
                    row["order_id"],
                ),
            )
            self.conn.commit()
            updated = self.conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (row["order_id"],)
            ).fetchone()
        return dict(updated) if updated else None

    async def unlock_order(self, order_id: int, user_id: int) -> tuple[bool, str, dict[str, Any] | None]:
        return await asyncio.to_thread(self._unlock_order_sync, order_id, user_id)

    def _unlock_order_sync(self, order_id: int, user_id: int) -> tuple[bool, str, dict[str, Any] | None]:
        now = utc_now_iso()
        with self.lock:
            order = self.conn.execute(
                """
                SELECT * FROM orders
                WHERE order_id = ? AND user_id = ? AND status = 'otp_locked'
                """,
                (order_id, user_id),
            ).fetchone()
            if not order:
                return False, "Order is not available for unlocking.", None
            user = self.conn.execute(
                "SELECT balance FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if not user or float(user["balance"]) < float(order["price"]):
                return False, "Insufficient balance.", None
            self.conn.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ?",
                (order["price"], user_id),
            )
            self.conn.execute(
                """
                UPDATE orders
                SET status = 'completed', completed_at = ?, updated_at = ?
                WHERE order_id = ?
                """,
                (now, now, order_id),
            )
            self.conn.commit()
            updated = self.conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
        return True, "OTP unlocked successfully.", dict(updated) if updated else None

    async def user_stats(self, user_id: int) -> dict[str, Any]:
        return await asyncio.to_thread(self._user_stats_sync, user_id)

    def _user_stats_sync(self, user_id: int) -> dict[str, Any]:
        with self.lock:
            row = self.conn.execute(
                """
                SELECT
                    COUNT(*) AS total_orders,
                    SUM(CASE WHEN otp_message IS NOT NULL THEN 1 ELSE 0 END) AS successful_otps,
                    SUM(CASE WHEN status IN ('waiting_otp', 'otp_locked') THEN 1 ELSE 0 END) AS active_orders
                FROM orders
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        return {
            "total_orders": int(row["total_orders"] or 0),
            "successful_otps": int(row["successful_otps"] or 0),
            "active_orders": int(row["active_orders"] or 0),
        }

    async def admin_stats(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._admin_stats_sync)

    async def top_users_by_otps(self, limit: int = 5) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._top_users_by_otps_sync, limit)

    def _admin_stats_sync(self) -> dict[str, Any]:
        with self.lock:
            users = self.conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()
            orders = self.conn.execute("SELECT COUNT(*) AS total FROM orders").fetchone()
            revenue = self.conn.execute(
                "SELECT SUM(price) AS total FROM orders WHERE status = 'completed' AND price > 0"
            ).fetchone()
            active = self.conn.execute(
                "SELECT COUNT(*) AS total FROM orders WHERE status IN ('waiting_otp', 'otp_locked')"
            ).fetchone()
        return {
            "total_users": int(users["total"] or 0),
            "total_orders": int(orders["total"] or 0),
            "revenue": float(revenue["total"] or 0),
            "active_orders": int(active["total"] or 0),
        }

    def _top_users_by_otps_sync(self, limit: int) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT
                    u.user_id,
                    u.username,
                    COUNT(o.order_id) AS otp_count
                FROM users u
                JOIN orders o ON o.user_id = u.user_id
                WHERE o.otp_message IS NOT NULL
                GROUP BY u.user_id, u.username
                ORDER BY otp_count DESC, u.user_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
