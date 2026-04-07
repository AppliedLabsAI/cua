"""Safety guardrails for the CUA agent."""

from __future__ import annotations

import fnmatch
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent

from guardrails.destructive import DestructiveClickPolicy
from guardrails.dns import DnsProtection
from settings import UTILITY_MODEL
from telemetry import get_tracer
from telemetry.metrics import safety_degraded_total
from telemetry.spans import (
    ATTR_GENAI_INPUT_TOKENS,
    ATTR_GENAI_MODEL,
    ATTR_GENAI_OUTPUT_TOKENS,
    ATTR_GUARD_ALLOWED,
    ATTR_GUARD_REASON,
    ATTR_GUARD_USED_LLM,
    GUARDRAIL_LLM,
)

if TYPE_CHECKING:
    from guardrails.stuck import StuckVerdict

logger = logging.getLogger(__name__)

_BLOCKED_DOMAINS_DEFAULT = [
    "*.gov",
    "*.gov.*",
    "*.bank.*",
    "chase.com",
    "*.chase.com",
    "wellsfargo.com",
    "*.wellsfargo.com",
    "bankofamerica.com",
    "*.bankofamerica.com",
    "citi.com",
    "*.citi.com",
    "usbank.com",
    "*.usbank.com",
    "capitalone.com",
    "*.capitalone.com",
    "hsbc.com",
    "*.hsbc.com",
    "barclays.co.uk",
    "*.barclays.co.uk",
    "mail.google.com",
    "outlook.live.com",
    "outlook.office.com",
    "paypal.com",
    "*.paypal.com",
    "venmo.com",
    "stripe.com",
    "*.stripe.com",
    "square.com",
    "cash.app",
    "twitter.com",
    "x.com",
    "facebook.com",
    "linkedin.com",
    "instagram.com",
    "tiktok.com",
]

_DESTRUCTIVE_CHECK_PROMPT = """\
You are a safety classifier for a browser automation agent. Determine whether \
the proposed click action is DESTRUCTIVE or IRREVERSIBLE.

Proposed click target: {selector}

A click is DESTRUCTIVE if it would:
- Submit a purchase, payment, or financial transaction
- Delete, deactivate, or close an account
- Send an email, message, or publish content
- Confirm an irreversible operation (e.g., "Yes, delete", "Confirm transfer")
- Submit a form that commits to an action with real-world consequences

A click is NOT destructive if it:
- Navigates to another page, opens a menu, or expands a section
- Selects an option, filters results, or sorts data
- Opens a modal/dialog that still requires further confirmation
- Is part of a read-only information gathering workflow

Respond with ONLY a JSON object:
{{"destructive": true, "reason": "brief reason"}} or \
{{"destructive": false, "reason": "brief reason"}}"""


class DestructiveCheckResult(BaseModel):
    """Structured response from the destructive click checker."""

    destructive: bool = False
    reason: str = ""


class GuardrailConfig(BaseModel):
    """Configuration for CUA safety guardrails."""

    model_config = ConfigDict(extra="ignore")

    allowed_domains: list[str] | None = None
    blocked_domains: list[str] = Field(
        default_factory=lambda: list(_BLOCKED_DOMAINS_DEFAULT)
    )
    max_urls_visited: int = 50
    max_consecutive_errors: int = 5
    allow_private_networks: bool = False
    enable_llm_action_check: bool = True
    stuck_window_size: int = 12
    stuck_repeat_hint: int = 3
    stuck_repeat_warn: int = 5
    stuck_repeat_stop: int = 7
    stuck_cycle_max_length: int = 3
    stuck_cycle_repeats: int = 3
    stuck_revisit_gap: int = 5
    stuck_failure_cluster_window: int = 5
    stuck_failure_cluster_threshold: int = 3


class GuardrailResult(BaseModel):
    """Outcome of a guardrail check."""

    allowed: bool
    reason: str | None = None
    needs_confirmation: bool = False


_destructive_checker: Agent[None, DestructiveCheckResult] | None = None


def _get_destructive_checker() -> Agent[None, DestructiveCheckResult]:
    """Build the destructive-action checker lazily to avoid import-time resolution."""
    global _destructive_checker
    if _destructive_checker is None:
        _destructive_checker = Agent[None, DestructiveCheckResult](
            UTILITY_MODEL,
            output_type=DestructiveCheckResult,
            instructions=_DESTRUCTIVE_CHECK_PROMPT,
            model_settings={"max_tokens": 100},
        )
    return _destructive_checker


def _domain_matches(domain: str, pattern: str) -> bool:
    """Match a domain against a glob pattern, handling bare domains correctly."""
    if fnmatch.fnmatch(domain, pattern):
        return True
    if pattern.startswith("*."):
        bare = pattern[2:]
        if fnmatch.fnmatch(domain, bare) or domain == bare:
            return True
    return False


def _check_ssrf(hostname: str) -> GuardrailResult | None:
    """Block requests to private/internal networks (SSRF protection)."""
    reason = DnsProtection(cache_max=0).check_hostname(hostname)
    if reason is None:
        return None
    return GuardrailResult(allowed=False, reason=reason)


