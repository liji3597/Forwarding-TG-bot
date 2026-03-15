from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Sequence
from io import BytesIO

from telethon import TelegramClient
from telethon.tl import types

from tg_forwarder.config.schema import ProtectedExtractorConfig

logger = logging.getLogger(__name__)


class ProtectedExtractor:
    def __init__(self, config: ProtectedExtractorConfig) -> None:
        self._config = config
        self._max_bytes = config.max_file_size_mb * 1024 * 1024
        self._rate_window: deque[float] = deque()
        self._rate_lock = asyncio.Lock()

    async def extract_and_send(
        self,
        client: TelegramClient,
        msg: types.Message,
        target: int | str,
    ) -> types.Message:
        await self._acquire_rate_slot()

        if msg.media is None:
            return await client.send_message(
                entity=target,
                message=msg.message or "",
                formatting_entities=msg.entities,
                link_preview=bool(msg.web_preview),
            )

        self._ensure_size_allowed(msg)
        payload = await self._download_to_buffer(client, msg)
        try:
            kwargs: dict[str, object] = {}
            caption = msg.message or ""
            if caption:
                kwargs["caption"] = caption
                kwargs["formatting_entities"] = msg.entities
            if msg.voice:
                kwargs["voice_note"] = True
            if msg.video_note:
                kwargs["video_note"] = True
            if msg.document and not (
                msg.video or msg.voice or msg.video_note or msg.sticker
            ):
                kwargs["force_document"] = True

            sent = await client.send_file(entity=target, file=payload, **kwargs)
            return sent[0] if isinstance(sent, list) else sent
        finally:
            payload.close()

    async def extract_album(
        self,
        client: TelegramClient,
        messages: Sequence[types.Message],
        target: int | str,
    ) -> list[types.Message]:
        sent: list[types.Message] = []
        for message in sorted(messages, key=lambda m: m.id):
            try:
                sent.append(
                    await self.extract_and_send(client, message, target)
                )
            except ValueError as exc:
                logger.warning("album item skipped msg=%s: %s", message.id, exc)
        if not sent:
            raise ValueError("all messages skipped by protected extractor")
        return sent

    async def _acquire_rate_slot(self) -> None:
        while True:
            async with self._rate_lock:
                now = time.monotonic()
                while self._rate_window and now - self._rate_window[0] >= 60:
                    self._rate_window.popleft()
                if len(self._rate_window) < self._config.rate_limit:
                    self._rate_window.append(now)
                    return
                sleep_for = max(0.01, 60 - (now - self._rate_window[0]))
            await asyncio.sleep(sleep_for)

    def _ensure_size_allowed(self, msg: types.Message) -> None:
        size = msg.file.size if msg.file else None
        if size is not None and size > self._max_bytes:
            raise ValueError(
                f"file too large: {size} bytes > {self._config.max_file_size_mb} MB"
            )

    async def _download_to_buffer(
        self, client: TelegramClient, msg: types.Message
    ) -> BytesIO:
        payload = BytesIO()
        payload.name = self._filename_for(msg)
        await client.download_media(msg, file=payload)
        actual_size = payload.getbuffer().nbytes
        if actual_size > self._max_bytes:
            payload.close()
            raise ValueError(
                f"downloaded file too large: {actual_size} bytes > "
                f"{self._config.max_file_size_mb} MB"
            )
        payload.seek(0)
        return payload

    def _filename_for(self, msg: types.Message) -> str:
        if msg.file and msg.file.name:
            return msg.file.name
        if msg.photo:
            return f"photo_{msg.id}.jpg"
        if msg.video:
            return f"video_{msg.id}.mp4"
        if msg.voice:
            return f"voice_{msg.id}.ogg"
        if msg.video_note:
            return f"vnote_{msg.id}.mp4"
        return f"media_{msg.id}.bin"
