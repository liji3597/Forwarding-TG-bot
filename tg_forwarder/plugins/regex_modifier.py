from __future__ import annotations

import logging
import re

from tg_forwarder.config.schema import ReplacementRule

logger = logging.getLogger(__name__)


class RegexModifier:
    @staticmethod
    def apply(text: str, rules: list[ReplacementRule]) -> str:
        result = text
        for rule in rules:
            try:
                result = re.sub(rule.regex, rule.replace, result)
            except re.error:
                logger.warning("invalid regex rule skipped: %s", rule.regex)
        return result
