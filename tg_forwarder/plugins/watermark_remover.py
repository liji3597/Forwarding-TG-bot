from __future__ import annotations

import re


class WatermarkRemover:
    _ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\u2060\ufeff]")
    _CHANNEL_LINK_RE = re.compile(
        r"(?:https?://)?t\.me/(?:joinchat/)?[A-Za-z0-9_+/]+", re.I
    )
    _AD_PATTERNS = (
        re.compile(r"(?im)^\s*(?:广告|推广|商务合作|合作联系)[^\n]*$"),
        re.compile(r"(?im)^\s*(?:关注|订阅)[^\n]*(?:频道|群组|群聊)[^\n]*$"),
        re.compile(r"(?im)^\s*@[\w\d_]{4,}\s*$"),
    )

    @classmethod
    def clean(cls, text: str) -> str:
        cleaned = cls._ZERO_WIDTH_RE.sub("", text)
        cleaned = cls.remove_channel_links(cleaned)
        for pattern in cls._AD_PATTERNS:
            cleaned = pattern.sub("", cleaned)
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        return "\n".join(lines)

    @classmethod
    def remove_channel_links(cls, text: str) -> str:
        return cls._CHANNEL_LINK_RE.sub("", text)
