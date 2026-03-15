from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from telethon import TelegramClient

from tg_forwarder.config.loader import ConfigLoadError, load_config
from tg_forwarder.config.schema import AppConfig, ForwardJob, ReplacementRule
from tg_forwarder.core.dedup import DedupStore
from tg_forwarder.core.dispatcher import DispatchScheduler
from tg_forwarder.core.ingress import IngressAdapter, IngressMessage

if TYPE_CHECKING:
    from tg_forwarder.bot.commands import BotCommandRouter

logger = logging.getLogger(__name__)


class ForwardingEngine:
    def __init__(self, *, config_path: Path) -> None:
        self._config_path = config_path
        self._config: AppConfig | None = None
        self._user_client: TelegramClient | None = None
        self._bot_client: TelegramClient | None = None
        self._dedup = DedupStore(db_path=Path("data/state.sqlite3"), retention_days=30)
        self._dispatcher = DispatchScheduler()
        self._ingress: IngressAdapter | None = None
        self._stop_event = asyncio.Event()
        self._running = False
        self._pause_all = False
        self._paused_jobs: set[str] = set()
        self._bot_router: BotCommandRouter | None = None

    @property
    def config(self) -> AppConfig | None:
        return self._config

    @property
    def user_client(self) -> TelegramClient | None:
        return self._user_client

    @property
    def bot_client(self) -> TelegramClient | None:
        return self._bot_client

    @property
    def dispatcher(self) -> DispatchScheduler:
        return self._dispatcher

    @property
    def dedup(self) -> DedupStore:
        return self._dedup

    def pause(self, job_name: str | None = None) -> None:
        if job_name is None:
            self._pause_all = True
            self._paused_jobs.clear()
        else:
            self._paused_jobs.add(job_name)

    def resume(self, job_name: str | None = None) -> None:
        if job_name is None:
            self._pause_all = False
            self._paused_jobs.clear()
        else:
            self._paused_jobs.discard(job_name)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        try:
            self._config = load_config(self._config_path)
            await self._init_clients(self._config)
            await self._dedup.init()
            await self._dispatcher.start()
            await self._start_ingress()
            await self._start_bot_router()
            logger.info("forwarding engine started")
            await self._stop_event.wait()
        except ConfigLoadError:
            logger.exception("failed to load config")
            raise
        finally:
            await self.stop()

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False

        await self._stop_bot_router()

        if self._ingress:
            await self._ingress.stop()
            self._ingress = None

        await self._dispatcher.stop()
        await self._dedup.close()

        if self._bot_client:
            await self._bot_client.disconnect()
            self._bot_client = None
        if self._user_client:
            await self._user_client.disconnect()
            self._user_client = None

        logger.info("forwarding engine stopped")

    async def reload_config(self) -> None:
        logger.info("reloading configuration from %s", self._config_path)
        new_config = load_config(self._config_path)
        self._config = new_config

        await self._stop_bot_router()
        if self._ingress:
            await self._ingress.stop()
            self._ingress = None
        await self._start_ingress()
        await self._start_bot_router()
        logger.info("configuration reloaded")

    def request_shutdown(self) -> None:
        self._stop_event.set()

    async def emit_audit(self, message: str) -> None:
        if self._config is None:
            return
        audit_channel = self._config.monitoring.audit_channel
        if audit_channel is None or self._user_client is None:
            return
        try:
            await self._user_client.send_message(audit_channel, message)
        except Exception:
            logger.exception("failed to send audit message")

    async def _init_clients(self, config: AppConfig) -> None:
        user = config.sessions.userbot
        self._user_client = TelegramClient("data/userbot", user.api_id, user.api_hash)
        await self._user_client.start(phone=user.phone)
        logger.info("userbot client connected")

        if config.sessions.bot:
            self._bot_client = TelegramClient(
                "data/bot", user.api_id, user.api_hash
            )
            await self._bot_client.start(bot_token=config.sessions.bot.token)
            logger.info("bot client connected")

    async def _start_ingress(self) -> None:
        if self._config is None:
            raise RuntimeError("config not loaded")
        self._ingress = IngressAdapter(
            config=self._config,
            user_client=self._user_client,
            bot_client=self._bot_client,
            callback=self._on_ingress_message,
        )
        await self._ingress.start()

    async def _start_bot_router(self) -> None:
        if self._config is None or self._bot_client is None:
            return
        if self._bot_router is not None:
            return

        from tg_forwarder.bot.commands import BotCommandRouter
        from tg_forwarder.features.self_forward import SelfForwardHandler

        router = BotCommandRouter(
            engine=self,
            self_forward=SelfForwardHandler(self),
        )
        await router.start()
        self._bot_router = router

    async def _stop_bot_router(self) -> None:
        if self._bot_router is None:
            return
        await self._bot_router.stop()
        self._bot_router = None

    async def _on_ingress_message(self, msg: IngressMessage) -> None:
        if self._config is None:
            return

        job = msg.job
        if self._pause_all or job.name in self._paused_jobs:
            logger.debug("paused job=%s msg=%s", job.name, msg.message_id)
            return

        if await self._dedup.is_duplicate(msg.source_chat_id, msg.message_id, job.target):
            logger.debug(
                "duplicate skipped src=%s msg=%s dst=%s",
                msg.source_chat_id, msg.message_id, job.target,
            )
            return

        if not self._passes_filters(msg.text, job):
            logger.debug("filtered job=%s msg=%s", job.name, msg.message_id)
            return

        processed_text = self._apply_modifications(msg.text, job)

        async def _send() -> None:
            client = self._get_client_for_job(job)
            await self._copy_to_target(client, msg, job, processed_text)
            await self._dedup.mark_sent(msg.source_chat_id, msg.message_id, job.target)
            await self.emit_audit(
                f"[✅ 转发] job={job.name} src={msg.source_chat_id} "
                f"msg={msg.message_id} dst={job.target}"
            )

        future = self._dispatcher.submit(
            _send,
            source_chat=msg.source_chat_id,
            target_chat=job.target,
            lineage=msg.lineage,
        )
        future.add_done_callback(self._log_dispatch_result)

    def _log_dispatch_result(self, future: asyncio.Future[None]) -> None:
        try:
            future.result()
        except Exception:
            logger.exception("dispatch task failed")

    def _passes_filters(self, text: str, job: ForwardJob) -> bool:
        content = text.casefold()
        if job.filters.whitelist:
            if not any(kw.casefold() in content for kw in job.filters.whitelist):
                return False
        if job.filters.blacklist:
            if any(kw.casefold() in content for kw in job.filters.blacklist):
                return False
        return True

    def _apply_modifications(self, text: str, job: ForwardJob) -> str:
        if self._config is None:
            return text

        rules: list[ReplacementRule] = []
        if job.use_template:
            template = self._config.templates[job.use_template]
            rules.extend(template.replacements)
        rules.extend(job.modifications)

        result = text
        for rule in rules:
            try:
                result = re.sub(rule.regex, rule.replace, result)
            except re.error:
                logger.warning("regex error in rule %s, skipping", rule.regex)
        return result

    def _get_client_for_job(self, job: ForwardJob) -> TelegramClient:
        if job.mode == "bot":
            if self._bot_client is None:
                raise RuntimeError("bot client not initialized")
            return self._bot_client
        if self._user_client is None:
            raise RuntimeError("userbot client not initialized")
        return self._user_client

    async def _copy_to_target(
        self,
        client: TelegramClient,
        msg: IngressMessage,
        job: ForwardJob,
        processed_text: str,
    ) -> None:
        text_modified = processed_text != msg.text
        has_media = msg.raw_message.media is not None

        if not text_modified:
            await client.send_message(entity=job.target, message=msg.raw_message)
        elif has_media:
            await client.send_file(
                entity=job.target,
                file=msg.raw_message.media,
                caption=processed_text,
                formatting_entities=msg.raw_message.entities,
            )
        else:
            await client.send_message(
                entity=job.target,
                message=processed_text,
                formatting_entities=msg.raw_message.entities,
                link_preview=bool(msg.raw_message.web_preview),
            )
