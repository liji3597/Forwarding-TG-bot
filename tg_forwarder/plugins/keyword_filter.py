from __future__ import annotations

from collections.abc import Sequence


class KeywordFilter:
    @staticmethod
    def matches(
        text: str,
        whitelist: Sequence[str],
        blacklist: Sequence[str],
    ) -> tuple[bool, str | None]:
        content = text.casefold()

        wl = [t.strip() for t in whitelist if t and t.strip()]
        if wl and not any(t.casefold() in content for t in wl):
            return False, "whitelist_not_matched"

        for term in blacklist:
            normalized = term.strip()
            if normalized and normalized.casefold() in content:
                return False, f"blacklist:{normalized}"

        return True, None
