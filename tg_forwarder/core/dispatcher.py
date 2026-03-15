from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from telethon.errors import FloodWaitError

logger = logging.getLogger(__name__)


class TokenBucket:
    def __init__(self, rate: float, capacity: int) -> None:
        self._rate = rate
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated_at
                self._updated_at = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                needed = tokens - self._tokens
                wait_for = needed / self._rate if self._rate > 0 else 1.0
            await asyncio.sleep(max(wait_for, 0.01))


@dataclass(order=True, slots=True)
class DispatchTask:
    priority: int
    created_at: float
    task_id: int = field(compare=False)
    run: Callable[[], Awaitable[None]] = field(compare=False)
    source_chat: int = field(compare=False)
    target_chat: int = field(compare=False)
    lineage: tuple[int, ...] = field(compare=False, default_factory=tuple)
    max_attempts: int = field(compare=False, default=5)
    attempts: int = field(compare=False, default=0)
    future: asyncio.Future[None] = field(compare=False, default=None)  # type: ignore[assignment]


class DispatchScheduler:
    def __init__(
        self,
        *,
        workers: int = 2,
        global_rate: float = 20.0,
        global_capacity: int = 20,
        per_chat_rate: float = 1.0,
        per_chat_capacity: int = 3,
    ) -> None:
        self._queue: asyncio.PriorityQueue[DispatchTask] = asyncio.PriorityQueue()
        self._workers = workers
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._global_bucket = TokenBucket(rate=global_rate, capacity=global_capacity)
        self._per_chat_rate = per_chat_rate
        self._per_chat_capacity = per_chat_capacity
        self._per_chat_buckets: dict[int, TokenBucket] = defaultdict(
            self._build_chat_bucket
        )
        self._task_seq = 0
        self._stopping = False

    def _build_chat_bucket(self) -> TokenBucket:
        return TokenBucket(rate=self._per_chat_rate, capacity=self._per_chat_capacity)

    async def start(self) -> None:
        if self._worker_tasks:
            return
        self._stopping = False
        for idx in range(self._workers):
            self._worker_tasks.append(asyncio.create_task(self._worker_loop(idx)))

    async def stop(self) -> None:
        self._stopping = True
        for worker in self._worker_tasks:
            worker.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()
        await self._drain_pending("dispatcher stopped")

    async def _drain_pending(self, reason: str) -> None:
        while not self._queue.empty():
            task = await self._queue.get()
            if not task.future.done():
                task.future.set_exception(RuntimeError(reason))
            self._queue.task_done()

    def submit(
        self,
        run: Callable[[], Awaitable[None]],
        *,
        source_chat: int,
        target_chat: int,
        lineage: tuple[int, ...] = (),
        priority: int = 100,
        max_attempts: int = 5,
    ) -> asyncio.Future[None]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()

        if target_chat in lineage or source_chat == target_chat:
            future.set_exception(
                ValueError(f"loop guard: source={source_chat} target={target_chat}")
            )
            return future

        self._task_seq += 1
        task = DispatchTask(
            priority=priority,
            created_at=time.monotonic(),
            task_id=self._task_seq,
            run=run,
            source_chat=source_chat,
            target_chat=target_chat,
            lineage=lineage,
            max_attempts=max_attempts,
            future=future,
        )
        self._queue.put_nowait(task)
        return future

    async def _worker_loop(self, worker_id: int) -> None:
        while True:
            task = await self._queue.get()
            try:
                if task.future.cancelled():
                    continue
                await self._global_bucket.acquire()
                await self._per_chat_buckets[task.target_chat].acquire()
                await task.run()
                if not task.future.done():
                    task.future.set_result(None)
            except FloodWaitError as exc:
                self._handle_retry(task, exc, float(exc.seconds))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._handle_retry(task, exc, 1.0)
            finally:
                self._queue.task_done()

    def _handle_retry(
        self, task: DispatchTask, exc: Exception, base_delay: float
    ) -> None:
        if task.attempts + 1 >= task.max_attempts:
            if not task.future.done():
                task.future.set_exception(exc)
            logger.error(
                "dispatch exhausted task=%s target=%s attempts=%s",
                task.task_id, task.target_chat, task.attempts + 1,
            )
        else:
            task.attempts += 1
            delay = self._backoff(base_delay, task.attempts)
            logger.warning(
                "retrying task=%s target=%s attempt=%s delay=%.1fs",
                task.task_id, task.target_chat, task.attempts, delay,
            )
            asyncio.create_task(self._requeue_after(task, delay))

    async def _requeue_after(self, task: DispatchTask, delay: float) -> None:
        await asyncio.sleep(delay)
        if self._stopping or task.future.done():
            return
        self._queue.put_nowait(task)

    @staticmethod
    def _backoff(base: float, attempt: int) -> float:
        exp = min(2**attempt, 32)
        jitter = random.uniform(0.0, 1.5)
        return min(300.0, base * exp + jitter)
