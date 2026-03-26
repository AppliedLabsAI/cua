"""Safety guardrails for the CUA agent.

Enforces domain restrictions, resource limits, and optional human-in-the-loop
confirmation. Called by ActionRouter BEFORE executing any action.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from settings import SAFETY_MODEL
from telemetry import get_tracer
from telemetry.metrics import safety_degraded_total
from telemetry.spans import (
    ATTR_GENAI_INPUT_TOKENS,
    ATTR_GENAI_MODEL,
    ATTR_GENAI_OUTPUT_TOKENS,
    ATTR_GENAI_SYSTEM,
    ATTR_GUARD_ALLOWED,
    ATTR_GUARD_REASON,
    ATTR_GUARD_USED_LLM,
    GUARDRAIL_LLM,
)

log = logging.getLogger(__name__)

# Regex-based fast path for destructive click detection.
# Matches against the selector text to avoid Haiku calls for obvious cases.
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

_BLOCKED_DOMAINS_DEFAULT = [
    # NOTE: *.bank.* only matches domains with literal ".bank." segment (e.g. foo.bank.example).
    # Real bank domains (chase.com, wellsfargo.com) must be added explicitly if needed.
    "*.bank.*",
    "*.gov",
    "mail.google.com",
    "outlook.live.com",
    "paypal.com",
    "venmo.com",
    "stripe.com",
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


@dataclass
class GuardrailConfig:
    """Configuration for CUA safety guardrails."""

    allowed_domains: list[str] | None = None
    blocked_domains: list[str] = field(
        default_factory=lambda: list(_BLOCKED_DOMAINS_DEFAULT)
    )

    max_urls_visited: int = 50
    max_consecutive_errors: int = 5
    allow_private_networks: bool = False
    enable_llm_action_check: bool = True

    @staticmethod
    def from_dict(data: dict) -> GuardrailConfig:
        """Create a GuardrailConfig from a dict (e.g. parsed from JSON)."""
        known_fields = set(GuardrailConfig.__dataclass_fields__)
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return GuardrailConfig(**filtered)


# Private IP ranges blocked by SSRF protection
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),  # Loopback
    ipaddress.ip_network("10.0.0.0/8"),  # RFC 1918
    ipaddress.ip_network("172.16.0.0/12"),  # RFC 1918
    ipaddress.ip_network("192.168.0.0/16"),  # RFC 1918
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local / cloud metadata
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique-local
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
]


def _check_ssrf(hostname: str) -> GuardrailResult | None:
    """Block requests to private/internal networks (SSRF protection).

    Returns a blocking GuardrailResult if the hostname resolves to a private
    IP range, or None if the hostname is safe.
    """
    if hostname in ("localhost", "localhost.localdomain"):
        return GuardrailResult(
            allowed=False, reason="Blocked: localhost (SSRF protection)"
        )

    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        # Not an IP literal — hostname is fine (DNS resolution happens at browser level)
        return None

    for network in _PRIVATE_NETWORKS:
        if addr in network:
            return GuardrailResult(
                allowed=False,
                reason=f"Blocked: private IP {hostname} (SSRF protection)",
            )

    return None


@dataclass
class GuardrailResult:
    """Outcome of a guardrail check.

    When needs_confirmation is True, the action is not hard-blocked but
    requires the agent to retry the same action to confirm intent.
    """

    allowed: bool
    reason: str | None = None
    needs_confirmation: bool = False


class GuardrailEngine:
    """Enforces safety boundaries on CUA actions."""

    def __init__(self, config: GuardrailConfig | None = None) -> None:
        self.config = config or GuardrailConfig()
        self.urls_visited: set[str] = set()
        self.consecutive_errors: int = 0
        self._llm_enabled = self.config.enable_llm_action_check
        self._llm_client = None
        self._approved_selectors: set[str] = set()
        self._pending_confirmations: set[str] = set()
        self._tracer = get_tracer()

    def check_url(self, url: str) -> GuardrailResult:
        """Check if a URL is allowed to be visited."""
        try:
            parsed = urlparse(url)
            domain = (parsed.hostname or "").lower()
        except Exception:
            return GuardrailResult(allowed=False, reason=f"Invalid URL: {url}")

        if not domain:
            return GuardrailResult(allowed=True)

        # SSRF protection — block private/internal networks unless explicitly allowed
        if not self.config.allow_private_networks:
            hostname = parsed.hostname or ""
            ssrf_result = _check_ssrf(hostname)
            if ssrf_result is not None:
                return ssrf_result

        # Allowlist takes precedence
        if self.config.allowed_domains is not None:
            if not any(
                fnmatch.fnmatch(domain, pat) for pat in self.config.allowed_domains
            ):
                return GuardrailResult(
                    allowed=False,
                    reason=f"Domain {domain} not in allowed list",
                )
            return GuardrailResult(allowed=True)

        # Blocklist check
        for pattern in self.config.blocked_domains:
            if fnmatch.fnmatch(domain, pattern):
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

    def _get_llm_client(self):
        if self._llm_client is None:
            from anthropic import Anthropic

            self._llm_client = Anthropic()
        return self._llm_client

    def _check_destructive_llm(self, selector: str) -> GuardrailResult:
        """Check if a click selector targets a destructive action.

        Layer 1 (always runs): Regex patterns catch obvious destructive/safe
        selectors in microseconds. Works even when LLM is disabled.

        Layer 2 (optional): Haiku LLM validates ambiguous selectors only
        when ``_llm_enabled`` is True. When disabled, ambiguous selectors
        are allowed (fail-open) — the regex layer still blocks known
        destructive patterns.

        Returns GuardrailResult(allowed=False) if destructive, allowed=True otherwise.
        """
        normalized = selector.strip().lower()
        if normalized in self._approved_selectors:
            return GuardrailResult(allowed=True)

        # --- Layer 1: Regex (always runs, even when LLM disabled) ---
        if _DESTRUCTIVE_RE.search(normalized):
            log.warning("Regex flagged destructive click: %s", selector)
            return GuardrailResult(
                allowed=False,
                reason=f"Destructive action blocked (pattern match): {selector}",
                needs_confirmation=True,
            )
        if _SAFE_CLICK_RE.search(normalized):
            self._approved_selectors.add(normalized)
            return GuardrailResult(allowed=True)

        # --- Layer 2: LLM fallback for ambiguous selectors ---
        if not self._llm_enabled:
            # LLM disabled — allow ambiguous selectors (regex layer above
            # still catches known destructive patterns).
            self._approved_selectors.add(normalized)
            return GuardrailResult(allowed=True)

        with self._tracer.start_as_current_span(
            GUARDRAIL_LLM,
            attributes={
                ATTR_GENAI_SYSTEM: "anthropic",
                ATTR_GENAI_MODEL: SAFETY_MODEL,
                ATTR_GUARD_USED_LLM: True,
            },
        ) as llm_span:
            try:
                prompt = _DESTRUCTIVE_CHECK_PROMPT.format(selector=selector)
                response = self._get_llm_client().messages.create(
                    model=SAFETY_MODEL,
                    max_tokens=100,
                    messages=[{"role": "user", "content": prompt}],
                )

                llm_span.set_attributes(
                    {
                        ATTR_GENAI_INPUT_TOKENS: response.usage.input_tokens,
                        ATTR_GENAI_OUTPUT_TOKENS: response.usage.output_tokens,
                    }
                )

                block = response.content[0]
                text: str = str(block.text) if hasattr(block, "text") else ""
                text = text.strip()

                if text.startswith("```"):
                    text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

                data = json.loads(text)
                is_destructive = data.get("destructive", False)
                reason = data.get("reason", "")

                if is_destructive:
                    log.warning(
                        "Haiku flagged destructive click: %s (%s)", selector, reason
                    )
                    llm_span.set_attributes(
                        {
                            ATTR_GUARD_ALLOWED: False,
                            ATTR_GUARD_REASON: reason[:500],
                        }
                    )
                    return GuardrailResult(
                        allowed=False,
                        reason=f"Destructive action blocked (LLM): {reason}",
                        needs_confirmation=True,
                    )

                self._approved_selectors.add(normalized)
                llm_span.set_attributes({ATTR_GUARD_ALLOWED: True})
                log.debug("Haiku approved click: %s (%s)", selector, reason)
                return GuardrailResult(allowed=True)

            except Exception as exc:
                log.warning(
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
                return GuardrailResult(
                    allowed=False,
                    reason=("Safety validation unavailable for ambiguous click action"),
                )

    def check_action(
        self, action: str, tool_input: dict, *, skip_llm: bool = False
    ) -> GuardrailResult:
        """Check clicks for destructive intent using Haiku with confirmation flow.

        When a destructive action is detected, it is not hard-blocked. Instead,
        the result has needs_confirmation=True, prompting the agent to confirm.
        If the agent retries the same selector, the action is allowed through.

        Set skip_llm=True when an outer layer (e.g. ScopeVerifier) will
        perform its own LLM validation.
        """
        if action != "click":
            return GuardrailResult(allowed=True)

        selector = tool_input.get("selector", "").lower()
        if not selector:
            return GuardrailResult(allowed=True)

        # Agent retry: selector is pending confirmation → allow (confirmed)
        normalized = selector.strip().lower()
        if normalized in self._pending_confirmations:
            self._pending_confirmations.discard(normalized)
            log.info("Agent confirmed destructive action: %s", selector)
            return GuardrailResult(allowed=True)

        # Haiku LLM check
        if not skip_llm:
            llm_result = self._check_destructive_llm(selector)
            if not llm_result.allowed:
                if llm_result.needs_confirmation:
                    self._pending_confirmations.add(normalized)
                return llm_result
            return llm_result

        return GuardrailResult(allowed=True)

    def record_error(self) -> GuardrailResult | None:
        """Track consecutive errors. Return stop signal if too many."""
        self.consecutive_errors += 1
        if self.consecutive_errors > self.config.max_consecutive_errors:
            return GuardrailResult(
                allowed=False,
                reason=(
                    f"Too many consecutive errors ({self.consecutive_errors}) — agent appears stuck"
                ),
            )
        return None

    def record_success(self) -> None:
        """Reset error counter on success."""
        self.consecutive_errors = 0
