from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeAlias

from telethon.tl import types

FlushCallback: TypeAlias = Callable[[list[types.Message], int], Awaitable[None]]
AlbumKey: TypeAlias = tuple[int, int]


class AlbumBatcher:
    def __init__(self, wait_seconds: float, callback: FlushCallback) -> None:
        self._wait_seconds = wait_seconds
        self._callback = callback
        self._buffers: dict[AlbumKey, list[types.Message]] = {}
        self._timers: dict[AlbumKey, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def add(self, message: types.Message, source_chat_id: int) -> None:
        if self._closed:
            return

        grouped_id = message.grouped_id
        if grouped_id is None:
            await self._callback([message], source_chat_id)
            return

        key: AlbumKey = (source_chat_id, int(grouped_id))
        async with self._lock:
            if self._closed:
                return
            batch = self._buffers.setdefault(key, [])
            batch.append(message)
            if key not in self._timers:
                self._timers[key] = asyncio.create_task(self._flush_after_wait(key))

    async def flush(self) -> None:
        async with self._lock:
            keys = list(self._buffers.keys())
        for key in keys:
            await self._flush_key(key)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
        await self.flush()
        async with self._lock:
            timers = list(self._timers.values())
            self._timers.clear()
        for timer in timers:
            timer.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)

    async def _flush_after_wait(self, key: AlbumKey) -> None:
        try:
            await asyncio.sleep(self._wait_seconds)
            await self._flush_key(key)
        except asyncio.CancelledError:
            return

    async def _flush_key(self, key: AlbumKey) -> None:
        messages = await self._pop_batch(key)
        if not messages:
            return
        messages.sort(key=lambda m: m.id)
        await self._callback(messages, key[0])

    async def _pop_batch(self, key: AlbumKey) -> list[types.Message]:
        async with self._lock:
            timer = self._timers.pop(key, None)
            current = asyncio.current_task()
            if timer is not None and timer is not current:
                timer.cancel()
            return self._buffers.pop(key, [])
