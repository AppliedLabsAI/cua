"""Safety guardrails for the CUA agent.

Enforces domain restrictions, resource limits, and optional human-in-the-loop
confirmation. Called by ActionRouter BEFORE executing any action.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

log = logging.getLogger(__name__)

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


_DESTRUCTIVE_ACTION_KEYWORDS: dict[str, list[str]] = {
    "form_submit": ["place order", "submit order", "submit payment", "complete checkout"],
    "purchase": ["pay now", "purchase now", "buy now", "complete order", "complete purchase"],
    "account_modify": ["delete account", "deactivate", "close account", "remove account"],
    "send_message": ["send email", "send message", "publish post", "post comment"],
}


@dataclass
class GuardrailConfig:
    """Configuration for CUA safety guardrails."""

    allowed_domains: list[str] | None = None
    blocked_domains: list[str] = field(default_factory=lambda: list(_BLOCKED_DOMAINS_DEFAULT))

    # Action categories to block — defaults to all destructive categories.
    # Set to [] to disable action classification.
    blocked_action_categories: list[str] = field(
        default_factory=lambda: list(_DESTRUCTIVE_ACTION_KEYWORDS.keys())
    )

    max_urls_visited: int = 50
    max_consecutive_errors: int = 5
    allow_private_networks: bool = False

    @staticmethod
    def from_dict(data: dict) -> GuardrailConfig:
        """Create a GuardrailConfig from a dict (e.g. parsed from JSON)."""
        known_fields = {f for f in GuardrailConfig.__dataclass_fields__}
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
        return GuardrailResult(allowed=False, reason="Blocked: localhost (SSRF protection)")

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
    """Outcome of a guardrail check."""

    allowed: bool
    reason: str | None = None


class GuardrailEngine:
    """Enforces safety boundaries on CUA actions."""

    def __init__(self, config: GuardrailConfig | None = None) -> None:
        self.config = config or GuardrailConfig()
        self._blocked_categories: frozenset[str] = frozenset(self.config.blocked_action_categories)
        self.urls_visited: set[str] = set()
        self.consecutive_errors: int = 0

    def check_url(self, url: str) -> GuardrailResult:
        """Check if a URL is allowed to be visited."""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
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
            if not any(fnmatch.fnmatch(domain, pat) for pat in self.config.allowed_domains):
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
        if url not in self.urls_visited and len(self.urls_visited) >= self.config.max_urls_visited:
            return GuardrailResult(
                allowed=False,
                reason=f"Max URL limit reached ({self.config.max_urls_visited})",
            )
        self.urls_visited.add(url)
        return self.check_url(url)

    def check_action(self, action: str, tool_input: dict) -> GuardrailResult:
        """Block clicks on destructive UI elements based on selector text.

        Playwright/Patchright selectors often embed button text (e.g.
        ``text=Submit``, ``role=button[name="Place Order"]``), so matching
        keywords against the selector string catches most destructive actions.
        """
        if action != "click" or not self._blocked_categories:
            return GuardrailResult(allowed=True)

        selector = tool_input.get("selector", "").lower()
        if not selector:
            return GuardrailResult(allowed=True)

        for category, keywords in _DESTRUCTIVE_ACTION_KEYWORDS.items():
            if category not in self._blocked_categories:
                continue
            for kw in keywords:
                if kw in selector:
                    return GuardrailResult(
                        allowed=False,
                        reason=(
                            f"Destructive action blocked: click selector matches "
                            f"'{category}' (keyword '{kw}')"
                        ),
                    )

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