class GuardrailEngine:
    """Enforces safety boundaries on CUA actions."""

    def __init__(self, config: GuardrailConfig | None = None) -> None:
        from guardrails.stuck import StuckDetector

        self.config = config or GuardrailConfig()
        self.urls_visited: set[str] = set()
        self.consecutive_errors = 0
        self._tracer = get_tracer()
        self._dns = DnsProtection()
        self._llm_enabled = self.config.enable_llm_action_check
        self._destructive = DestructiveClickPolicy(
            llm_enabled=self._llm_enabled,
            llm_check=self._check_destructive_with_llm,
        )
        self._stuck = StuckDetector(
            window_size=self.config.stuck_window_size,
            repeat_hint=self.config.stuck_repeat_hint,
            repeat_warn=self.config.stuck_repeat_warn,
            repeat_stop=self.config.stuck_repeat_stop,
            cycle_max_length=self.config.stuck_cycle_max_length,
            cycle_repeats=self.config.stuck_cycle_repeats,
            revisit_gap=self.config.stuck_revisit_gap,
            failure_cluster_window=self.config.stuck_failure_cluster_window,
            failure_cluster_threshold=self.config.stuck_failure_cluster_threshold,
        )

    def check_url(self, url: str) -> GuardrailResult:
        """Check if a URL is allowed to be visited."""
        try:
            parsed = urlparse(url)
            domain = (parsed.hostname or "").lower()
        except Exception:
            return GuardrailResult(allowed=False, reason=f"Invalid URL: {url}")

        if not domain:
            return GuardrailResult(allowed=True)

        if not self.config.allow_private_networks:
            reason = self._dns.check_hostname(parsed.hostname or "")
            if reason is not None:
                return GuardrailResult(allowed=False, reason=reason)

        if self.config.allowed_domains is not None:
            if not any(
                _domain_matches(domain, pat) for pat in self.config.allowed_domains
            ):
                return GuardrailResult(
                    allowed=False,
                    reason=f"Domain {domain} not in allowed list",
                )
            return GuardrailResult(allowed=True)

        for pattern in self.config.blocked_domains:
            if _domain_matches(domain, pattern):
                return GuardrailResult(
                    allowed=False,
                    reason=f"Domain {domain} is blocked (matches {pattern})",
                )

        return GuardrailResult(allowed=True)

    def check_navigation(self, url: str) -> GuardrailResult:
        """Check URL + track visit count."""
        if (
            url not in self.urls_visited
            and len(self.urls_visited) >= self.config.max_urls_visited
        ):
            return GuardrailResult(
                allowed=False,
                reason=f"Max URL limit reached ({self.config.max_urls_visited})",
            )
        self.urls_visited.add(url)
        return self.check_url(url)

    async def _check_destructive_with_llm(self, selector: str) -> str | None:
        with self._tracer.start_as_current_span(
            GUARDRAIL_LLM,
            attributes={
                ATTR_GENAI_MODEL: UTILITY_MODEL,
                ATTR_GUARD_USED_LLM: True,
            },
        ) as llm_span:
            try:
                result = await _get_destructive_checker().run(
                    f"Proposed click target: {selector}"
                )
                usage = result.usage()
                llm_span.set_attributes(
                    {
                        ATTR_GENAI_INPUT_TOKENS: usage.input_tokens or 0,
                        ATTR_GENAI_OUTPUT_TOKENS: usage.output_tokens or 0,
                    }
                )
                if result.output.destructive:
                    reason = result.output.reason
                    logger.warning(
                        "Haiku flagged destructive click: %s (%s)", selector, reason
                    )
                    llm_span.set_attributes(
                        {
                            ATTR_GUARD_ALLOWED: False,
                            ATTR_GUARD_REASON: reason[:500],
                        }
                    )
                    return f"Destructive action blocked (LLM): {reason}"

                llm_span.set_attributes({ATTR_GUARD_ALLOWED: True})
                logger.debug(
                    "Haiku approved click: %s (%s)", selector, result.output.reason
                )
                return None
            except Exception as exc:
                logger.warning(
                    "Haiku destructive check unavailable, blocking ambiguous click: %s",
                    exc,
                )
                safety_degraded_total().add(
                    1,
                    {"component": "guardrail_destructive_check", "fallback": "block"},
                )
                llm_span.set_attributes(
                    {
                        ATTR_GUARD_ALLOWED: False,
                        ATTR_GUARD_REASON: "validation unavailable",
                    }
                )
                return "Safety validation unavailable for ambiguous click action"

    async def check_action(
        self, action: str, tool_input: dict, *, skip_llm: bool = False
    ) -> GuardrailResult:
        """Check clicks for destructive intent."""
        if action != "click":
            return GuardrailResult(allowed=True)

        selector = tool_input.get("selector", "").lower()
        if not selector or skip_llm:
            return GuardrailResult(allowed=True)

        self._destructive.set_llm_enabled(self._llm_enabled)
        reason = await self._destructive.check(selector)
        if reason is None:
            return GuardrailResult(allowed=True)
        return GuardrailResult(allowed=False, reason=reason)

    def record_error(self) -> GuardrailResult | None:
        """Track consecutive errors. Return stop signal if too many."""
        self.consecutive_errors += 1
        if self.consecutive_errors > self.config.max_consecutive_errors:
            return GuardrailResult(
                allowed=False,
                reason=(
                    f"Too many consecutive errors ({self.consecutive_errors}) — "
                    "agent appears stuck"
                ),
            )
        return None

    def record_success(self) -> None:
        """Reset error counter on success."""
        self.consecutive_errors = 0

    def record_action(
        self,
        action: str,
        tool_input: dict,
        input_summary: str,
        *,
        success: bool,
        visited_urls: list[str] | None = None,
    ) -> StuckVerdict:
        """Track action for stuck pattern detection."""
        return self._stuck.record(
            action,
            tool_input,
            input_summary=input_summary,
            success=success,
            visited_urls=visited_urls,
        )
