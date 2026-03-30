"""Safety guardrails for the CUA agent.

Enforces domain restrictions, resource limits, and optional LLM-backed click
classification. Called by ActionRouter before executing any action.
"""

from __future__ import annotations

import concurrent.futures
import fnmatch
import ipaddress
import logging
import re
import socket
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from guardrails.stuck import StuckVerdict

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent

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
    # Government
    "*.gov",
    "*.gov.*",
    # Banking — generic pattern + major US/UK banks
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
    # Email
    "mail.google.com",
    "outlook.live.com",
    "outlook.office.com",
    # Payment / financial
    "paypal.com",
    "*.paypal.com",
    "venmo.com",
    "stripe.com",
    "*.stripe.com",
    "square.com",
    "cash.app",
    # Social media
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


_destructive_checker: Agent[None, DestructiveCheckResult] | None = None


def _get_destructive_checker() -> Agent[None, DestructiveCheckResult]:
    """Build the destructive-action checker lazily to avoid import-time provider resolution."""
    global _destructive_checker
    if _destructive_checker is None:
        _destructive_checker = Agent[None, DestructiveCheckResult](
            UTILITY_MODEL,
            output_type=DestructiveCheckResult,
            instructions=_DESTRUCTIVE_CHECK_PROMPT,
            model_settings={"max_tokens": 100},
        )
    return _destructive_checker


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

    # Stuck detection thresholds
    stuck_window_size: int = 8
    stuck_repeat_hint: int = 3
    stuck_repeat_warn: int = 5
    stuck_repeat_stop: int = 7
    stuck_cycle_max_length: int = 3
    stuck_cycle_repeats: int = 3


# Private IP ranges blocked by SSRF protection
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),  # "This" network (includes 0.0.0.0)
    ipaddress.ip_network("127.0.0.0/8"),  # Loopback
    ipaddress.ip_network("10.0.0.0/8"),  # RFC 1918
    ipaddress.ip_network("172.16.0.0/12"),  # RFC 1918
    ipaddress.ip_network("192.168.0.0/16"),  # RFC 1918
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local / cloud metadata
    ipaddress.ip_network("::/128"),  # IPv6 unspecified (::)
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique-local
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
]


