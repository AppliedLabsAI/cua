"""Scope Verifier — deterministic pre-execution check against TaskScope.

Replaces reactive keyword-matching with structural verification:
the action must be within the task's allowed actions and domains.
Preserves existing SSRF and navigation limit checks from GuardrailEngine.
"""

from __future__ import annotations

import fnmatch
import logging
from urllib.parse import urlparse

from blinders.scope import TaskScope
from guardrails import GuardrailEngine

log = logging.getLogger(__name__)


class ScopeVerifier:
    """Deterministic pre-execution check against TaskScope.

    Non-bypassable by prompt injection because it operates on the
    TaskScope derived from trusted user input, not from web content.
    """

    def __init__(self, scope: TaskScope, guardrails: GuardrailEngine) -> None:
        self.scope = scope
        self.guardrails = guardrails

    def check(self, action: str, tool_input: dict) -> str | None:
        """Check if an action is allowed. Returns reason if blocked, None if allowed."""
        # 1. Action type restriction — structural, not behavioral
        if action not in self.scope.allowed_actions:
            log.info(
                "Scope blocked action '%s' (goal_type=%s)",
                action,
                self.scope.goal_type,
            )
            return f"Action '{action}' not allowed for {self.scope.goal_type} tasks"

        # 2. Domain scope (for goto actions)
        if action == "goto":
            url = tool_input.get("url", "")
            domain_block = self._check_domain(url)
            if domain_block:
                return domain_block

            # Existing SSRF protection (kept — orthogonal defense)
            ssrf = self.guardrails.check_url(url)
            if not ssrf.allowed:
                return ssrf.reason

            # Existing navigation limit (kept — operational safeguard)
            nav = self.guardrails.check_navigation(url)
            if not nav.allowed:
                return nav.reason

        # 3. Existing action classification (kept as defense in depth)
        action_check = self.guardrails.check_action(action, tool_input)
        if not action_check.allowed:
            return action_check.reason

        # 4. execute_sequence: verify each step recursively
        if action == "execute_sequence":
            for step in tool_input.get("steps", []):
                step_action = step.get("action", "")
                result = self.check(step_action, step)
                if result:
                    return result

        return None

    def _check_domain(self, url: str) -> str | None:
        """Check if URL's domain is within the task scope.

        If allowed_domains is empty (no URLs found in directive),
        domain scoping is not applied — falls through to existing
        guardrail domain checks.
        """
        if not self.scope.allowed_domains:
            return None  # No domain restriction from scope

        try:
            parsed = urlparse(url)
            domain = (parsed.hostname or "").lower()
        except Exception:
            return f"Invalid URL: {url}"

        if not domain:
            return None

        for pattern in self.scope.allowed_domains:
            if fnmatch.fnmatch(domain, pattern):
                return None

        log.info(
            "Scope blocked domain '%s' (allowed: %s)",
            domain,
            self.scope.allowed_domains,
        )
        return f"Domain '{domain}' not in task scope"
