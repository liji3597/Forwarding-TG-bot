from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from telethon import events
from telethon.errors import ChatForwardsRestrictedError
from telethon.tl import types

from tg_forwarder.core.album_batcher import AlbumBatcher
from tg_forwarder.core.copier import MessageCopier
from tg_forwarder.core.engine import ForwardingEngine
from tg_forwarder.features.audit import AuditLogger
from tg_forwarder.plugins.protected_extractor import ProtectedExtractor
from tg_forwarder.plugins.watermark_remover import WatermarkRemover

logger = logging.getLogger(__name__)


class SelfForwardHandler:
    def __init__(self, engine: ForwardingEngine) -> None:
        self._engine = engine
        self._copier = MessageCopier()
        self._watermark = WatermarkRemover()
        self._audit = AuditLogger(engine)
        self._extractor: ProtectedExtractor | None = None
        self._album_batcher: AlbumBatcher | None = None
        self._handler: Callable[[events.NewMessage.Event], Awaitable[None]] | None = (
            None
        )
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        config = self._engine.config
        if config is None or not config.self_forward.enabled:
            return
        if self._engine.bot_client is None or self._engine.user_client is None:
            raise RuntimeError("self-forward requires both bot and userbot clients")

        self._extractor = (
            ProtectedExtractor(config.protected_extractor)
            if config.protected_extractor.enabled
            else None
        )
        self._album_batcher = AlbumBatcher(
            wait_seconds=config.self_forward.album_wait_seconds,
            callback=self._on_album_flush,
        )
        self._handler = self._handle_private_forward
        self._engine.bot_client.add_event_handler(
            self._handler,
            events.NewMessage(incoming=True, func=lambda e: e.is_private),
        )
        self._started = True

    async def close(self) -> None:
        if not self._started:
            return
        bot = self._engine.bot_client
        if bot is not None and self._handler is not None:
            bot.remove_event_handler(self._handler)
        if self._album_batcher is not None:
            await self._album_batcher.close()
        self._album_batcher = None
        self._handler = None
        self._started = False

    async def _handle_private_forward(self, event: events.NewMessage.Event) -> None:
        if self._album_batcher is None:
            return
        message = event.message
        if not isinstance(message, types.Message):
            return
        if message.fwd_from is None:
            return
        chat_id = event.chat_id
        if chat_id is None:
            return
        await self._album_batcher.add(message, chat_id)

    async def _on_album_flush(
        self, messages: list[types.Message], source_chat_id: int
    ) -> None:
        if not messages:
            return
        config = self._engine.config
        user = self._engine.user_client
        bot = self._engine.bot_client
        if config is None or user is None or bot is None:
            return

        target = self._resolve_target(config.self_forward.target)
        protected = False

        try:
            count = await self._try_copy(messages, target)
        except ChatForwardsRestrictedError:
            if self._extractor is None:
                await bot.send_message(source_chat_id, "❌ 内容受保护，无法保存")
                return
            try:
                count = await self._try_extract(messages, target)
                protected = True
            except Exception as exc:
                logger.exception("protected extraction failed")
                await self._audit.log_error("self_forward.extract", exc)
                await bot.send_message(source_chat_id, "❌ 保存失败")
                return
        except Exception as exc:
            logger.exception("self-forward copy failed")
            await self._audit.log_error("self_forward.copy", exc)
            await bot.send_message(source_chat_id, "❌ 保存失败")
            return

        mode = "受保护提取" if protected else "复制"
        label = self._target_label(config.self_forward.target)
        msg_type = self._message_type(messages)
        await bot.send_message(
            source_chat_id,
            f"✅ 已保存 {count} 条消息\n目标: {label}\n类型: {msg_type}\n方式: {mode}",
        )
        await self._audit.log_save(
            src=source_chat_id,
            msg_id=messages[-1].id,
            dst=config.self_forward.target,
            msg_type=msg_type,
            protected=protected,
        )

    async def _try_copy(self, messages: list[types.Message], target: int | str) -> int:
        config = self._engine.config
        user = self._engine.user_client
        if config is None or user is None:
            return 0

        ordered = sorted(messages, key=lambda m: m.id)

        if len(ordered) == 1:
            msg = ordered[0]
            if not config.self_forward.strip_attribution:
                await user.forward_messages(entity=target, messages=msg)
                return 1
            modified = self._modified_text(msg)
            await self._copier.copy_message(user, msg, target, modified_text=modified)
            return 1

        if not config.self_forward.strip_attribution:
            result = await user.forward_messages(entity=target, messages=ordered)
            return len(result) if isinstance(result, list) else 1

        sent = await self._copier.copy_messages(user, ordered, target)
        return len(sent)

    async def _try_extract(
        self, messages: list[types.Message], target: int | str
    ) -> int:
        user = self._engine.user_client
        if user is None or self._extractor is None:
            return 0
        if len(messages) == 1:
            await self._extractor.extract_and_send(user, messages[0], target)
            return 1
        sent = await self._extractor.extract_album(user, messages, target)
        return len(sent)

    def _modified_text(self, message: types.Message) -> str | None:
        config = self._engine.config
        if config is None:
            return None
        text = message.message
        if text is None:
            return None

        result = text
        if config.self_forward.apply_modifications:
            result = self._watermark.clean(result)
        if config.self_forward.append_source:
            name = self._source_name(message)
            result += config.self_forward.source_format.format(source_name=name)
        return None if result == text else result

    @staticmethod
    def _source_name(message: types.Message) -> str:
        fwd = message.fwd_from
        if fwd and fwd.from_name:
            return fwd.from_name
        if fwd and fwd.from_id:
            return str(fwd.from_id)
        return "未知来源"

    @staticmethod
    def _resolve_target(target: int | str) -> int | str:
        return "me" if target == "saved" else target

    @staticmethod
    def _target_label(target: int | str) -> str:
        if target == "saved":
            return "Saved Messages"
        return str(target)

    @staticmethod
    def _message_type(messages: list[types.Message]) -> str:
        if len(messages) > 1:
            return f"相册 ({len(messages)} 条)"
        msg = messages[0]
        if msg.photo:
            return "图片"
        if msg.video:
            return "视频"
        if msg.document:
            return "文件"
        if msg.sticker:
            return "贴纸"
        if msg.voice:
            return "语音"
        if msg.video_note:
            return "视频笔记"
        if msg.message:
            return "文字"
        return "其他"
