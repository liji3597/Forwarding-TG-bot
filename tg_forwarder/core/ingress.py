from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from telethon import TelegramClient, events, utils
from telethon.tl import types

from tg_forwarder.config.schema import AppConfig, ForwardJob


@dataclass(slots=True)
class IngressMessage:
    source_chat_id: int
    message_id: int
    text: str
    raw_message: types.Message
    mode: Literal["userbot", "bot"]
    job: ForwardJob
    lineage: tuple[int, ...]


class IngressAdapter:
    def __init__(
        self,
        *,
        config: AppConfig,
        user_client: TelegramClient | None,
        bot_client: TelegramClient | None,
        callback: Callable[[IngressMessage], Awaitable[None]],
    ) -> None:
        self._config = config
        self._user_client = user_client
        self._bot_client = bot_client
        self._callback = callback
        self._jobs_index: dict[tuple[str, int], list[ForwardJob]] = defaultdict(list)
        self._handlers: list[tuple[TelegramClient, Callable[..., Awaitable[None]]]] = []

    async def start(self) -> None:
        self._build_jobs_index()
        await self._register_handlers()

    async def stop(self) -> None:
        for client, handler in self._handlers:
            client.remove_event_handler(handler)
        self._handlers.clear()

    def _build_jobs_index(self) -> None:
        self._jobs_index.clear()
        for job in self._config.jobs:
            self._jobs_index[(job.mode, job.source)].append(job)

    async def _register_handlers(self) -> None:
        user_sources = sorted(
            {job.source for job in self._config.jobs if job.mode == "userbot"}
        )
        bot_sources = sorted(
            {job.source for job in self._config.jobs if job.mode == "bot"}
        )

        if self._user_client and user_sources:
            handler = self._build_handler("userbot")
            self._user_client.add_event_handler(
                handler, events.NewMessage(chats=user_sources)
            )
            self._handlers.append((self._user_client, handler))

        if self._bot_client and bot_sources:
            handler = self._build_handler("bot")
            self._bot_client.add_event_handler(
                handler, events.NewMessage(chats=bot_sources)
            )
            self._handlers.append((self._bot_client, handler))

    def _build_handler(
        self, mode: Literal["userbot", "bot"]
    ) -> Callable[[events.NewMessage.Event], Awaitable[None]]:
        async def _handle_event(event: events.NewMessage.Event) -> None:
            message = event.message
            if not isinstance(message, types.Message):
                return
            if message.peer_id is None:
                return

            source_chat_id = utils.get_peer_id(message.peer_id)
            jobs = self._jobs_index.get((mode, source_chat_id), [])
            if not jobs:
                return

            normalized_text = message.message or ""
            for job in jobs:
                ingress_msg = IngressMessage(
                    source_chat_id=source_chat_id,
                    message_id=message.id,
                    text=normalized_text,
                    raw_message=message,
                    mode=mode,
                    job=job,
                    lineage=(source_chat_id,),
                )
                await self._callback(ingress_msg)

        return _handle_event
