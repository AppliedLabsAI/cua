"""Destructive-click classification policy."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

_DESTRUCTIVE_RE = re.compile(
    r"delete|remove|destroy|deactivate|close.account|terminate|cancel.subscription"
    r"|refund|issue.refund|charge.?back"
    r"|pay.now|buy.now|purchase|place.order|submit.order|complete.purchase|checkout"
    r"|confirm.transfer|send.money|wire.transfer"
    r"|send.email|send.message|publish|post.comment|submit.review"
    r"|yes.*delete|confirm.*remov|approve.*refund",
    re.IGNORECASE,
)
_SAFE_CLICK_RE = re.compile(
    r"^(text=|role=)?(nav|menu|tab|link|filter|sort|search|view|show|open|expand"
    r"|collapse|back|next|prev|page|details|info|settings|edit|close$"
    r"|cancel$|dismiss|log.?in|sign.?in|submit$|save$|apply$|select|choose)",
    re.IGNORECASE,
)


class DestructiveClickPolicy:
    """Classify click selectors with regex fast paths and optional LLM fallback."""

    def __init__(
        self,
        *,
        llm_enabled: bool,
        llm_check: Callable[[str], Awaitable[str | None]],
    ) -> None:
        self._llm_enabled = llm_enabled
        self._llm_check = llm_check
        self._approved_selectors: set[str] = set()

    def set_llm_enabled(self, enabled: bool) -> None:
        """Update whether ambiguous selectors may fall back to the LLM check."""
        self._llm_enabled = enabled

    async def check(self, selector: str) -> str | None:
        """Return a block reason, or ``None`` when the click is allowed."""
        normalized = selector.strip().lower()
        if normalized in self._approved_selectors:
            return None

        if _DESTRUCTIVE_RE.search(normalized):
            return f"Destructive action blocked (pattern match): {selector}"
        if _SAFE_CLICK_RE.search(normalized):
            self._approved_selectors.add(normalized)
            return None
        if not self._llm_enabled:
            self._approved_selectors.add(normalized)
            return None

        reason = await self._llm_check(selector)
        if reason is None:
            self._approved_selectors.add(normalized)
        return reason
