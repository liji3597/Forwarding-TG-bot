from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from telethon import TelegramClient, events

from tg_forwarder.core.engine import ForwardingEngine
from tg_forwarder.features.preview import PreviewHandler


class AdminCommands:
    def __init__(self, engine: ForwardingEngine, preview: PreviewHandler) -> None:
        self._engine = engine
        self._preview = preview
        self._handlers: list[Callable[[events.NewMessage.Event], Awaitable[None]]] = []
        self._started = False
        self._started_at = time.monotonic()

    async def start(self) -> None:
        if self._started:
            return
        if self._engine.bot_client is None:
            raise RuntimeError("bot client not initialized")

        self._register(self._handle_status, r"^/status(?:@\w+)?(?:\s|$)")
        self._register(self._handle_reload, r"^/reload(?:@\w+)?(?:\s|$)")
        self._register(self._handle_preview, r"^/preview(?:@\w+)?(?:\s|$)")
        self._register(self._handle_pause, r"^/pause(?:@\w+)?(?:\s|$)")
        self._register(self._handle_resume, r"^/resume(?:@\w+)?(?:\s|$)")
        self._register(self._handle_stats, r"^/stats(?:@\w+)?(?:\s|$)")
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        if self._engine.bot_client is not None:
            for handler in self._handlers:
                self._engine.bot_client.remove_event_handler(handler)
        self._handlers.clear()
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

    async def _handle_status(self, event: events.NewMessage.Event) -> None:
        if not await self._ensure_admin(event):
            return
        config = self._engine.config
        if config is None:
            await event.reply("❌ 配置未加载")
            return

        uptime = self._fmt_duration(int(time.monotonic() - self._started_at))
        uc = "✅" if self._connected(self._engine.user_client) else "❌"
        bc = "✅" if self._connected(self._engine.bot_client) else "❌"
        sf = "✅ 启用" if config.self_forward.enabled else "❌ 关闭"

        await event.reply(
            f"📊 系统状态\n"
            f"运行时长: {uptime}\n"
            f"任务数量: {len(config.jobs)}\n"
            f"自转发: {sf}\n"
            f"Userbot: {uc} | Bot: {bc}"
        )

    async def _handle_reload(self, event: events.NewMessage.Event) -> None:
        if not await self._ensure_admin(event):
            return
        try:
            await self._engine.reload_config()
            await event.reply("✅ 配置重载成功")
        except Exception as exc:
            await event.reply(f"❌ 重载失败: {exc}")

    async def _handle_preview(self, event: events.NewMessage.Event) -> None:
        if not await self._ensure_admin(event):
            return
        parts = (event.raw_text or "").strip().split(maxsplit=2)
        if len(parts) < 2:
            await event.reply("用法: /preview <msg_link> [job_name]")
            return

        link = parts[1]
        job_name = parts[2].strip() if len(parts) > 2 else self._default_job()
        if not job_name:
            await event.reply("❌ 无可用任务")
            return

        client = self._engine.user_client or self._engine.bot_client
        if client is None:
            await event.reply("❌ 客户端不可用")
            return

        try:
            result = await self._preview.preview(client, link, job_name)
        except Exception as exc:
            await event.reply(f"❌ 预览失败: {exc}")
            return

        status = "✅ 通过" if result.passed_filter else "❌ 拦截"
        rules = ", ".join(result.applied_rules) if result.applied_rules else "无"
        reason = result.filter_reason or "无"
        await event.reply(
            f"🔍 预览结果\n"
            f"任务: {job_name}\n"
            f"过滤: {status}\n"
            f"原因: {reason}\n"
            f"规则: {rules}\n\n"
            f"原文:\n{self._clip(result.original_text)}\n\n"
            f"修改后:\n{self._clip(result.modified_text)}"
        )

    async def _handle_pause(self, event: events.NewMessage.Event) -> None:
        if not await self._ensure_admin(event):
            return
        arg = self._cmd_arg(event)
        if arg is None:
            self._engine.pause()
            await event.reply("⏸️ 已暂停全部任务")
        elif self._job_exists(arg):
            self._engine.pause(arg)
            await event.reply(f"⏸️ 已暂停: {arg}")
        else:
            await event.reply(f"❌ 任务不存在: {arg}")

    async def _handle_resume(self, event: events.NewMessage.Event) -> None:
        if not await self._ensure_admin(event):
            return
        arg = self._cmd_arg(event)
        if arg is None:
            self._engine.resume()
            await event.reply("▶️ 已恢复全部任务")
        elif self._job_exists(arg):
            self._engine.resume(arg)
            await event.reply(f"▶️ 已恢复: {arg}")
        else:
            await event.reply(f"❌ 任务不存在: {arg}")

    async def _handle_stats(self, event: events.NewMessage.Event) -> None:
        if not await self._ensure_admin(event):
            return
        try:
            total = await self._engine.dedup.count_total()
            today = await self._engine.dedup.count_today()
            week = await self._engine.dedup.count_week()
        except Exception as exc:
            await event.reply(f"❌ 统计失败: {exc}")
            return
        await event.reply(
            f"📈 转发统计\n今日: {today}\n本周: {week}\n总计: {total}"
        )

    def _job_exists(self, name: str) -> bool:
        config = self._engine.config
        return config is not None and any(j.name == name for j in config.jobs)

    def _default_job(self) -> str | None:
        config = self._engine.config
        if config and config.jobs:
            return config.jobs[0].name
        return None

    @staticmethod
    def _cmd_arg(event: events.NewMessage.Event) -> str | None:
        parts = (event.raw_text or "").strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 and parts[1].strip() else None

    @staticmethod
    def _connected(client: TelegramClient | None) -> bool:
        return client is not None and bool(client.is_connected())

    @staticmethod
    def _clip(text: str, limit: int = 900) -> str:
        return text if len(text) <= limit else f"{text[:limit]}…"

    @staticmethod
    def _fmt_duration(seconds: int) -> str:
        h, rem = divmod(max(seconds, 0), 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
