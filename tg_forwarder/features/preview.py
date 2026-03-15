from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from telethon import TelegramClient
from telethon.tl import types

from tg_forwarder.config.schema import ForwardJob, ReplacementRule
from tg_forwarder.core.engine import ForwardingEngine
from tg_forwarder.plugins.keyword_filter import KeywordFilter

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PreviewResult:
    original_text: str
    modified_text: str
    passed_filter: bool
    filter_reason: str | None
    applied_rules: list[str]


class PreviewHandler:
    _LINK_RE = re.compile(r"(?:https?://)?t\.me/c/(\d+)/(\d+)")

    def __init__(self, engine: ForwardingEngine) -> None:
        self._engine = engine

    async def preview(
        self,
        client: TelegramClient,
        message_link: str,
        job_name: str,
    ) -> PreviewResult:
        config = self._engine.config
        if config is None:
            raise RuntimeError("engine config not loaded")

        job = self._find_job(job_name)
        chat_id, message_id = self._parse_link(message_link)

        raw = await client.get_messages(entity=chat_id, ids=message_id)
        message = raw[0] if isinstance(raw, list) else raw
        if not isinstance(message, types.Message):
            raise ValueError("message not found")

        original_text = message.message or ""
        passed, reason = KeywordFilter.matches(
            original_text, job.filters.whitelist, job.filters.blacklist
        )
        modified_text, applied_rules = self._apply_rules(
            original_text, self._rules_for_job(job)
        )
        return PreviewResult(
            original_text=original_text,
            modified_text=modified_text,
            passed_filter=passed,
            filter_reason=reason,
            applied_rules=applied_rules,
        )

    def _find_job(self, job_name: str) -> ForwardJob:
        config = self._engine.config
        if config is None:
            raise RuntimeError("engine config not loaded")
        for job in config.jobs:
            if job.name == job_name:
                return job
        raise ValueError(f"job `{job_name}` not found")

    def _rules_for_job(self, job: ForwardJob) -> list[ReplacementRule]:
        config = self._engine.config
        if config is None:
            return []
        rules: list[ReplacementRule] = []
        if job.use_template:
            template = config.templates.get(job.use_template)
            if template:
                rules.extend(template.replacements)
        rules.extend(job.modifications)
        return rules

    def _parse_link(self, message_link: str) -> tuple[int, int]:
        match = self._LINK_RE.search(message_link.strip())
        if not match:
            raise ValueError("invalid link, expected t.me/c/CHAT_ID/MSG_ID")
        return int(f"-100{match.group(1)}"), int(match.group(2))

    @staticmethod
    def _apply_rules(
        text: str, rules: list[ReplacementRule]
    ) -> tuple[str, list[str]]:
        result = text
        applied: list[str] = []
        for rule in rules:
            try:
                updated = re.sub(rule.regex, rule.replace, result)
            except re.error:
                logger.warning("invalid regex in preview: %s", rule.regex)
                continue
            if updated != result:
                applied.append(rule.regex)
            result = updated
        return result, applied
