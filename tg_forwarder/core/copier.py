from __future__ import annotations

from collections.abc import Sequence

from telethon import TelegramClient
from telethon.tl import types


class MessageCopier:
    async def copy_message(
        self,
        client: TelegramClient,
        msg: types.Message,
        target: int | str,
        *,
        modified_text: str | None = None,
    ) -> types.Message:
        if modified_text is not None and msg.media is None:
            return await client.send_message(
                entity=target,
                message=modified_text,
                formatting_entities=msg.entities,
                link_preview=bool(msg.web_preview),
            )
        if modified_text is not None and msg.media is not None:
            return await client.send_file(
                entity=target,
                file=msg.media,
                caption=modified_text,
                formatting_entities=msg.entities,
            )
        return await client.send_message(entity=target, message=msg)

    async def copy_messages(
        self,
        client: TelegramClient,
        messages: Sequence[types.Message],
        target: int | str,
    ) -> list[types.Message]:
        sent: list[types.Message] = []
        for message in sorted(messages, key=lambda m: m.id):
            sent.append(await self.copy_message(client, message, target))
        return sent