def _is_private_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if an IP address falls within any blocked private network."""
    return any(addr in network for network in _PRIVATE_NETWORKS)


# Cache DNS resolution results to avoid repeated lookups in tight loops.
# Key: hostname, Value: GuardrailResult | None (None = safe).
_dns_cache: dict[str, GuardrailResult | None] = {}
_DNS_CACHE_MAX = 1024


_DNS_TIMEOUT_S = 2.0  # Max time to wait for DNS resolution


def _resolve_and_check(hostname: str) -> GuardrailResult | None:
    """Resolve a hostname via DNS and check all returned IPs against private ranges.

    Uses a thread pool with a timeout to prevent slow DNS from blocking
    request handling.
    """

    def _do_resolve():
        return socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_do_resolve)
            addrinfos = future.result(timeout=_DNS_TIMEOUT_S)
        for _family, _type, _proto, _canonname, sockaddr in addrinfos:
            ip_str = sockaddr[0]
            try:
                addr = ipaddress.ip_address(ip_str)
                if _is_private_ip(addr):
                    log.warning(
                        "SSRF blocked: %s resolves to private IP %s", hostname, ip_str
                    )
                    return GuardrailResult(
                        allowed=False,
                        reason=f"Blocked: {hostname} resolves to private IP {ip_str} (SSRF protection)",
                    )
            except ValueError:
                continue
    except (socket.gaierror, concurrent.futures.TimeoutError):
        # DNS resolution failed or timed out — allow (browser will fail gracefully)
        pass
    return None


def _check_ssrf(hostname: str) -> GuardrailResult | None:
    """Block requests to private/internal networks (SSRF protection).

    Returns a blocking GuardrailResult if the hostname resolves to a private
    IP range, or None if the hostname is safe.

    Performs DNS resolution for non-IP hostnames to prevent bypass via
    attacker-controlled domains that resolve to internal IPs (e.g., cloud
    metadata endpoints like 169.254.169.254).
    """
    if hostname in ("localhost", "localhost.localdomain"):
        return GuardrailResult(
            allowed=False, reason="Blocked: localhost (SSRF protection)"
        )

    # Check IP literals directly
    try:
        addr = ipaddress.ip_address(hostname)
        if _is_private_ip(addr):
            return GuardrailResult(
                allowed=False,
                reason=f"Blocked: private IP {hostname} (SSRF protection)",
            )
        return None
    except ValueError:
        pass  # Not an IP literal — resolve hostname below

    # Check cache before doing DNS resolution
    if hostname in _dns_cache:
        return _dns_cache[hostname]

    result = _resolve_and_check(hostname)

    # Store in cache (evict all if cache grows too large)
    if len(_dns_cache) >= _DNS_CACHE_MAX:
        _dns_cache.clear()
    _dns_cache[hostname] = result

    return result


class GuardrailResult(BaseModel):
    """Outcome of a guardrail check.

    `needs_confirmation` is retained for backwards compatibility but is not
    used by the default autonomous flow.
    """

    allowed: bool
    reason: str | None = None
    needs_confirmation: bool = False


def _domain_matches(domain: str, pattern: str) -> bool:
    """Match a domain against a glob pattern, handling bare domains correctly.

    fnmatch("irs.gov", "*.gov") returns True (``*`` matches ``irs``), but
    fnmatch("gov", "*.gov") returns False because ``*`` must match at least
    one character. This helper also strips the ``*.`` prefix and checks the
    bare suffix so patterns like ``*.gov`` additionally match ``gov`` itself.
    """
    if fnmatch.fnmatch(domain, pattern):
        return True
    # For patterns like "*.example.com", also match "example.com" itself
    if pattern.startswith("*."):
        bare = pattern[2:]  # "*.gov" → "gov", "*.bank.*" → "bank.*"
        if fnmatch.fnmatch(domain, bare) or domain == bare:
            return True
    return False


class GuardrailEngine:
    """Enforces safety boundaries on CUA actions."""

    def __init__(self, config: GuardrailConfig | None = None) -> None:
        from guardrails.stuck import StuckDetector

        self.config = config or GuardrailConfig()
        self.urls_visited: set[str] = set()
        self.consecutive_errors: int = 0
        self._llm_enabled = self.config.enable_llm_action_check
        self._approved_selectors: set[str] = set()
        self._tracer = get_tracer()
        self._stuck = StuckDetector(
            window_size=self.config.stuck_window_size,
            repeat_hint=self.config.stuck_repeat_hint,
            repeat_warn=self.config.stuck_repeat_warn,
            repeat_stop=self.config.stuck_repeat_stop,
            cycle_max_length=self.config.stuck_cycle_max_length,
            cycle_repeats=self.config.stuck_cycle_repeats,
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

        # SSRF protection — block private/internal networks unless explicitly allowed
        if not self.config.allow_private_networks:
            hostname = parsed.hostname or ""
            ssrf_result = _check_ssrf(hostname)
            if ssrf_result is not None:
                return ssrf_result

        # Allowlist takes precedence
        if self.config.allowed_domains is not None:
            if not any(
                _domain_matches(domain, pat) for pat in self.config.allowed_domains
            ):
                return GuardrailResult(
                    allowed=False,
                    reason=f"Domain {domain} not in allowed list",
                )
            return GuardrailResult(allowed=True)

        # Blocklist check
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

    async def _check_destructive_llm(self, selector: str) -> GuardrailResult:
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
                ATTR_GENAI_MODEL: UTILITY_MODEL,
                ATTR_GUARD_USED_LLM: True,
            },
        ) as llm_span:
            try:
                prompt = f"Proposed click target: {selector}"
                result = await _get_destructive_checker().run(prompt)
                usage = result.usage()

                llm_span.set_attributes(
                    {
                        ATTR_GENAI_INPUT_TOKENS: usage.input_tokens or 0,
                        ATTR_GENAI_OUTPUT_TOKENS: usage.output_tokens or 0,
                    }
                )

                is_destructive = result.output.destructive
                reason = result.output.reason

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

    async def check_action(
        self, action: str, tool_input: dict, *, skip_llm: bool = False
    ) -> GuardrailResult:
        """Check clicks for destructive intent.

        This method only owns click classification when ``skip_llm`` is False.
        When an outer layer already performs task-alignment validation, set
        ``skip_llm=True`` and this method becomes a no-op for click intent.
        """
        if action != "click":
            return GuardrailResult(allowed=True)

        selector = tool_input.get("selector", "").lower()
        if not selector:
            return GuardrailResult(allowed=True)

        # When an outer layer owns the decision (e.g. task-alignment validation),
        # do not run destructive-click checks here.
        if skip_llm:
            return GuardrailResult(allowed=True)

        return await self._check_destructive_llm(selector)

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

    def record_action(self, input_summary: str, *, success: bool) -> StuckVerdict:
        """Track action for stuck pattern detection.

        Called after every action execution (both success and failure).
        Returns a verdict indicating whether the agent appears stuck.
        """
        return self._stuck.record(input_summary, success=success)
