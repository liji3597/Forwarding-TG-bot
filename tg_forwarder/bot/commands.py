from __future__ import annotations

from collections.abc import Awaitable, Callable

from telethon import events

from tg_forwarder.bot.admin import AdminCommands
from tg_forwarder.core.engine import ForwardingEngine
from tg_forwarder.features.preview import PreviewHandler
from tg_forwarder.features.self_forward import SelfForwardHandler


class BotCommandRouter:
    def __init__(
        self, engine: ForwardingEngine, self_forward: SelfForwardHandler
    ) -> None:
        self._engine = engine
        self._self_forward = self_forward
        self._admin = AdminCommands(engine=engine, preview=PreviewHandler(engine))
        self._handlers: list[
            Callable[[events.NewMessage.Event], Awaitable[None]]
        ] = []
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        if self._engine.bot_client is None:
            raise RuntimeError("bot client not initialized")

        await self._self_forward.start()
        self._register(self._handle_start, r"^/start(?:@\w+)?(?:\s|$)")
        self._register(self._handle_help, r"^/help(?:@\w+)?(?:\s|$)")
        await self._admin.start()
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        if self._engine.bot_client is not None:
            for handler in self._handlers:
                self._engine.bot_client.remove_event_handler(handler)
        self._handlers.clear()
        await self._admin.stop()
        await self._self_forward.close()
        self._started = False

    def _register(
        self,
        handler: Callable[[events.NewMessage.Event], Awaitable[None]],
        pattern: str,
    ) -> None:
        bot = self._engine.bot_client
        if bot is None:
            raise RuntimeError("bot client not initialized")
        bot.add_event_handler(handler, events.NewMessage(pattern=pattern))
        self._handlers.append(handler)

    async def _ensure_admin(self, event: events.NewMessage.Event) -> bool:
        config = self._engine.config
        admin_id = config.monitoring.admin_id if config else None
        if admin_id is None or event.sender_id != admin_id:
            await event.reply("⛔ 无权限")
            return False
        return True

    async def _handle_start(self, event: events.NewMessage.Event) -> None:
        if not await self._ensure_admin(event):
            return
        await event.reply(
            "🤖 TG Forwarder 已就绪\n使用 /help 查看可用命令"
        )

    async def _handle_help(self, event: events.NewMessage.Event) -> None:
        if not await self._ensure_admin(event):
            return
        await event.reply(
            "📚 可用命令\n"
            "/status - 查看状态\n"
            "/reload - 重载配置\n"
            "/preview <link> [job] - 预览处理结果\n"
            "/pause [job] - 暂停任务\n"
            "/resume [job] - 恢复任务\n"
            "/stats - 转发统计"
        )
