from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite


class DedupStore:
    def __init__(self, db_path: Path, retention_days: int = 30) -> None:
        self._db_path = db_path
        self._retention_days = retention_days
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._write_count = 0

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS forwarded_messages (
                src_chat INTEGER NOT NULL,
                msg_id INTEGER NOT NULL,
                dst_chat INTEGER NOT NULL,
                ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (src_chat, msg_id, dst_chat)
            );
            """
        )
        await self._db.commit()
        await self.cleanup()

    async def is_duplicate(self, src_chat: int, msg_id: int, dst_chat: int) -> bool:
        if self._db is None:
            raise RuntimeError("DedupStore not initialized")
        async with self._lock:
            cursor = await self._db.execute(
                "SELECT 1 FROM forwarded_messages "
                "WHERE src_chat = ? AND msg_id = ? AND dst_chat = ? LIMIT 1;",
                (src_chat, msg_id, dst_chat),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return row is not None

    async def mark_sent(self, src_chat: int, msg_id: int, dst_chat: int) -> None:
        if self._db is None:
            raise RuntimeError("DedupStore not initialized")
        async with self._lock:
            await self._db.execute(
                "INSERT OR IGNORE INTO forwarded_messages (src_chat, msg_id, dst_chat) "
                "VALUES (?, ?, ?);",
                (src_chat, msg_id, dst_chat),
            )
            await self._db.commit()
            self._write_count += 1
            if self._write_count % 500 == 0:
                await self.cleanup()

    async def cleanup(self) -> None:
        if self._db is None:
            raise RuntimeError("DedupStore not initialized")
        async with self._lock:
            await self._db.execute(
                "DELETE FROM forwarded_messages WHERE ts < datetime('now', ?);",
                (f"-{self._retention_days} days",),
            )
            await self._db.commit()

    async def count_total(self) -> int:
        return await self._scalar("SELECT COUNT(*) FROM forwarded_messages;")

    async def count_today(self) -> int:
        return await self._scalar(
            "SELECT COUNT(*) FROM forwarded_messages "
            "WHERE ts >= datetime('now', 'start of day');"
        )

    async def count_week(self) -> int:
        return await self._scalar(
            "SELECT COUNT(*) FROM forwarded_messages "
            "WHERE ts >= datetime('now', '-7 days');"
        )

    async def _scalar(self, sql: str) -> int:
        if self._db is None:
            raise RuntimeError("DedupStore not initialized")
        async with self._lock:
            cursor = await self._db.execute(sql)
            row = await cursor.fetchone()
            await cursor.close()
        return int(row[0]) if row and row[0] is not None else 0

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
